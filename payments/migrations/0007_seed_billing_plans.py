# Phase 3 of billing plans and limits: seed the plan catalog. There is no feature
# flag in this rollout — the `unlimited` plan *is* the kill switch. Every organization
# is placed on it (a later phase) so enforcement code can run everywhere from day one
# without being able to block anyone until an org is deliberately migrated onto a real
# plan. `free`'s limit values and entitlement grants are placeholders; product
# supplies the real numbers before any organization is actually moved onto it
# (deferred, tracked as its own phase).
from decimal import Decimal

from django.db import migrations

from payments.billing_constants import Entitlement, LimitedResource, LimitKind


UNLIMITED_PLAN_SLUG = "unlimited"
FREE_PLAN_SLUG = "free"

# Every LimitedResource member gets a NULL (no ceiling) row on `unlimited` — this is
# what makes it safe as the rollout switch. Kind still needs to be correct per
# resource so a later phase's postpaid/prepaid branching does not have to special-case
# an unlimited plan.
POSTPAID_RESOURCES = {LimitedResource.EVENT_OCCURRENCES}

# Placeholder ceilings for the `free` plan. Real numbers come from product before any
# organization is actually rolled onto `free` (see the plan's Open Questions).
FREE_PLAN_LIMITS: dict[str, dict] = {
    LimitedResource.ORGANIZATION_MEMBERS: {"limit_value": 5, "overage_unit_price": None},
    LimitedResource.RESOURCE_CALENDARS: {"limit_value": 3, "overage_unit_price": None},
    LimitedResource.CALENDAR_GROUPS: {"limit_value": 2, "overage_unit_price": None},
    LimitedResource.BUNDLE_CALENDARS: {"limit_value": 1, "overage_unit_price": None},
    LimitedResource.AVAILABILITY_WINDOWS: {"limit_value": 5, "overage_unit_price": None},
    LimitedResource.WEBHOOK_SUBSCRIPTIONS: {"limit_value": 1, "overage_unit_price": None},
    LimitedResource.PUBLIC_API_SYSTEM_USERS: {"limit_value": 0, "overage_unit_price": None},
    LimitedResource.EVENT_OCCURRENCES: {"limit_value": 50, "overage_unit_price": Decimal("0.0500")},
}

# Restricted on `free` by design: only the core Google-sync path is open. Real product
# entitlement grants come with the real limit numbers above.
FREE_PLAN_ENTITLEMENTS: dict[str, bool] = {
    Entitlement.EXTERNAL_CALENDAR_GOOGLE: True,
    Entitlement.EXTERNAL_CALENDAR_MICROSOFT: False,
    Entitlement.PARTNER_API: False,
    Entitlement.WHITE_LABEL_BRANDING: False,
    Entitlement.ADVANCED_SCHEDULING: False,
}


def seed_billing_plans(apps, schema_editor):
    BillingPlan = apps.get_model("payments", "BillingPlan")
    PlanLimit = apps.get_model("payments", "PlanLimit")
    PlanEntitlement = apps.get_model("payments", "PlanEntitlement")

    unlimited_plan, _created = BillingPlan.objects.update_or_create(
        slug=UNLIMITED_PLAN_SLUG,
        defaults={
            "name": "Unlimited",
            "is_active": True,
            "is_default_for_new_organizations": True,
            "monthly_price": Decimal("0"),
            "annual_price": None,
            "currency": "USD",
            "grace_period_days": None,
        },
    )
    for resource_key in LimitedResource.values:
        PlanLimit.objects.update_or_create(
            plan=unlimited_plan,
            resource_key=resource_key,
            defaults={
                "limit_value": None,
                "kind": (
                    LimitKind.POSTPAID if resource_key in POSTPAID_RESOURCES else LimitKind.PREPAID
                ),
                "overage_unit_price": None,
            },
        )
    for entitlement_key in Entitlement.values:
        PlanEntitlement.objects.update_or_create(
            plan=unlimited_plan,
            entitlement_key=entitlement_key,
            defaults={"is_enabled": True},
        )

    free_plan, _created = BillingPlan.objects.update_or_create(
        slug=FREE_PLAN_SLUG,
        defaults={
            "name": "Free",
            "is_active": True,
            "is_default_for_new_organizations": False,
            "monthly_price": Decimal("0"),
            "annual_price": None,
            "currency": "USD",
            "grace_period_days": None,
        },
    )
    for resource_key, values in FREE_PLAN_LIMITS.items():
        PlanLimit.objects.update_or_create(
            plan=free_plan,
            resource_key=resource_key,
            defaults={
                "limit_value": values["limit_value"],
                "kind": (
                    LimitKind.POSTPAID if resource_key in POSTPAID_RESOURCES else LimitKind.PREPAID
                ),
                "overage_unit_price": values["overage_unit_price"],
            },
        )
    for entitlement_key, is_enabled in FREE_PLAN_ENTITLEMENTS.items():
        PlanEntitlement.objects.update_or_create(
            plan=free_plan,
            entitlement_key=entitlement_key,
            defaults={"is_enabled": is_enabled},
        )


def unseed_billing_plans(apps, schema_editor):
    """Reverse: delete the two seeded plans (and, via CASCADE, their limits and
    entitlements).

    Safe only if no `Subscription` still references these plans — true at this
    phase (3) on its own, since organizations are not placed on a plan until
    Phase 4. From Phase 4 (`payments.0008`) onward, `Subscription.plan` is
    `on_delete=PROTECT`, so reversing the full chain to before this migration
    requires reversing `0008` first — its own reverse deletes exactly the
    `Subscription` rows *it* created (tagged `meta.backfilled_by`), which is what
    keeps this delete free of a `ProtectedError`. Reversing `0006` directly while
    any organically-created (non-backfilled) `Subscription` still references
    `unlimited` or `free` still raises `ProtectedError`, by design."""
    BillingPlan = apps.get_model("payments", "BillingPlan")
    BillingPlan.objects.filter(slug__in=[UNLIMITED_PLAN_SLUG, FREE_PLAN_SLUG]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0006_planentitlement_planlimit"),
    ]

    operations = [
        migrations.RunPython(seed_billing_plans, reverse_code=unseed_billing_plans),
    ]
