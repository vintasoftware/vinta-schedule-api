# Every organization always has exactly one active plan, from creation, with no
# plan-less state. This backfills every *pre-existing* billing-root organization
# onto `unlimited` — deliberately not `free`. `free` carries real ceilings;
# applying them to organizations that predate this feature would block them as a
# side effect of the rollout itself, which must never happen. `unlimited` is the
# declared rollout switch (every `PlanLimit.limit_value` is NULL), so this
# migration changes no organization's observable behavior.
#
# A billing root is an organization with no parent, OR an organization that can
# itself invite/create other organizations (a nested reseller is its own billing
# root, not a child pooling against a grandparent's subscription). This predicate
# is FROZEN as `BILLING_ROOT_Q` below, matching the host's configured hierarchy
# (`payments.seams.hierarchy.ResellerHierarchy`: `parent_field="parent"`,
# `root_flag_field="can_invite_organizations"`, which
# `vinta_billing.hierarchy.ParentFieldHierarchy.billing_root_q()` turns into
# `Q(parent__isnull=True) | Q(can_invite_organizations=True)`), rather than
# imported from `vinta_billing.services.subscription_service.billing_root_filter`.
# That function resolves `settings.VINTA_BILLING["HIERARCHY"]` *at call time*, not
# at migration-write time -- if the setting is ever unset, dropped, or repointed
# (e.g. reverted to the package's default `FlatHierarchy`, whose `billing_root_q()`
# is a bare `Q()` matching every row), a fresh `migrate` would silently backfill a
# `Subscription` for every organization, including reseller children, which is
# exactly what this migration must never do. Row selection is not "behaviour" that
# may float with the live setting -- it is data this migration writes, so it is
# frozen the same way `payments.0007` freezes `LimitKind` and the resource /
# entitlement keys.
#
# `MissingSeedBillingPlanError` (see the local copy below) is frozen for the same
# reason: a future rename of the package exception would otherwise turn this
# import into an `ImportError` that fails every test database build (`migrate`
# from zero).
#
# Keyset-paginated on `pk` (not an in-memory id list) so this never materializes
# more than one batch of organizations at a time, regardless of table size.
import datetime

from django.db import migrations
from django.db.models import Q
from django.utils import timezone


#: Frozen local copy of `vinta_billing.hierarchy.ParentFieldHierarchy.billing_root_q()`
#: for the host's configured hierarchy -- see the module docstring above for why this
#: is frozen rather than imported.
BILLING_ROOT_Q = Q(parent__isnull=True) | Q(can_invite_organizations=True)


class MissingSeedBillingPlanError(RuntimeError):
    """Frozen local copy of `vinta_billing.exceptions.MissingSeedBillingPlanError`'s
    message -- see the module docstring above for why this is not imported."""

    def __init__(self, slug: str):
        super().__init__(
            f"Required seed BillingPlan {slug!r} is missing. Check migration order "
            "and seed data before re-running."
        )


BATCH_SIZE = 500
UNLIMITED_PLAN_SLUG = "unlimited"

# Stamped onto every `Subscription` this migration creates so its reverse can
# delete exactly those rows and no others — see `delete_backfilled_subscriptions`.
BACKFILL_META_KEY = "backfilled_by"
BACKFILL_META_VALUE = "payments.0009"


