# Seed the plan catalog. There is no feature flag in this rollout — the `unlimited`
# plan *is* the kill switch. Every organization is placed on it so enforcement code
# can run everywhere from day one without being able to block anyone until an org is
# deliberately migrated onto a real plan. `free`'s limit values and entitlement
# grants are placeholders; product supplies the real numbers before any organization
# is actually moved onto it.
#
# Why the numbers below are literals rather than an import from
# `payments/billing_plans_catalog.py`. A data migration has to keep meaning what it
# meant when it was written (`AGENTS.md` on data migrations re-deriving their logic),
# and `free`'s ceilings are explicitly placeholders awaiting product: the first time
# somebody supplies the real numbers, an import here would retroactively change what
# this migration seeded on every fresh `migrate`. The live catalog is a *separate*
# module with a separate owner (`seed_billing_plans` there) -- it is free to move, this
# is not, and a divergence between the two is the intended outcome rather than a bug to
# be pinned shut. What the *current* catalog must satisfy is asserted against the live
# code in `payments/tests/test_plan_catalog.py`; what this migration wrote is asserted
# in `payments/tests/test_plan_seed_migration.py`, which drives this module.
#
# The resource / entitlement keys and the two `LimitKind` values below are frozen as
# plain string literals rather than imported, for the same reason and by the same
# rule this migration already applied to `payments.constants.PaymentProviders` in
# `payments.0009` / `payments.0017`: a migration that *writes* an enum member's value
# into a row has to stay correct even if the live source of that value is later
# renamed or restructured. `LimitedResource` / `Entitlement` have no package
# equivalent at all -- Phase 6 of
# `ai-plans/2026-08-19-MIGRATE_BILLING_ENGINE_TO_VINTA_DJANGO_BILLING_IMPLEMENTATION_PLAN.md`
# turned them into registrations against `vinta_billing.registry`
# (`payments/seams/resources.py`) -- so there is nothing importable to freeze against
# in the first place. `LimitKind` does exist in `vinta_billing.constants`, but its two
# members are written into every `PlanLimit` row this migration creates, which is data
# rather than field metadata (contrast `payments.migrations.0021`, whose
# `DocumentTypes.choices` reference is validation metadata Django never turns into
# DDL) -- so it is frozen here too, for internal consistency with the resource and
# entitlement keys it is paired with on every row.
from decimal import Decimal

from django.db import migrations


UNLIMITED_PLAN_SLUG = "unlimited"
FREE_PLAN_SLUG = "free"

#: `vinta_billing.constants.LimitKind`'s two stored values, frozen -- see the module
#: docstring above.
LIMIT_KIND_PREPAID = "prepaid"
LIMIT_KIND_POSTPAID = "postpaid"

#: The eight `LimitedResource` members' stored values, frozen -- see the module
#: docstring above. Order matches `payments/seams/resources.py`'s registration order.
RESOURCE_ORGANIZATION_MEMBERS = "organization_members"
RESOURCE_RESOURCE_CALENDARS = "resource_calendars"
RESOURCE_CALENDAR_GROUPS = "calendar_groups"
RESOURCE_BUNDLE_CALENDARS = "bundle_calendars"
RESOURCE_AVAILABILITY_WINDOWS = "availability_windows"
RESOURCE_WEBHOOK_SUBSCRIPTIONS = "webhook_subscriptions"
RESOURCE_PUBLIC_API_SYSTEM_USERS = "public_api_system_users"
RESOURCE_EVENT_OCCURRENCES = "event_occurrences"

RESOURCE_KEYS = (
    RESOURCE_ORGANIZATION_MEMBERS,
    RESOURCE_RESOURCE_CALENDARS,
    RESOURCE_CALENDAR_GROUPS,
    RESOURCE_BUNDLE_CALENDARS,
    RESOURCE_AVAILABILITY_WINDOWS,
    RESOURCE_WEBHOOK_SUBSCRIPTIONS,
    RESOURCE_PUBLIC_API_SYSTEM_USERS,
    RESOURCE_EVENT_OCCURRENCES,
)

#: The five `Entitlement` members' stored values, frozen -- see the module docstring
#: above.
ENTITLEMENT_EXTERNAL_CALENDAR_GOOGLE = "external_calendar_google"
ENTITLEMENT_EXTERNAL_CALENDAR_MICROSOFT = "external_calendar_microsoft"
ENTITLEMENT_PARTNER_API = "partner_api"
ENTITLEMENT_WHITE_LABEL_BRANDING = "white_label_branding"
ENTITLEMENT_ADVANCED_SCHEDULING = "advanced_scheduling"

