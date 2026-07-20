"""Pooled limits across a reseller tree.

A reseller child holds no ``Subscription``; its usage is counted against its
billing root's single pooled ceiling, together with every other organization in
the subtree. Two things must hold and are easy to get wrong:

- The pool covers the *whole* subtree at any depth, not just direct children —
  otherwise a reseller sells one seat pool and hands out unlimited seats by
  nesting one level deeper.
- The pool **stops** at a nested billing root (``can_invite_organizations=True``),
  which pays for its own subtree. Folding it in would charge the ancestor for
  capacity it never sold, and double-count the same rows in two pools.

``parent`` is user-mutable through Django admin, so the traversal also has to
terminate on a cyclic chain rather than recursing forever.
"""

import datetime

from django.utils import timezone

import pytest
from model_bakery import baker

from organizations.models import Organization, OrganizationMembership
from payments.billing_constants import BillingState, LimitedResource, LimitKind
from payments.exceptions import BillingRootCycleError
from payments.models import BillingPlan, Subscription, SubscriptionPlanLimit
from payments.services.entitlement_service import EntitlementService


@pytest.fixture
def service():
    return EntitlementService()


@pytest.fixture
def plan():
    return baker.make(BillingPlan, is_default_for_new_organizations=False)


def make_subscription(organization, plan, member_limit):
    now = timezone.now()
    subscription = baker.make(
        Subscription,
        organization=organization,
        plan=plan,
        billing_state=BillingState.FREE,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )
    baker.make(
        SubscriptionPlanLimit,
        subscription=subscription,
        resource_key=LimitedResource.ORGANIZATION_MEMBERS,
        limit_value=member_limit,
        kind=LimitKind.PREPAID,
    )
    return subscription


def add_members(organization, count):
    baker.make(OrganizationMembership, organization=organization, is_active=True, _quantity=count)


