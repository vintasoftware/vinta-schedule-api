"""``SubscriptionService`` — placing organizations on a plan and moving them
between plans.

The load-bearing behavior under test: plan change re-copies non-overridden
``SubscriptionPlanLimit`` / ``SubscriptionEntitlement`` rows and leaves
``is_overridden=True`` rows untouched, and catalog edits never propagate to an
already-sold subscription. See the plan's "Per-subscription limit copy" guiding
decision.
"""

import pytest
from model_bakery import baker

from organizations.models import Organization
from payments.billing_constants import BillingState, Entitlement, LimitedResource, LimitKind
from payments.models import BillingPlan, PlanEntitlement, PlanLimit, Subscription
from payments.services.subscription_service import SubscriptionService, resolve_billing_root


@pytest.fixture
def service():
    return SubscriptionService()


@pytest.fixture
def plan():
    catalog_plan = baker.make(BillingPlan, is_default_for_new_organizations=False)
    baker.make(
        PlanLimit,
        plan=catalog_plan,
        resource_key=LimitedResource.ORGANIZATION_MEMBERS,
        limit_value=5,
        kind=LimitKind.PREPAID,
    )
    baker.make(
        PlanEntitlement,
        plan=catalog_plan,
        entitlement_key=Entitlement.PARTNER_API,
        is_enabled=True,
    )
    return catalog_plan


@pytest.mark.django_db
class TestResolveBillingRoot:
    def test_standalone_organization_resolves_to_itself(self):
        org = baker.make(Organization, parent=None, can_invite_organizations=False)

        assert resolve_billing_root(org) == org

    def test_reseller_root_resolves_to_itself(self):
        org = baker.make(Organization, parent=None, can_invite_organizations=True)

        assert resolve_billing_root(org) == org

    def test_direct_child_resolves_to_reseller_root(self):
        root = baker.make(Organization, can_invite_organizations=True)
        child = baker.make(Organization, parent=root, can_invite_organizations=False)

        assert resolve_billing_root(child) == root

    def test_child_of_non_reseller_top_org_resolves_to_that_top_org(self):
        """A malformed tree (parent set, but no ancestor is ever flagged
        can_invite_organizations=True) still terminates at the topmost ancestor —
        which, being parent-less, is guaranteed to hold its own subscription."""
        top = baker.make(Organization, parent=None, can_invite_organizations=False)
        child = baker.make(Organization, parent=top, can_invite_organizations=False)

        assert resolve_billing_root(child) == top

    def test_cyclic_parent_chain_terminates(self):
        org_a = baker.make(Organization, can_invite_organizations=False)
        org_b = baker.make(Organization, parent=org_a, can_invite_organizations=False)
        org_a.parent = org_b
        org_a.save(update_fields=["parent"])

        result = resolve_billing_root(org_a)

        assert result.pk in (org_a.pk, org_b.pk)


@pytest.mark.django_db
class TestCreateSubscriptionForOrganization:
    def test_creates_subscription_with_default_plan_when_none_given(self, service):
        org = baker.make(Organization, parent=None)

        subscription = service.create_subscription_for_organization(org)

        assert subscription is not None
        assert subscription.organization == org
        assert subscription.plan.slug == "unlimited"
        assert subscription.billing_state == BillingState.FREE

    def test_copies_plan_limits_and_entitlements(self, service, plan):
        org = baker.make(Organization, parent=None)

        subscription = service.create_subscription_for_organization(org, plan=plan)

        limit = subscription.limits.get(resource_key=LimitedResource.ORGANIZATION_MEMBERS)
        assert limit.limit_value == 5
        assert limit.is_overridden is False

        entitlement = subscription.entitlements.get(entitlement_key=Entitlement.PARTNER_API)
        assert entitlement.is_enabled is True
        assert entitlement.is_overridden is False

    def test_reseller_child_gets_no_subscription(self, service, plan):
        root = baker.make(Organization, can_invite_organizations=True)
        child = baker.make(Organization, parent=root, can_invite_organizations=False)

        result = service.create_subscription_for_organization(child, plan=plan)

        assert result is None
        assert not Subscription.objects.filter(organization=child).exists()

    def test_idempotent_returns_existing_subscription(self, service, plan):
        org = baker.make(Organization, parent=None)

        first = service.create_subscription_for_organization(org, plan=plan)
        second = service.create_subscription_for_organization(org, plan=plan)

        assert first is not None
        assert second is not None
        assert first.pk == second.pk
        assert Subscription.objects.filter(organization=org).count() == 1


