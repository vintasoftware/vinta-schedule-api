"""Low-level regression coverage for the reseller-root billing policy branch.

``IsBillingOwnerOrAdmin`` has a direct organization check and an acting-
reseller-root subtree branch. The latter names the membership organization
explicitly rather than asking the backend from ambient context.

``vinta_orgs.auth_backends.OrganizationModelBackend`` resolves a bare
``user.has_perm("payments.manage_billing")`` only for the organization bound to
the current context. The package header resolver currently always binds that
same organization as the resolved membership, so no endpoint request can make
the subtree branch decisive. The tests preserve the branch's direct, low-level
policy behavior without asserting a request path that does not exist.

Phase 4 keeps this branch hand-written (the plan's "Four rules stay
hand-written" Guiding Decision) and names the organization explicitly through
``organizations.authorization.has_organization_permission``. The package header
resolver cannot currently produce the cross-binding request shape that would
make this branch decisive, so these are direct, low-level policy tests rather
than endpoint claims.
"""

from django.contrib.auth import get_user_model

import pytest
from model_bakery import baker
from rest_framework.test import APIRequestFactory

from common.organization_context import organization_context
from common.organization_services import memberships
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from organizations.permission_catalog import MANAGE_BILLING
from organizations.permissions import IsBillingOwnerOrAdmin
from organizations.tests.helpers import make_membership


User = get_user_model()


@pytest.fixture
def reseller_root(db):
    return baker.make(
        Organization, name="Reseller", slug="reseller-root", can_invite_organizations=True
    )


@pytest.fixture
def descendant(reseller_root):
    return baker.make(Organization, name="Client", slug="reseller-child", parent=reseller_root)


@pytest.fixture
def grandchild(descendant):
    return baker.make(
        Organization, name="Sub-client", slug="reseller-grandchild", parent=descendant
    )


@pytest.fixture
def plain_root(db):
    """A root that pays for nobody -- ``can_invite_organizations`` is False."""
    return baker.make(Organization, name="Solo", slug="solo-root", can_invite_organizations=False)


def _request(user):
    membership = user if isinstance(user, OrganizationMembership) else None
    if membership is not None:
        user = membership.user
    else:
        membership = memberships.resolve_for_user(user)

    request = APIRequestFactory().post("/")
    request.user = user
    request.organization_membership = membership
    return request


@pytest.mark.django_db
class TestActingResellerRoot:
    permission = IsBillingOwnerOrAdmin()

    def test_an_admin_of_the_root_may_manage_a_descendants_billing(self, reseller_root, descendant):
        """The low-level branch admits a permitted reseller-root membership
        against a descendant target."""
        user = baker.make(User)
        make_membership(user=user, organization=reseller_root, role=OrganizationRole.ADMIN)

        with organization_context(descendant):
            assert self.permission.has_object_permission(_request(user), None, descendant) is True

    def test_the_binding_a_request_actually_produces(self, reseller_root, descendant):
        """The same low-level policy is true when the root is bound directly."""
        user = baker.make(User)
        make_membership(user=user, organization=reseller_root, role=OrganizationRole.ADMIN)

        with organization_context(reseller_root):
            assert self.permission.has_object_permission(_request(user), None, descendant) is True
            # The control: the same binding, a plain member, refused. Without it
            # the assertion above would also hold against a branch that admitted
            # everyone whose bound organization happened to be a reseller root.
            plain = baker.make(User)
            make_membership(user=plain, organization=reseller_root, role=OrganizationRole.MEMBER)

            assert self.permission.has_object_permission(_request(plain), None, descendant) is False

    def test_a_bare_has_perm_under_that_binding_would_have_said_no(self, reseller_root, descendant):
        """The low-level branch must name the reseller-root organization."""
        user = baker.make(User)
        make_membership(user=user, organization=reseller_root, role=OrganizationRole.ADMIN)
        with organization_context(descendant):
            assert user.has_perm(MANAGE_BILLING) is False
            assert self.permission.has_object_permission(_request(user), None, descendant) is True

    def test_a_billing_owner_of_the_root_may_too(self, reseller_root, descendant):
        """``payments.manage_billing`` is the capability, not "is an admin" --
        the ``organization_billing_owner`` group carries it as well, which is
        what makes the old ``is_admin or is_billing_owner`` disjunction one
        permission rather than two."""
        user = baker.make(User)
        make_membership(
            user=user,
            organization=reseller_root,
            role=OrganizationRole.MEMBER,
            is_billing_owner=True,
        )
        with organization_context(descendant):
            assert self.permission.has_object_permission(_request(user), None, descendant) is True

    def test_the_reach_extends_down_the_whole_subtree(self, reseller_root, descendant, grandchild):
        user = baker.make(User)
        make_membership(user=user, organization=reseller_root, role=OrganizationRole.ADMIN)
        with organization_context(grandchild):
            assert self.permission.has_object_permission(_request(user), None, grandchild) is True

    def test_a_plain_member_of_the_root_may_not(self, reseller_root, descendant):
        """The capability sub-check is the only part that swapped, so this is the
        row that catches it having been dropped rather than swapped."""
        user = baker.make(User)
        make_membership(user=user, organization=reseller_root, role=OrganizationRole.MEMBER)
        with organization_context(descendant):
            assert self.permission.has_object_permission(_request(user), None, descendant) is False

    def test_a_deactivated_admin_of_the_root_may_not(self, reseller_root, descendant):
        user = baker.make(User)
        make_membership(
            user=user,
            organization=reseller_root,
            role=OrganizationRole.ADMIN,
            is_active=False,
        )
        with organization_context(descendant):
            assert self.permission.has_object_permission(_request(user), None, descendant) is False

    def test_an_admin_of_a_root_that_invites_nobody_may_not(self, plain_root, descendant):
        """``can_invite_organizations`` stays in the branch. It is not a
        permission and was not swapped for one -- an ordinary organization's
        admin gets no reach outside it."""
        user = baker.make(User)
        make_membership(user=user, organization=plain_root, role=OrganizationRole.ADMIN)
        with organization_context(descendant):
            assert self.permission.has_object_permission(_request(user), None, descendant) is False

    def test_the_reach_does_not_run_upward(self, reseller_root, descendant):
        """An admin of the descendant gets nothing over the parent, even though
        the parent is the billing root. ``is_target_in_subtree`` is directional
        and stays so."""
        user = baker.make(User)
        make_membership(user=user, organization=descendant, role=OrganizationRole.ADMIN)
        with organization_context(descendant):
            assert (
                self.permission.has_object_permission(_request(user), None, reseller_root) is False
            )

    def test_the_reach_does_not_run_sideways(self, reseller_root, descendant):
        """Two unrelated roots: neither is in the other's subtree."""
        other_root = baker.make(
            Organization, name="Rival", slug="rival-root", can_invite_organizations=True
        )
        user = baker.make(User)
        make_membership(user=user, organization=other_root, role=OrganizationRole.ADMIN)
        with organization_context(descendant):
            assert self.permission.has_object_permission(_request(user), None, descendant) is False

    def test_the_coarse_gate_also_asks_about_the_membership_organization(
        self, reseller_root, descendant
    ):
        """The coarse gate checks the membership's explicitly named organization."""
        user = baker.make(User)
        make_membership(user=user, organization=reseller_root, role=OrganizationRole.ADMIN)
        with organization_context(descendant):
            assert self.permission.has_permission(_request(user), None) is True
