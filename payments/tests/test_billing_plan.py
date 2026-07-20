from django.core.exceptions import ValidationError
from django.db import IntegrityError

import pytest
from model_bakery import baker

from payments.billing_constants import Entitlement, LimitedResource, LimitKind
from payments.models import BillingPlan, PlanEntitlement, PlanLimit


@pytest.fixture
def billing_plan():
    return baker.make(BillingPlan)


@pytest.mark.django_db
class TestPlanLimit:
    def test_rejects_duplicate_resource_key_per_plan(self, billing_plan):
        """The `uniq_plan_limit_resource` constraint enforces at most one
        `PlanLimit` row per `(plan, resource_key)` pair."""
        baker.make(
            PlanLimit,
            plan=billing_plan,
            resource_key=LimitedResource.ORGANIZATION_MEMBERS,
            kind=LimitKind.PREPAID,
        )

        with pytest.raises(IntegrityError):
            baker.make(
                PlanLimit,
                plan=billing_plan,
                resource_key=LimitedResource.ORGANIZATION_MEMBERS,
                kind=LimitKind.PREPAID,
            )

    def test_same_resource_key_allowed_on_different_plans(self):
        """The constraint is scoped to the plan, not global on `resource_key`."""
        plan_a = baker.make(BillingPlan)
        plan_b = baker.make(BillingPlan)

        baker.make(
            PlanLimit,
            plan=plan_a,
            resource_key=LimitedResource.ORGANIZATION_MEMBERS,
            kind=LimitKind.PREPAID,
        )
        baker.make(
            PlanLimit,
            plan=plan_b,
            resource_key=LimitedResource.ORGANIZATION_MEMBERS,
            kind=LimitKind.PREPAID,
        )

        assert (
            PlanLimit.objects.filter(
                plan__in=[plan_a, plan_b], resource_key=LimitedResource.ORGANIZATION_MEMBERS
            ).count()
            == 2
        )

    def test_null_limit_value_means_unlimited(self, billing_plan):
        limit = baker.make(
            PlanLimit,
            plan=billing_plan,
            resource_key=LimitedResource.EVENT_OCCURRENCES,
            kind=LimitKind.POSTPAID,
            limit_value=None,
        )

        assert limit.limit_value is None


@pytest.mark.django_db
class TestBillingPlanLimitCoverage:
    """Plan completeness as a model-level invariant.

    It used to be asserted only over *seed data*, which cannot see a plan an admin
    authors at runtime — and an incomplete plan is what turns a downgrade into an
    infinite ceiling (BLOCKER 3, Phase 5 review).
    """

    def test_clean_rejects_a_plan_missing_a_limited_resource(self, billing_plan):
        baker.make(
            PlanLimit,
            plan=billing_plan,
            resource_key=LimitedResource.ORGANIZATION_MEMBERS,
            limit_value=5,
            kind=LimitKind.PREPAID,
        )

        with pytest.raises(ValidationError) as exc_info:
            billing_plan.full_clean()

        message = str(exc_info.value)
        assert LimitedResource.RESOURCE_CALENDARS in message

    def test_clean_accepts_a_plan_covering_every_limited_resource(self, billing_plan):
        for resource_key in LimitedResource.values:
            baker.make(
                PlanLimit,
                plan=billing_plan,
                resource_key=resource_key,
                limit_value=0,
                kind=LimitKind.PREPAID,
            )

        billing_plan.full_clean()

    def test_missing_keys_are_reported_for_an_unsaved_plan(self):
        """An unsaved plan has no rows to read, so it is missing everything —
        rather than raising on the related manager or reporting a vacuous pass."""
        assert BillingPlan().get_missing_limited_resource_keys() == sorted(LimitedResource.values)


@pytest.mark.django_db
class TestPlanEntitlement:
    def test_rejects_duplicate_entitlement_key_per_plan(self, billing_plan):
        baker.make(
            PlanEntitlement,
            plan=billing_plan,
            entitlement_key=Entitlement.PARTNER_API,
        )

        with pytest.raises(IntegrityError):
            baker.make(
                PlanEntitlement,
                plan=billing_plan,
                entitlement_key=Entitlement.PARTNER_API,
            )