@pytest.mark.django_db
class TestChangePlan:
    def test_replans_non_overridden_limits_and_entitlements(self, service, plan):
        org = baker.make(Organization, parent=None)
        subscription = service.create_subscription_for_organization(org, plan=plan)

        new_plan = baker.make(BillingPlan, is_default_for_new_organizations=False)
        baker.make(
            PlanLimit,
            plan=new_plan,
            resource_key=LimitedResource.ORGANIZATION_MEMBERS,
            limit_value=20,
            kind=LimitKind.PREPAID,
        )
        baker.make(
            PlanEntitlement,
            plan=new_plan,
            entitlement_key=Entitlement.PARTNER_API,
            is_enabled=False,
        )

        service.change_plan(subscription, new_plan)
        subscription.refresh_from_db()

        assert subscription.plan == new_plan
        limit = subscription.limits.get(resource_key=LimitedResource.ORGANIZATION_MEMBERS)
        assert limit.limit_value == 20
        assert limit.is_overridden is False
        entitlement = subscription.entitlements.get(entitlement_key=Entitlement.PARTNER_API)
        assert entitlement.is_enabled is False

    def test_overridden_limit_survives_plan_change_untouched(self, service, plan):
        org = baker.make(Organization, parent=None)
        subscription = service.create_subscription_for_organization(org, plan=plan)

        overridden = subscription.limits.get(resource_key=LimitedResource.ORGANIZATION_MEMBERS)
        overridden.limit_value = 999
        overridden.is_overridden = True
        overridden.save()

        new_plan = baker.make(BillingPlan, is_default_for_new_organizations=False)
        baker.make(
            PlanLimit,
            plan=new_plan,
            resource_key=LimitedResource.ORGANIZATION_MEMBERS,
            limit_value=20,
            kind=LimitKind.PREPAID,
        )

        service.change_plan(subscription, new_plan)
        overridden.refresh_from_db()

        assert overridden.limit_value == 999
        assert overridden.is_overridden is True

    def test_overridden_entitlement_survives_plan_change_untouched(self, service, plan):
        org = baker.make(Organization, parent=None)
        subscription = service.create_subscription_for_organization(org, plan=plan)

        overridden = subscription.entitlements.get(entitlement_key=Entitlement.PARTNER_API)
        overridden.is_enabled = False
        overridden.is_overridden = True
        overridden.save()

        new_plan = baker.make(BillingPlan, is_default_for_new_organizations=False)
        baker.make(
            PlanEntitlement,
            plan=new_plan,
            entitlement_key=Entitlement.PARTNER_API,
            is_enabled=True,
        )

        service.change_plan(subscription, new_plan)
        overridden.refresh_from_db()

        assert overridden.is_enabled is False
        assert overridden.is_overridden is True

    def test_catalog_edit_does_not_propagate_to_existing_subscription(self, service, plan):
        """The catalog `PlanLimit` is the source used at copy time only. Editing it
        after a subscription already copied its rows must not change what the
        subscription enforces — an org keeps what it was sold."""
        org = baker.make(Organization, parent=None)
        subscription = service.create_subscription_for_organization(org, plan=plan)

        catalog_limit = plan.limits.get(resource_key=LimitedResource.ORGANIZATION_MEMBERS)
        catalog_limit.limit_value = 1
        catalog_limit.save()

        sub_limit = subscription.limits.get(resource_key=LimitedResource.ORGANIZATION_MEMBERS)
        assert sub_limit.limit_value == 5
