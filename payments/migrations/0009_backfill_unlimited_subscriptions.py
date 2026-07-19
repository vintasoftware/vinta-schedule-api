# Phase 4 of billing plans and limits: every organization always has exactly one
# active plan, from creation, with no plan-less state. This backfills every
# *pre-existing* billing-root organization onto `unlimited` — deliberately not
# `free`. `free` carries real ceilings; applying them to organizations that
# predate this feature would block them as a side effect of the rollout itself,
# which the plan's objective 6 ("no organization is blocked as a consequence of
# the rollout itself") sets at zero. `unlimited` is this plan's declared rollout
# switch (every `PlanLimit.limit_value` is NULL), so this migration changes no
# organization's observable behavior.
#
# A billing root is an organization with no parent, OR an organization that can
# itself invite/create other organizations (a nested reseller is its own billing
# root, not a child pooling against a grandparent's subscription) — see
# `billing_root_filter` / `is_billing_root` in
# `payments.services.subscription_service`, the single predicate for this
# decision used here, in `SubscriptionService.create_subscription_for_organization`,
# and in the "no plan-less state" acceptance query. Importing it here (rather than
# re-deriving the condition against the historical `apps.get_model` models) is a
# deliberate deviation from the usual migration-isolation convention: the
# function only does attribute/field-name access, so it works unchanged against
# both the historical and the current `Organization` model, and a single
# predicate is safer than keeping two copies of "is a billing root" in sync by
# hand.
#
# Keyset-paginated on `pk` (not an in-memory id list) so this never materializes
# more than one batch of organizations at a time, regardless of table size.
import datetime

from django.db import migrations
from django.utils import timezone

from payments.exceptions import MissingSeedBillingPlanError
from payments.services.subscription_service import billing_root_filter


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
        # The Phase 3 seed migration (0007) should already have created this. A
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
                billing_root_filter(),
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
