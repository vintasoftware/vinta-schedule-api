"""What the **live** plan catalog must satisfy, independent of what `0007` froze.

`payments/billing_plans_catalog.py` is head state: the root `conftest.py`'s post-flush
repair and `payments/tests/billing_fixtures.reseed_billing_plans` both seed from it,
and it is free to move the day product supplies the real `free` numbers. That freedom
is exactly why `payments/migrations/0007_seed_billing_plans.py` keeps its own frozen
copy — so the two are *not* pinned to each other here, and a divergence between them is
the intended outcome rather than a failure.

What is pinned is the rule the live seeder must never break, whatever the numbers
become: `SubscriptionService.assert_plan_is_complete` refuses a plan missing a
`PlanLimit` row for any `LimitedResource`, and `EntitlementService.get_effective_limit`
reads an absent row as *unlimited*. A fixture that reseeded an incomplete catalog would
therefore hand a resource an infinite ceiling rather than fail.

Every test below **drives `seed_billing_plans()` against an emptied table**, so it
asserts what the live seeder writes rather than what the migrated database happens to
contain — which is the migration's subject, covered in `test_plan_seed_migration.py`.
"""

import pytest

from payments.billing_constants import Entitlement, LimitedResource
from payments.billing_plans_catalog import (
    FREE_PLAN_SLUG,
    UNLIMITED_PLAN_SLUG,
    seed_billing_plans,
)
from payments.models import BillingPlan


@pytest.mark.django_db
class TestTheLiveSeederWritesACompleteCatalog:
    @pytest.fixture(autouse=True)
    def _seeded_by_the_live_catalog(self):
        BillingPlan.objects.all().delete()

        seed_billing_plans()

    def test_it_seeds_both_plans(self):
        assert set(BillingPlan.objects.values_list("slug", flat=True)) == {
            UNLIMITED_PLAN_SLUG,
            FREE_PLAN_SLUG,
        }

    def test_every_seeded_plan_covers_every_limited_resource(self):
        expected = set(LimitedResource.values)

        for plan in BillingPlan.objects.all():
            covered = set(plan.limits.values_list("resource_key", flat=True))

            assert expected <= covered, (
                f"BillingPlan {plan.slug!r} has no PlanLimit row for "
                f"{sorted(expected - covered)}. An omitted row reads as unlimited, so "
                "'not included' has to be limit_value=0 and never omission."
            )

    def test_every_seeded_plan_declares_every_entitlement(self):
        """Absent is not the same as disabled to a reader, but it is to this catalog:
        spelling every entitlement out is what keeps ``has_entitlement`` answering from
        data rather than from a default."""
        expected = set(Entitlement.values)

        for plan in BillingPlan.objects.all():
            declared = set(plan.entitlements.values_list("entitlement_key", flat=True))

            assert expected <= declared, (
                f"BillingPlan {plan.slug!r} declares no Entitlement row for "
                f"{sorted(expected - declared)}."
            )

    def test_exactly_one_plan_is_the_default_for_new_organizations(self):
        assert BillingPlan.objects.filter(is_default_for_new_organizations=True).count() == 1

    def test_seeding_twice_converges_instead_of_duplicating(self):
        plan = BillingPlan.objects.get(slug=UNLIMITED_PLAN_SLUG)
        plan.is_active = False
        plan.save(update_fields=["is_active"])

        seed_billing_plans()

        plan.refresh_from_db()
        assert plan.is_active is True
        assert BillingPlan.objects.filter(slug=UNLIMITED_PLAN_SLUG).count() == 1