@pytest.mark.django_db
class TestPooledUsage:
    def test_three_level_tree_sums_usage_across_all_descendants(self, service, plan):
        """root(1) -> child(2) -> grandchild(4) = 7 seats, all against root's pool."""
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        child = baker.make(Organization, parent=root, can_invite_organizations=False)
        grandchild = baker.make(Organization, parent=child, can_invite_organizations=False)
        make_subscription(root, plan, member_limit=10)

        add_members(root, 1)
        add_members(child, 2)
        add_members(grandchild, 4)

        for organization in (root, child, grandchild):
            assert (
                service.get_current_usage(organization, LimitedResource.ORGANIZATION_MEMBERS) == 7
            ), f"usage seen from {organization.pk} should be the whole pool"

    def test_child_resolves_to_the_roots_ceiling(self, service, plan):
        """A child holds no subscription of its own — its ceiling is the root's."""
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        child = baker.make(Organization, parent=root, can_invite_organizations=False)
        make_subscription(root, plan, member_limit=5)

        assert not Subscription.objects.filter(organization=child).exists()
        result = service.get_effective_limit(child, LimitedResource.ORGANIZATION_MEMBERS)
        assert result.limit_value == 5

    def test_two_children_over_the_pooled_ceiling_block_each_other(self, service, plan):
        """The phase's acceptance scenario, verbatim: a reseller root with two
        children each holding 3 members against a pooled limit of 5."""
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        child_a = baker.make(Organization, parent=root, can_invite_organizations=False)
        child_b = baker.make(Organization, parent=root, can_invite_organizations=False)
        make_subscription(root, plan, member_limit=5)
        add_members(child_a, 3)
        add_members(child_b, 3)

        result = service.check_limit(child_a, LimitedResource.ORGANIZATION_MEMBERS)

        assert result.allowed is False
        assert result.current_usage == 6
        assert result.ceiling == 5

    def test_pool_stops_at_a_nested_billing_root(self, service, plan):
        """A nested reseller pays for its own subtree, so its usage must not be
        counted against — nor limited by — its ancestor's pool."""
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        child = baker.make(Organization, parent=root, can_invite_organizations=False)
        nested_reseller = baker.make(Organization, parent=root, can_invite_organizations=True)
        nested_child = baker.make(
            Organization, parent=nested_reseller, can_invite_organizations=False
        )
        make_subscription(root, plan, member_limit=5)
        make_subscription(nested_reseller, plan, member_limit=2)

        add_members(root, 1)
        add_members(child, 1)
        add_members(nested_reseller, 1)
        add_members(nested_child, 1)

        assert service.get_current_usage(root, LimitedResource.ORGANIZATION_MEMBERS) == 2
        assert service.get_current_usage(nested_reseller, LimitedResource.ORGANIZATION_MEMBERS) == 2
        assert (
            service.get_effective_limit(
                nested_child, LimitedResource.ORGANIZATION_MEMBERS
            ).limit_value
            == 2
        )

    def test_sibling_subtree_usage_does_not_leak(self, service, plan):
        """Two unrelated roots keep separate pools."""
        root_a = baker.make(Organization, parent=None, can_invite_organizations=True)
        child_a = baker.make(Organization, parent=root_a, can_invite_organizations=False)
        root_b = baker.make(Organization, parent=None, can_invite_organizations=True)
        child_b = baker.make(Organization, parent=root_b, can_invite_organizations=False)
        make_subscription(root_a, plan, member_limit=5)
        make_subscription(root_b, plan, member_limit=5)

        add_members(child_a, 2)
        add_members(child_b, 4)

        assert service.get_current_usage(child_a, LimitedResource.ORGANIZATION_MEMBERS) == 2
        assert service.get_current_usage(child_b, LimitedResource.ORGANIZATION_MEMBERS) == 4

    def test_add_on_on_the_root_lifts_the_whole_subtree(self, service, plan):
        from payments.models import SubscriptionAddOn

        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        child = baker.make(Organization, parent=root, can_invite_organizations=False)
        subscription = make_subscription(root, plan, member_limit=5)
        add_members(child, 5)

        assert not service.check_limit(child, LimitedResource.ORGANIZATION_MEMBERS).allowed

        baker.make(
            SubscriptionAddOn,
            subscription=subscription,
            resource_key=LimitedResource.ORGANIZATION_MEMBERS,
            quantity=3,
            is_recurring=True,
            is_active=True,
        )

        result = service.check_limit(child, LimitedResource.ORGANIZATION_MEMBERS)
        assert result.allowed is True
        assert result.ceiling == 8


@pytest.mark.django_db
class TestCyclicParentChain:
    def test_cycle_in_the_ancestor_walk_raises_instead_of_recursing_forever(self, service, plan):
        """``a.parent = b`` and ``b.parent = a``, neither a billing root.

        Walking up never reaches a root. ``resolve_billing_root`` raises rather
        than returning an arbitrary cycle member, so the caller gets a loud,
        traceable failure instead of a silently wrong pool. The assertion that
        matters is that the call *returns at all*.
        """
        org_a = baker.make(Organization, parent=None, can_invite_organizations=False)
        org_b = baker.make(Organization, parent=org_a, can_invite_organizations=False)
        Organization.objects.filter(pk=org_a.pk).update(parent=org_b)
        org_a.refresh_from_db()

        with pytest.raises(BillingRootCycleError):
            service.check_limit(org_a, LimitedResource.ORGANIZATION_MEMBERS)

    def test_cycle_reachable_by_descent_terminates(self, service, plan):
        """The case a ``seen`` set on the *descent* is needed for.

        ``root`` is a billing root by virtue of ``can_invite_organizations=True``
        while its own ``parent`` points back into the cycle, so descending from it
        walks ``root -> child -> root``. Without the visited set this loops until
        the process dies; with it, the pool is the two organizations, counted once
        each.
        """
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        child = baker.make(Organization, parent=root, can_invite_organizations=False)
        Organization.objects.filter(pk=root.pk).update(parent=child)
        root.refresh_from_db()
        make_subscription(root, plan, member_limit=5)

        add_members(root, 1)
        add_members(child, 2)

        assert service.get_current_usage(root, LimitedResource.ORGANIZATION_MEMBERS) == 3
