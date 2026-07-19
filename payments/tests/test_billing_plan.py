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
