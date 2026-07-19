# Phase 4 of billing plans and limits: every organization always has exactly one
# active plan, from creation, with no plan-less state. This backfills every
# *pre-existing* organization onto `unlimited` — deliberately not `free`. `free`
# carries real ceilings; applying them to organizations that predate this feature
# would block them as a side effect of the rollout itself, which the plan's
# objective 6 ("no organization is blocked as a consequence of the rollout
# itself") sets at zero. `unlimited` is this plan's declared rollout switch (every
# `PlanLimit.limit_value` is NULL), so this migration changes no organization's
# observable behavior.
#
# Only organizations that are their own billing root (`parent__isnull=True`) get a
# `Subscription` here — a reseller child pools against its root's subscription
# instead (see `payments.services.subscription_service.resolve_billing_root`).
# Batched to avoid loading every organization into memory at once.
import datetime

from django.db import migrations
from django.utils import timezone


BATCH_SIZE = 500
UNLIMITED_PLAN_SLUG = "unlimited"


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
    except BillingPlan.DoesNotExist:
        # The Phase 3 seed migration (0006) should already have created this. Nothing
        # to backfill against if it is somehow missing — fail loudly is the safer
        # choice for a `RunPython`, but this migration must still be re-runnable, so
        # just no-op rather than crash a deploy that ran migrations out of order.
        return

    plan_limits = list(PlanLimit.objects.filter(plan=unlimited_plan))
    plan_entitlements = list(PlanEntitlement.objects.filter(plan=unlimited_plan))

    now = timezone.now()
    period_end = now + datetime.timedelta(days=30)

    org_ids = list(
        Organization.objects.filter(parent__isnull=True, subscription__isnull=True)
        .order_by("pk")
        .values_list("pk", flat=True)
        .iterator(chunk_size=BATCH_SIZE)
    )

    for start in range(0, len(org_ids), BATCH_SIZE):
        batch_ids = org_ids[start : start + BATCH_SIZE]
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
                    payment_provider="mercadopago",
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


def noop_reverse(apps, schema_editor):
    """Intentionally a no-op, not a delete.

    Unlike the Phase 3 seed migration's reverse (safe to delete because nothing
    referenced the seeded rows yet), this migration's whole purpose is to make
    `Subscription` rows exist that other data immediately starts depending on. By
    the time this migration might be reversed, organizations may have been
    deliberately moved back onto `unlimited` as the documented support rollback
    ("change_plan back to unlimited, no deploy") for reasons unrelated to this
    migration — deleting every `unlimited` subscription on reverse would destroy
    that legitimate state along with the backfilled one, with no way to tell them
    apart. Reversing this rollout is an operational decision (per-organization
    `change_plan`), not a schema reverse.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0007_subscriptionentitlement_subscriptionplanlimit"),
        ("organizations", "0016_organizationmembership_is_billing_owner"),
    ]

    operations = [
        migrations.RunPython(backfill_unlimited_subscriptions, reverse_code=noop_reverse),
    ]
