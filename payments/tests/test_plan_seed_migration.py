"""Verifies the `0007_seed_billing_plans` data migration's end state.

The test DB is fully migrated before any test runs (pytest-django), so asserting
against `BillingPlan.objects` here is asserting against what the seed migration
actually produced — not a factory standing in for it. `LimitedResource.values` is
enumerated dynamically (never a hardcoded list) so a limited resource added in a
later phase is caught here if its `unlimited` row goes missing, per the plan's
"no feature flag / unlimited is the rollback plan" guiding decision.
"""

import importlib

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

    def test_every_seeded_plan_covers_every_limited_resource(self):
        """The plan-completeness invariant, stated once for the whole catalog.

        `SubscriptionService._prune_stale_limits` leans on this: a plan carrying a
        row for every `LimitedResource` member can never leave a subscription in the
        absent-row state on a plan change, and absent-row is what
        `EntitlementService.get_effective_limit` reads as **unlimited**. Without
        this, adding a resource to `LimitedResource` and forgetting one plan would
        let a downgrade onto that plan hand the resource an infinite ceiling
        (BLOCKER 3, Phase 5 review).

        Enumerated over every seeded plan, not just `unlimited`, so a plan added to
        the catalog later is held to the same rule.
        """
        expected = set(LimitedResource.values)
        for plan in BillingPlan.objects.all():
            covered = set(plan.limits.values_list("resource_key", flat=True))
            assert expected <= covered, (
                f"BillingPlan {plan.slug!r} has no PlanLimit row for "
                f"{sorted(expected - covered)}. Every plan must carry a row for every "
                "LimitedResource member -- 'not included' is limit_value=0, never "
                "omission, because an omitted row reads as unlimited."
            )

    def test_only_one_default_plan_across_the_seeded_catalog(self):
        assert BillingPlan.objects.filter(is_default_for_new_organizations=True).count() == 1

    def test_seeding_converges_and_updates_existing_plans(self):
        """If a plan with slug `unlimited` already exists with wrong values (from a
        partial deploy, manual fix, or earlier test run), the seed migration must
        converge — updating the existing plan's fields to their canonical values.
        The same applies to PlanLimit and PlanEntitlement rows."""
        from django.apps import apps

        # Get the seeding function from the migration module
        migration_module = importlib.import_module("payments.migrations.0007_seed_billing_plans")
        seed_billing_plans = migration_module.seed_billing_plans

        plan = BillingPlan.objects.get(slug="unlimited")

        # Simulate a partial deploy or manual correction that left the plan
        # in an incorrect state.
        plan.is_default_for_new_organizations = False
        plan.is_active = False
        plan.save()

        # Also corrupt a PlanLimit row to verify it converges too.
        limit = plan.limits.get(resource_key=LimitedResource.EVENT_OCCURRENCES)
        limit.kind = LimitKind.PREPAID  # Should be POSTPAID
        limit.save()

        # Re-run the seeding function; it should converge to the canonical state.
        seed_billing_plans(apps, None)

        # Assert the plan was corrected.
        plan.refresh_from_db()
        assert plan.is_active is True
        assert plan.is_default_for_new_organizations is True

        # Assert the PlanLimit was corrected.
        limit.refresh_from_db()
        assert limit.kind == LimitKind.POSTPAID
