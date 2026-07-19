"""Verifies the `0006_seed_billing_plans` data migration's end state.

The test DB is fully migrated before any test runs (pytest-django), so asserting
against `BillingPlan.objects` here is asserting against what the seed migration
actually produced — not a factory standing in for it. `LimitedResource.values` is
enumerated dynamically (never a hardcoded list) so a limited resource added in a
later phase is caught here if its `unlimited` row goes missing, per the plan's
"no feature flag / unlimited is the rollback plan" guiding decision.
"""

import pytest

from payments.billing_constants import Entitlement, LimitedResource, LimitKind
from payments.models import BillingPlan


@pytest.mark.django_db
class TestPlanSeedMigration:
    def test_unlimited_plan_exists_and_is_default(self):
        plan = BillingPlan.objects.get(slug="unlimited")

        assert plan.is_active is True
        assert plan.is_default_for_new_organizations is True

    def test_unlimited_plan_has_a_null_limit_for_every_limited_resource(self):
        """The closing condition: `unlimited` must never be silently missing a
        `PlanLimit` row for any `LimitedResource` member, current or future."""
        plan = BillingPlan.objects.get(slug="unlimited")

        assert plan.limits.count() == len(LimitedResource.values)

        limits_by_resource = {limit.resource_key: limit for limit in plan.limits.all()}
        for resource_key in LimitedResource.values:
            assert resource_key in limits_by_resource, (
                f"unlimited plan is missing a PlanLimit row for {resource_key!r}"
            )
            assert limits_by_resource[resource_key].limit_value is None

    def test_unlimited_plan_has_every_entitlement_enabled(self):
        plan = BillingPlan.objects.get(slug="unlimited")

        assert plan.entitlements.count() == len(Entitlement.values)
        assert all(entitlement.is_enabled for entitlement in plan.entitlements.all())

    def test_unlimited_event_occurrences_limit_is_postpaid(self):
        """Kind still has to be correct on an unlimited row so later postpaid/prepaid
        branching does not have to special-case the unlimited plan."""
        plan = BillingPlan.objects.get(slug="unlimited")

        limit = plan.limits.get(resource_key=LimitedResource.EVENT_OCCURRENCES)
        assert limit.kind == LimitKind.POSTPAID

    def test_free_plan_exists_with_real_ceilings_and_is_not_default(self):
        plan = BillingPlan.objects.get(slug="free")

        assert plan.is_active is True
        assert plan.is_default_for_new_organizations is False
        assert plan.limits.count() == len(LimitedResource.values)
        assert all(limit.limit_value is not None for limit in plan.limits.all())

    def test_free_plan_event_occurrences_is_postpaid_with_an_allowance(self):
        plan = BillingPlan.objects.get(slug="free")

        limit = plan.limits.get(resource_key=LimitedResource.EVENT_OCCURRENCES)
        assert limit.kind == LimitKind.POSTPAID
        assert limit.limit_value is not None
        assert limit.overage_unit_price is not None

    def test_only_one_default_plan_across_the_seeded_catalog(self):
        assert BillingPlan.objects.filter(is_default_for_new_organizations=True).count() == 1
