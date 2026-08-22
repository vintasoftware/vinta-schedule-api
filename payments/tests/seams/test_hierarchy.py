"""``payments.seams.hierarchy.ResellerHierarchy`` -- the generic equivalent of
``payments.services.subscription_service.is_billing_root`` /
``resolve_billing_root``, wired against ``organizations.Organization``'s
``parent`` / ``can_invite_organizations`` fields.
"""

import pytest
from model_bakery import baker
from vinta_billing.exceptions import BillingRootCycleError

from organizations.models import Organization
from payments.seams.hierarchy import ResellerHierarchy


@pytest.fixture
def hierarchy() -> ResellerHierarchy:
    return ResellerHierarchy()


@pytest.mark.django_db
class TestIsBillingRoot:
    def test_a_parentless_organization_is_a_root(self, hierarchy):
        organization = baker.make(Organization, parent=None, can_invite_organizations=False)

        assert hierarchy.is_billing_root(organization) is True

    def test_a_child_flagged_can_invite_organizations_is_its_own_root(self, hierarchy):
        parent = baker.make(Organization, parent=None, can_invite_organizations=False)
        child = baker.make(Organization, parent=parent, can_invite_organizations=True)

        assert hierarchy.is_billing_root(child) is True

    def test_a_plain_child_is_not_a_root(self, hierarchy):
        parent = baker.make(Organization, parent=None, can_invite_organizations=False)
        child = baker.make(Organization, parent=parent, can_invite_organizations=False)

        assert hierarchy.is_billing_root(child) is False


@pytest.mark.django_db
class TestResolveBillingRoot:
    def test_a_parentless_organization_resolves_to_itself(self, hierarchy):
        organization = baker.make(Organization, parent=None, can_invite_organizations=False)

        assert hierarchy.resolve_billing_root(organization) == organization

    def test_a_reseller_child_resolves_to_itself(self, hierarchy):
        """A reseller pays for its own subtree, not its parent's -- it must not
        resolve past itself even though it has a parent."""
        parent = baker.make(Organization, parent=None, can_invite_organizations=False)
        reseller_child = baker.make(Organization, parent=parent, can_invite_organizations=True)

        assert hierarchy.resolve_billing_root(reseller_child) == reseller_child

    def test_a_plain_child_resolves_to_its_reseller(self, hierarchy):
        reseller = baker.make(Organization, parent=None, can_invite_organizations=True)
        child = baker.make(Organization, parent=reseller, can_invite_organizations=False)

        assert hierarchy.resolve_billing_root(child) == reseller

    def test_a_grandchild_resolves_to_the_nearest_reseller_ancestor(self, hierarchy):
        """The nearest ancestor wins, not the top of the whole chain -- a nested
        reseller pays for its own subtree."""
        top = baker.make(Organization, parent=None, can_invite_organizations=True)
        nested_reseller = baker.make(Organization, parent=top, can_invite_organizations=True)
        grandchild = baker.make(
            Organization, parent=nested_reseller, can_invite_organizations=False
        )

        assert hierarchy.resolve_billing_root(grandchild) == nested_reseller

    def test_a_parent_cycle_raises_instead_of_hanging(self, hierarchy):
        """``parent`` is user-mutable (Django admin), so a cycle is reachable in
        practice. ``resolve_billing_root`` must raise, not loop forever."""
        organization_a = baker.make(Organization, parent=None, can_invite_organizations=False)
        organization_b = baker.make(
            Organization, parent=organization_a, can_invite_organizations=False
        )
        # Close the cycle: `a`'s parent becomes `b`, so `a -> b -> a -> ...` never
        # reaches a parentless (or reseller-flagged) organization.
        organization_a.parent = organization_b
        organization_a.save(update_fields=["parent"])

        with pytest.raises(BillingRootCycleError):
            hierarchy.resolve_billing_root(organization_a)


@pytest.mark.django_db
class TestPooledOrganizationIds:
    def test_includes_the_root_and_its_descendants(self, hierarchy):
        root = baker.make(Organization, parent=None, can_invite_organizations=False)
        child = baker.make(Organization, parent=root, can_invite_organizations=False)
        grandchild = baker.make(Organization, parent=child, can_invite_organizations=False)

        pooled = set(hierarchy.pooled_organization_ids(root))

        assert pooled == {root.pk, child.pk, grandchild.pk}

    def test_stops_at_a_nested_billing_root(self, hierarchy):
        """A nested reseller -- and its own descendants -- pays for its own
        subtree, not the ancestor's. It is excluded from the ancestor's pool
        entirely (not merely from further descent): folding it, or its
        children, into the ancestor's ceiling would double-charge them.
        Matches ``payments.services.subscription_service
        ._get_pooled_organization_ids``'s identical ``continue`` on a nested
        root, which this seam replaces.
        """
        root = baker.make(Organization, parent=None, can_invite_organizations=False)
        nested_reseller = baker.make(Organization, parent=root, can_invite_organizations=True)
        nested_grandchild = baker.make(
            Organization, parent=nested_reseller, can_invite_organizations=False
        )

        pooled = set(hierarchy.pooled_organization_ids(root))

        assert pooled == {root.pk}
        assert nested_reseller.pk not in pooled
        assert nested_grandchild.pk not in pooled