def backfill_unlimited_subscriptions(apps, schema_editor):
    Organization = apps.get_model("organizations", "Organization")
    BillingPlan = apps.get_model("payments", "BillingPlan")
    Subscription = apps.get_model("payments", "Subscription")
    SubscriptionPlanLimit = apps.get_model("payments", "SubscriptionPlanLimit")
    SubscriptionEntitlement = apps.get_model("payments", "SubscriptionEntitlement")
    PlanLimit = apps.get_model("payments", "PlanLimit")
    PlanEntitlement = apps.get_model("payments", "PlanEntitlement")

    try:
        unlimited_plan = BillingPlan.objects.get(slug=UNLIMITED_PLAN_SLUG)
    except BillingPlan.DoesNotExist as exc:
        # The seed migration (0007) should already have created this. A
        # missing seed plan means a corrupted or out-of-order deploy: every
        # organization would otherwise stay plan-less permanently with no signal
        # and no re-run path (the reverse is not a delete). Fail loudly instead.
        raise MissingSeedBillingPlanError(UNLIMITED_PLAN_SLUG) from exc

    plan_limits = list(PlanLimit.objects.filter(plan=unlimited_plan))
    plan_entitlements = list(PlanEntitlement.objects.filter(plan=unlimited_plan))

    now = timezone.now()
    period_end = now + datetime.timedelta(days=30)

    last_pk = 0
    while True:
        batch_ids = list(
            Organization.objects.filter(
                BILLING_ROOT_Q,
                subscription__isnull=True,
                pk__gt=last_pk,
            )
            .order_by("pk")
            .values_list("pk", flat=True)[:BATCH_SIZE]
        )
        if not batch_ids:
            break
        last_pk = batch_ids[-1]

        subscriptions = Subscription.objects.bulk_create(
            [
                Subscription(
                    organization_id=org_id,
                    plan=unlimited_plan,
                    status="pending_send",
                    billing_state="free",
                    billing_interval="monthly",
                    current_period_start=now,
                    current_period_end=period_end,
                    # Placeholder: `unlimited` is $0 and never touches a gateway.
                    # Mirrors the same placeholder in
                    # SubscriptionService.create_subscription_for_organization.
                    payment_provider="mercadopago",
                    meta={BACKFILL_META_KEY: BACKFILL_META_VALUE},
                )
                for org_id in batch_ids
            ]
        )

        limit_rows = [
            SubscriptionPlanLimit(
                subscription=subscription,
                resource_key=plan_limit.resource_key,
                limit_value=plan_limit.limit_value,
                kind=plan_limit.kind,
                overage_unit_price=plan_limit.overage_unit_price,
                is_overridden=False,
            )
            for subscription in subscriptions
            for plan_limit in plan_limits
        ]
        SubscriptionPlanLimit.objects.bulk_create(limit_rows)

        entitlement_rows = [
            SubscriptionEntitlement(
                subscription=subscription,
                entitlement_key=plan_entitlement.entitlement_key,
                is_enabled=plan_entitlement.is_enabled,
                is_overridden=False,
            )
            for subscription in subscriptions
            for plan_entitlement in plan_entitlements
        ]
        SubscriptionEntitlement.objects.bulk_create(entitlement_rows)


def delete_backfilled_subscriptions(apps, schema_editor):
    """Reverse: delete only the `Subscription` rows this migration created,
    identified by the `meta.backfilled_by` stamp — not every `unlimited`
    subscription.

    Organizations may have been legitimately moved (back) onto `unlimited` via
    `SubscriptionService.change_plan` — the documented support rollback — for
    reasons unrelated to this migration; deleting every `unlimited` subscription
    on reverse would destroy that legitimate state along with the backfilled one.
    The `meta` stamp is what makes the two distinguishable.

    This is also what keeps `payments.0007`'s reverse (which deletes the seeded
    `BillingPlan` rows) from raising `ProtectedError`: `Subscription.plan` is
    `on_delete=PROTECT`, so any `Subscription` still referencing `unlimited` or
    `free` blocks that delete. Reversing the full chain (`0009` before `0007`)
    clears exactly the rows this migration is responsible for first.
    `SubscriptionPlanLimit` / `SubscriptionEntitlement` rows cascade-delete with
    their `Subscription`.
    """
    Subscription = apps.get_model("payments", "Subscription")
    Subscription.objects.filter(**{f"meta__{BACKFILL_META_KEY}": BACKFILL_META_VALUE}).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0008_subscriptionentitlement_subscriptionplanlimit"),
        ("organizations", "0016_organizationmembership_is_billing_owner"),
    ]

    operations = [
        migrations.RunPython(
            backfill_unlimited_subscriptions, reverse_code=delete_backfilled_subscriptions
        ),
    ]
