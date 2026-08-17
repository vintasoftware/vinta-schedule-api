"""The **live** billing plan catalog, and the seeder that writes it.

Head state, deliberately separate from
``payments/migrations/0007_seed_billing_plans.py``. That migration carries its own
frozen copy of these numbers and does not import this module: a data migration has to
keep meaning what it meant when it was written (``AGENTS.md`` on data migrations
re-deriving their logic), and ``free``'s ceilings below are explicitly *placeholders*
awaiting product. The day somebody supplies the real numbers, an import in ``0007``
would retroactively change what every fresh ``migrate`` seeds. Two functions, two
owners: this one is free to move, ``0007`` is not, and the two drifting apart is the
intended outcome rather than a bug.

Who reads this module:

* ``payments/tests/billing_fixtures.reseed_billing_plans`` and the root
  ``conftest.py``, to put the catalog back after a ``transaction=True`` test's flush
  destroyed the rows ``0007`` wrote (see that fixture for the ordering rules).
* Nothing in production. Production gets its catalog from ``0007`` and, from then on,
  from the admin.
"""

from decimal import Decimal
from typing import TypedDict

from payments.billing_constants import Entitlement, LimitedResource, LimitKind


UNLIMITED_PLAN_SLUG = "unlimited"
FREE_PLAN_SLUG = "free"

# Every LimitedResource member gets a NULL (no ceiling) row on `unlimited` — this is
# what makes it safe as the rollout switch. Kind still needs to be correct per
# resource so a later phase's postpaid/prepaid branching does not have to special-case
# an unlimited plan.
POSTPAID_RESOURCES = {LimitedResource.EVENT_OCCURRENCES}


class PlanSetting(TypedDict):
    limit_value: int
    overage_unit_price: Decimal | None


# Placeholder ceilings for the `free` plan. Real numbers come from product before any
# organization is actually rolled onto `free` (see the plan's Open Questions). Editing
# them here is safe and expected; `0007`'s copy stays where it is.
FREE_PLAN_LIMITS: dict[str, PlanSetting] = {
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


def seed_billing_plans() -> None:
    """Create (or converge) the seeded plans from **this module's live catalog**.

    ``update_or_create`` throughout, so it is idempotent and repairs a partially
    destroyed catalog rather than raising on the half that survived.

    Every ``LimitedResource`` member gets a ``PlanLimit`` row on every plan --
    ``SubscriptionService.assert_plan_is_complete`` refuses a plan that omits one,
    because an absent row reads as *unlimited* rather than as "not included".

    Runs against the live models on purpose: no historical-model injection seam, since
    no migration calls this. ``0007`` has its own copy.
    """
    from payments.models import BillingPlan, PlanEntitlement, PlanLimit

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