ENTITLEMENT_KEYS = (
    ENTITLEMENT_EXTERNAL_CALENDAR_GOOGLE,
    ENTITLEMENT_EXTERNAL_CALENDAR_MICROSOFT,
    ENTITLEMENT_PARTNER_API,
    ENTITLEMENT_WHITE_LABEL_BRANDING,
    ENTITLEMENT_ADVANCED_SCHEDULING,
)

# Every LimitedResource member gets a NULL (no ceiling) row on `unlimited` — this is
# what makes it safe as the rollout switch. Kind still needs to be correct per
# resource so postpaid/prepaid branching does not have to special-case an unlimited
# plan.
POSTPAID_RESOURCES = {RESOURCE_EVENT_OCCURRENCES}

# Placeholder ceilings for the `free` plan. Real numbers come from product before any
# organization is actually rolled onto `free`.
FREE_PLAN_LIMITS: dict[str, dict] = {
    RESOURCE_ORGANIZATION_MEMBERS: {"limit_value": 5, "overage_unit_price": None},
    RESOURCE_RESOURCE_CALENDARS: {"limit_value": 3, "overage_unit_price": None},
    RESOURCE_CALENDAR_GROUPS: {"limit_value": 2, "overage_unit_price": None},
    RESOURCE_BUNDLE_CALENDARS: {"limit_value": 1, "overage_unit_price": None},
    RESOURCE_AVAILABILITY_WINDOWS: {"limit_value": 5, "overage_unit_price": None},
    RESOURCE_WEBHOOK_SUBSCRIPTIONS: {"limit_value": 1, "overage_unit_price": None},
    RESOURCE_PUBLIC_API_SYSTEM_USERS: {"limit_value": 0, "overage_unit_price": None},
    RESOURCE_EVENT_OCCURRENCES: {"limit_value": 50, "overage_unit_price": Decimal("0.0500")},
}

# Restricted on `free` by design: only the core Google-sync path is open. Real product
# entitlement grants come with the real limit numbers above.
FREE_PLAN_ENTITLEMENTS: dict[str, bool] = {
    ENTITLEMENT_EXTERNAL_CALENDAR_GOOGLE: True,
    ENTITLEMENT_EXTERNAL_CALENDAR_MICROSOFT: False,
    ENTITLEMENT_PARTNER_API: False,
    ENTITLEMENT_WHITE_LABEL_BRANDING: False,
    ENTITLEMENT_ADVANCED_SCHEDULING: False,
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
    for resource_key in RESOURCE_KEYS:
        PlanLimit.objects.update_or_create(
            plan=unlimited_plan,
            resource_key=resource_key,
            defaults={
                "limit_value": None,
                "kind": (
                    LIMIT_KIND_POSTPAID
                    if resource_key in POSTPAID_RESOURCES
                    else LIMIT_KIND_PREPAID
                ),
                "overage_unit_price": None,
            },
        )
    for entitlement_key in ENTITLEMENT_KEYS:
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
                    LIMIT_KIND_POSTPAID
                    if resource_key in POSTPAID_RESOURCES
                    else LIMIT_KIND_PREPAID
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

    Safe only if no `Subscription` still references these plans — true right after
    this migration runs, on its own, since organizations are not placed on a plan
    until migration `payments.0009` runs. From `payments.0009` onward,
    `Subscription.plan` is `on_delete=PROTECT`, so reversing the full chain to
    before this migration requires reversing `0009` first — its own reverse
    deletes exactly the `Subscription` rows *it* created (tagged
    `meta.backfilled_by`), which is what keeps this delete free of a
    `ProtectedError`. Reversing `0007` directly while any organically-created
    (non-backfilled) `Subscription` still references `unlimited` or `free` still
    raises `ProtectedError`, by design."""
    BillingPlan = apps.get_model("payments", "BillingPlan")
    BillingPlan.objects.filter(slug__in=[UNLIMITED_PLAN_SLUG, FREE_PLAN_SLUG]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0006_planentitlement_planlimit"),
    ]

    operations = [
        migrations.RunPython(seed_billing_plans, reverse_code=unseed_billing_plans),
    ]
