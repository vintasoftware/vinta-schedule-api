"""Verifies the `0007_seed_billing_plans` data migration's end state.

The test DB is fully migrated before any test runs (pytest-django), so asserting
against `BillingPlan.objects` here is asserting against what the seed migration
actually produced — not a factory standing in for it. `RESOURCE_KEYS` is
enumerated dynamically (never a hardcoded list) so a limited resource added later is
caught here if its `unlimited` row goes missing. There is no feature flag: keeping
every organization on the `unlimited` plan is the rollback path.

That first sentence is only true while nothing reseeds the catalog underneath these
tests, which is what `no_billing_catalog_reseed` below is for: the root `conftest.py`
repairs the catalog from live code after a transactional test flushes it, and a repair
that ran here would recreate exactly what the migration seeds. Every assertion in this
module would then pass with `0007`'s `RunPython` gutted — which is how it stood before
the marker existed. The trade is deliberate: with the opt-out these tests would rather
go red on a flushed database than go green on a synthetic one.
"""

import pytest
from vinta_billing.constants import LimitKind
from vinta_billing.models import BillingPlan

from payments.seams.resource_keys import (
    ENTITLEMENT_KEYS,
    EVENT_OCCURRENCES,
    ORGANIZATION_MEMBERS,
    RESOURCE_KEYS,
)


@pytest.mark.no_billing_catalog_reseed
@pytest.mark.django_db
class TestPlanSeedMigration:
    def test_unlimited_plan_exists_and_is_default(self):
        plan = BillingPlan.objects.get(slug="unlimited")

        assert plan.is_active is True
        assert plan.is_default_for_new_scopes is True

    def test_unlimited_plan_has_a_null_limit_for_every_limited_resource(self):
        """The closing condition: `unlimited` must never be silently missing a
        `PlanLimit` row for any `LimitedResource` member, current or future."""
        plan = BillingPlan.objects.get(slug="unlimited")

        assert plan.limits.count() == len(RESOURCE_KEYS)

        limits_by_resource = {limit.resource_key: limit for limit in plan.limits.all()}
        for resource_key in RESOURCE_KEYS:
            assert resource_key in limits_by_resource, (
                f"unlimited plan is missing a PlanLimit row for {resource_key!r}"
            )
            assert limits_by_resource[resource_key].limit_value is None

    def test_unlimited_plan_has_every_entitlement_enabled(self):
        plan = BillingPlan.objects.get(slug="unlimited")

        assert set(plan.entitlements.values_list("entitlement_key", flat=True)) == set(
            ENTITLEMENT_KEYS
        )
        assert all(entitlement.is_enabled for entitlement in plan.entitlements.all())

    def test_unlimited_event_occurrences_limit_is_postpaid(self):
        """Kind still has to be correct on an unlimited row so later postpaid/prepaid
        branching does not have to special-case the unlimited plan."""
        plan = BillingPlan.objects.get(slug="unlimited")

        limit = plan.limits.get(resource_key=EVENT_OCCURRENCES)
        assert limit.kind == LimitKind.POSTPAID

    def test_unlimited_organization_members_limit_is_prepaid(self):
        """`payments.0007` freezes `LimitKind.PREPAID`'s stored value as a plain
        literal (`LIMIT_KIND_PREPAID`) for the seven non-event-occurrences
        resources. The `POSTPAID` side is pinned against a DB row above; this is
        the equivalent DB-row pin for the `PREPAID` side."""
        plan = BillingPlan.objects.get(slug="unlimited")

        limit = plan.limits.get(resource_key=ORGANIZATION_MEMBERS)
        assert limit.kind == LimitKind.PREPAID

    def test_free_plan_exists_with_real_ceilings_and_is_not_default(self):
        plan = BillingPlan.objects.get(slug="free")

        assert plan.is_active is True
        assert plan.is_default_for_new_scopes is False
        assert plan.limits.count() == len(RESOURCE_KEYS)
        assert all(limit.limit_value is not None for limit in plan.limits.all())

    def test_free_plan_event_occurrences_is_postpaid_with_an_allowance(self):
        plan = BillingPlan.objects.get(slug="free")

        limit = plan.limits.get(resource_key=EVENT_OCCURRENCES)
        assert limit.kind == LimitKind.POSTPAID
        assert limit.limit_value is not None
        assert limit.overage_unit_price is not None

    def test_every_seeded_plan_covers_every_limited_resource(self):
        """The plan-completeness rule, stated once for the whole catalog.

        `SubscriptionService.assert_plan_is_complete` enforces this at runtime for
        *any* plan a subscription is placed on, including one authored through the
        admin. This test is the seed-data half: it proves no shipped plan trips that
        check, so nobody is refused a plan change on catalog data we control.

        The rule itself: a plan carrying a row for every `LimitedResource` member can
        never leave a subscription in the absent-row state on a plan change, and
        absent-row (like a stale `limit_value=None`) is what
        `EntitlementService.get_effective_limit` reads as **unlimited**. Without it,
        adding a resource to `LimitedResource` and forgetting one plan would let a
        downgrade onto that plan hand the resource an infinite ceiling.

        Enumerated over every seeded plan, not just `unlimited`, so a plan added to
        the catalog later is held to the same rule.
        """
        assert BillingPlan.objects.exists(), (
            "No seeded plans at all — without this guard the loop below passes "
            "vacuously and asserts nothing."
        )
        expected = set(RESOURCE_KEYS)
        for plan in BillingPlan.objects.all():
            covered = set(plan.limits.values_list("resource_key", flat=True))
            assert expected <= covered, (
                f"BillingPlan {plan.slug!r} has no PlanLimit row for "
                f"{sorted(expected - covered)}. Every plan must carry a row for every "
                "LimitedResource member -- 'not included' is limit_value=0, never "
                "omission, because an omitted row reads as unlimited."
            )

    def test_only_one_default_plan_across_the_seeded_catalog(self):
        assert BillingPlan.objects.filter(is_default_for_new_scopes=True).count() == 1
