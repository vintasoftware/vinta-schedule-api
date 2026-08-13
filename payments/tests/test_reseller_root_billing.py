"""The one authorization case ``has_perm`` alone gets wrong, so it gets its own module.

``IsBillingOwnerOrAdmin`` has two branches. The first asks whether the caller
may manage billing **in the organization the object belongs to**. The second --
the acting-reseller-root branch -- asks whether they may manage billing in an
*ancestor* of it that pays for the descendant's capacity.

``vinta_orgs.auth_backends.OrganizationModelBackend`` resolves permissions for
the organization bound to the current context and for no other. So a bare
``user.has_perm("payments.manage_billing")`` inside that second branch would ask
about the bound organization -- the descendant -- and answer ``False`` for a
caller whose grant is held one level up. The branch would silently stop
granting: a *narrowing*, which at least fails loudly for the operator it locks
out, but a regression all the same, and one no other test in the suite would
catch (see ``payments/tests/views/test_occurrence_ledger_view.py``'s
``test_root_admin_sees_a_pooled_descendants_rows``, whose own docstring records
that pooling, not this branch, is what grants it access).

Phase 4 of the vinta-django-orgs migration therefore keeps the branch
hand-written (the plan's "Four rules stay hand-written" Guiding Decision) and
names the ancestor explicitly through
``organizations.authorization.has_organization_permission``. The tests below pin
that: the same call, under the same binding, answers differently depending on
whether the organization is named.
"""

from django.contrib.auth import get_user_model

import pytest
from model_bakery import baker
from rest_framework.test import APIRequestFactory

from common.organization_context import organization_context
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
    request = APIRequestFactory().post("/")
    request.user = user
    return request


def _acting_from(user, organization):
    """Resolve the caller's membership to ``organization``, as the request path does."""
    user._active_membership = OrganizationMembership.objects.filter(
        user=user, organization=organization, is_active=True
    ).first()
    return user


@pytest.mark.django_db
class TestActingResellerRoot:
    permission = IsBillingOwnerOrAdmin()

    def test_an_admin_of_the_root_may_manage_a_descendants_billing(self, reseller_root, descendant):
        """The headline case, in the shape the phase brief names: the bound
        organization is the **descendant** while the caller's resolved
        membership -- and their grant -- is in the parent."""
        user = baker.make(User)
        make_membership(user=user, organization=reseller_root, role=OrganizationRole.ADMIN)
        _acting_from(user, reseller_root)

        with organization_context(descendant):
            assert self.permission.has_object_permission(_request(user), None, descendant) is True

    def test_the_binding_a_request_actually_produces(self, reseller_root, descendant):
        """Bound = the caller's own organization, target = a descendant.

        Every other case in this class binds the **descendant** while the
        caller's only membership is in the root. Since Phase 3.5 that state
        cannot arise on the request path: ``X-Organization-Id`` naming an
        organization the caller does not belong to is a 403, and
        ``_active_membership`` is resolved from the same header -- so
        ``membership.organization`` *is* the bound organization, always. Those
        cases exercise the slow path (rebind, one query); this one exercises the
        fast path (``has_organization_permission`` sees the organization it is
        asked about already bound, and neither rebinds nor queries), which is
        the one every real request takes. Same answer, different route to it.

        (Whether any REST caller passes a *descendant* as ``obj`` at all is a
        separate question -- every one of them passes
        ``resolve_billing_root(acting)``, which is an ancestor-or-self. The
        branch is kept because it states a rule about the subtree that outlives
        the current call sites.)
        """
        user = baker.make(User)
        make_membership(user=user, organization=reseller_root, role=OrganizationRole.ADMIN)
        _acting_from(user, reseller_root)

        with organization_context(reseller_root):
            assert self.permission.has_object_permission(_request(user), None, descendant) is True
            # The control: the same binding, a plain member, refused. Without it
            # the assertion above would also hold against a branch that admitted
            # everyone whose bound organization happened to be a reseller root.
            plain = baker.make(User)
            make_membership(user=plain, organization=reseller_root, role=OrganizationRole.MEMBER)
            _acting_from(plain, reseller_root)

            assert self.permission.has_object_permission(_request(plain), None, descendant) is False

    def test_a_bare_has_perm_under_that_binding_would_have_said_no(self, reseller_root, descendant):
        """Why the branch cannot be a plain ``user.has_perm(...)``.

        Same user, same binding, same instant: asked without naming an
        organization the answer is ``False``, because the backend resolves
        against the bound descendant where this caller holds nothing. This is
        the assertion that makes the previous test's ``True`` mean something
        rather than merely being true.
        """
        user = baker.make(User)
        make_membership(user=user, organization=reseller_root, role=OrganizationRole.ADMIN)
        _acting_from(user, reseller_root)

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
        _acting_from(user, reseller_root)

        with organization_context(descendant):
            assert self.permission.has_object_permission(_request(user), None, descendant) is True

    def test_the_reach_extends_down_the_whole_subtree(self, reseller_root, descendant, grandchild):
        user = baker.make(User)
        make_membership(user=user, organization=reseller_root, role=OrganizationRole.ADMIN)
        _acting_from(user, reseller_root)

        with organization_context(grandchild):
            assert self.permission.has_object_permission(_request(user), None, grandchild) is True

    def test_a_plain_member_of_the_root_may_not(self, reseller_root, descendant):
        """The capability sub-check is the only part that swapped, so this is the
        row that catches it having been dropped rather than swapped."""
        user = baker.make(User)
        make_membership(user=user, organization=reseller_root, role=OrganizationRole.MEMBER)
        _acting_from(user, reseller_root)

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
        user._active_membership = OrganizationMembership.objects.get(
            user=user, organization=reseller_root
        )

        with organization_context(descendant):
            assert self.permission.has_object_permission(_request(user), None, descendant) is False

    def test_an_admin_of_a_root_that_invites_nobody_may_not(self, plain_root, descendant):
        """``can_invite_organizations`` stays in the branch. It is not a
        permission and was not swapped for one -- an ordinary organization's
        admin gets no reach outside it."""
        user = baker.make(User)
        make_membership(user=user, organization=plain_root, role=OrganizationRole.ADMIN)
        _acting_from(user, plain_root)

        with organization_context(descendant):
            assert self.permission.has_object_permission(_request(user), None, descendant) is False

    def test_the_reach_does_not_run_upward(self, reseller_root, descendant):
        """An admin of the descendant gets nothing over the parent, even though
        the parent is the billing root. ``is_target_in_subtree`` is directional
        and stays so."""
        user = baker.make(User)
        make_membership(user=user, organization=descendant, role=OrganizationRole.ADMIN)
        _acting_from(user, descendant)

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
        _acting_from(user, other_root)

        with organization_context(descendant):
            assert self.permission.has_object_permission(_request(user), None, descendant) is False

    def test_the_coarse_gate_also_asks_about_the_membership_organization(
        self, reseller_root, descendant
    ):
        """``has_permission`` runs before any object is known, so it can only ask
        about the caller's own organization. Under a descendant binding that is
        still the parent -- pinned because a bare ``has_perm`` here would refuse
        the request before ``has_object_permission`` ever ran, and the object
        branch's correctness would then be unobservable."""
        user = baker.make(User)
        make_membership(user=user, organization=reseller_root, role=OrganizationRole.ADMIN)
        _acting_from(user, reseller_root)

        with organization_context(descendant):
            assert self.permission.has_permission(_request(user), None) is True
