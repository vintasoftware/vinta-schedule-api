"""The Phase 4 contract: the same allow/deny, from a permission instead of a role.

Phase 4 of the vinta-django-orgs migration
(``ai-plans/2026-08-12-VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md``)
swapped the membership's two flat capability columns for an
organization-named permission check in every **permission class** --
``organizations.authorization.has_organization_permission``, *not*
``user.has_perm``, which answers about whichever organization happens to be
bound and unions in grants that are not about an organization at all (that
module's docstring argues both). The risk it carries is one-directional and
silent: a *widened* grant fails no test, because no test asserts that a
permitted caller is refused.

"Every permission class" is the honest scope, and is narrower than "every
authorization decision": four readers of the columns survived outside them
until Phase 6 -- ``public_api/scoping.py``,
``calendar_integration/querysets.py``,
``calendar_integration/services/external_event_change_request_service.py`` and
``booking_policy_permission_service.py``'s system-user adapters. Phase 6 moved
each onto ``organizations.authorization.membership_holds_permission`` along
with the column drop; nothing in this module covers them.

So this module is a matrix, **one test class per permission class**, not one per
file. Grouping by file would let a widened grant in one class hide behind its
neighbours passing; grouping by class means a row going green that should be red
names the class it belongs to.

Thirteen classes, which is the corrected count -- the plan body says fifteen
across four files, but ``users/permissions.py`` reads no membership state at all
and ``public_api/permissions.py`` gates on system-user API scopes, not on
membership. The thirteen are the four REST permission classes and the S3Direct
``auth`` callable in ``organizations/permissions.py``, plus the eight in
``calendar_integration/permissions.py``.

**What each row asserts** is the *outcome*, spelled as a literal ``True`` /
``False`` / status code, never derived from the rule under test. The membership
states are the ones that distinguish the old rule from the new one:

* ``admin`` -- allowed before and after.
* ``member`` -- refused before and after; the row that catches a widening.
* ``billing owner`` -- allowed on billing surfaces only.
* ``deactivated admin`` -- refused. Note **where** each row's refusal comes
  from: in a permission class the request resolver refuses first (it resolves
  active memberships only and hands back ``None``), so those rows do not
  exercise the auth backend's own gate at all.
  ``TestTheBackendsIsActiveGate`` at the end of this module covers the paths
  where that gate *is* the only thing standing between a deactivated admin and
  full rights -- every ``User.is_organization_admin`` caller, which names an
  organization and resolves no membership of its own. Removing the gate turns
  that class red and leaves the rest of this module green, which is the whole
  reason it is a separate class.
* ``admin of another organization`` -- refused *here*. The row that proves the
  permission is asked about the named organization rather than whatever happens
  to be bound.
* ``membership carrying no groups`` -- refused. Every production write path
  assigns ``organization_member``, so this is what an unconverted test fixture
  looks like rather than a state production reaches; it is pinned so the shape
  is a named, refused one rather than a quietly-passing test.

**Where a grant may come from** is asserted as explicitly as the outcomes, in
``TestOnlyAMembershipGrants``: authorization here is answered from an active
membership in the organization named, and from nothing else. The three shapes
that would otherwise leak -- a global ``user_permissions`` grant, membership of
the global ``organization_admin`` ``auth.Group`` (which ``users/admin.py``'s
group picker lists on the *user* form), and ``is_superuser`` -- were all inert
under the two flat columns, so admitting any of them would be a
widening, not a migration. See ``organizations/authorization.py``.

One deliberate outcome change is pinned here as a decision rather than left to
be discovered, and is argued in that same module: an inactive *user* passes
nothing.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.http import Http404

import pytest
from model_bakery import baker
from rest_framework.test import APIRequestFactory
from vinta_orgs import authorization as vinta_orgs_authorization

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    BookingPolicy,
    Calendar,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarOwnership,
)
from calendar_integration.permissions import (
    BookingPolicyPermission,
    CalendarAvailabilityPermission,
    CalendarEventPermission,
    CalendarGroupPermission,
    ExternalEventChangeRequestPermission,
    GroupScopedAvailabilityWindowPermission,
    GroupScopedBlockedTimePermission,
    GroupScopedQuotaRulePermission,
)
from calendar_integration.services.booking_policy_permission_service import (
    BookingPolicyPermissionService,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from common.organization_context import organization_context
from common.organization_services import memberships
from organizations import authorization
from organizations.authorization import has_organization_permission
from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
)
from organizations.permission_catalog import (
    GROUP_ORGANIZATION_ADMIN,
    GROUP_ORGANIZATION_BILLING_OWNER,
    GROUP_ORGANIZATION_MEMBER,
    MANAGE_BILLING,
    MANAGE_MEMBERS,
)
from organizations.permissions import (
    IsBillingOwnerOrAdmin,
    IsOrganizationAdmin,
    OrganizationInvitationPermission,
    OrganizationManagementPermission,
)
from organizations.tests.helpers import make_membership


User = get_user_model()


# ---------------------------------------------------------------------------
# Fixtures: one organization, one "other" organization, and a caller per state.
# ---------------------------------------------------------------------------


@pytest.fixture
def factory():
    return APIRequestFactory()


@pytest.fixture
def organization(db):
    return baker.make(Organization, name="Acme", slug="acme-parity")


@pytest.fixture
def other_organization(db):
    return baker.make(Organization, name="Other", slug="other-parity")


def _caller(organization, **membership_kwargs):
    """A user plus the membership that decides what they may do."""
    user = baker.make(User)
    make_membership(user=user, organization=organization, **membership_kwargs)
    return user


@pytest.fixture
def admin(organization):
    return _caller(organization, groups=[GROUP_ORGANIZATION_ADMIN])


@pytest.fixture
def member(organization):
    return _caller(organization, groups=[GROUP_ORGANIZATION_MEMBER])


@pytest.fixture
def billing_owner(organization):
    return _caller(organization, groups=[GROUP_ORGANIZATION_BILLING_OWNER])


@pytest.fixture
def deactivated_admin(organization):
    return _caller(organization, groups=[GROUP_ORGANIZATION_ADMIN], is_active=False)


@pytest.fixture
def ungrouped_admin(organization):
    """A membership nothing put in a group at all.

    Exactly what a raw ``baker.make`` produces. It is not a *state* production
    cannot reach -- every write path assigns ``organization_member``, which
    carries nothing, so this is indistinguishable in outcome from a plain
    member. Pinned all the same because it is what an unconverted test fixture
    looks like, and every gate below must refuse it.
    """
    user = baker.make(User)
    baker.make(
        OrganizationMembership,
        user=user,
        organization=organization,
    )
    return user


@pytest.fixture
def stranger(db):
    """Authenticated, but a member of nothing."""
    return baker.make(User)


@pytest.fixture
def foreign_admin(other_organization):
    """Administers ``other_organization`` and nothing else."""
    return _caller(other_organization, groups=[GROUP_ORGANIZATION_ADMIN])


def request_for(factory, user, method="get", data=None):
    """A request carrying only what a permission class reads.

    ``data`` is set as an attribute rather than posted: ``BookingPolicyPermission``
    reads ``request.data`` (DRF's parsed body), which a bare
    ``APIRequestFactory`` request does not have.
    """
    membership = user if isinstance(user, OrganizationMembership) else None
    if membership is not None:
        user = membership.user
    elif user is not None:
        membership = memberships.resolve_for_user(user)

    request = getattr(factory, method)("/")
    request.user = user
    request.organization_membership = membership
    request.data = data if data is not None else {}
    return request


def acting_in(user, organization):
    """Return the membership the package resolves onto a request.

    Permission classes read ``request.organization_membership``. Passing this
    result to ``request_for`` models that request contract, including for a
    caller with memberships in more than one organization.
    """
    return OrganizationMembership.objects.filter(
        user=user, organization=organization, is_active=True
    ).first()


class _View:
    """The bit of a view a permission class actually reads."""

    def __init__(self, action=None, **kwargs):
        self.action = action
        self.kwargs = kwargs


# ---------------------------------------------------------------------------
# 1. OrganizationManagementPermission -- gates on membership *absence*
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOrganizationManagementPermission:
    """Nothing about this class is permission-shaped, and nothing in it changed.

    Its rule is "you may reach the onboarding endpoints only if you are *not* a
    member" -- an inversion no capability can express. Kept verbatim per the
    plan's "Four rules stay hand-written" Guiding Decision; pinned here so a
    later sweep does not mistake it for one that was missed.
    """

    permission = OrganizationManagementPermission()

    def test_a_membership_less_user_reaches_onboarding(self, factory, stranger):
        assert self.permission.has_permission(request_for(factory, stranger), _View("list")) is True

    def test_an_admin_does_not(self, factory, admin, organization):
        request = request_for(factory, acting_in(admin, organization))

        assert self.permission.has_permission(request, _View("list")) is False

    def test_a_plain_member_does_not_either(self, factory, member, organization):
        request = request_for(factory, acting_in(member, organization))

        assert self.permission.has_permission(request, _View("list")) is False

    def test_a_deactivated_admin_counts_as_membership_less(
        self, factory, deactivated_admin, organization
    ):
        request = request_for(factory, deactivated_admin)

        assert self.permission.has_permission(request, _View("list")) is True

    def test_create_is_open_to_any_authenticated_caller(self, factory, admin, organization):
        request = request_for(factory, acting_in(admin, organization), method="post")

        assert self.permission.has_permission(request, _View("create")) is True

    def test_object_access_needs_the_object_to_be_your_own_organization(
        self, factory, admin, organization, other_organization
    ):
        request = request_for(factory, acting_in(admin, organization))

        assert (
            self.permission.has_object_permission(request, _View("retrieve"), organization) is True
        )
        assert (
            self.permission.has_object_permission(request, _View("retrieve"), other_organization)
            is False
        )


# ---------------------------------------------------------------------------
# 2. OrganizationInvitationPermission -- membership presence, no capability
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOrganizationInvitationPermission:
    """Reads membership *presence* only, and did before too. A plain member may
    manage their organization's invitations; that is not a Phase 4 widening."""

    permission = OrganizationInvitationPermission()

    @pytest.mark.parametrize(
        ("caller", "expected"),
        [("admin", True), ("member", True), ("billing_owner", True), ("stranger", False)],
    )
    def test_membership_presence_decides(self, request, factory, organization, caller, expected):
        user = request.getfixturevalue(caller)
        assert self.permission.has_permission(request_for(factory, user), _View()) is expected

    def test_a_deactivated_admin_is_refused(self, factory, deactivated_admin, organization):
        assert (
            self.permission.has_permission(request_for(factory, deactivated_admin), _View())
            is False
        )

    def test_object_access_is_confined_to_your_own_organization(
        self, factory, admin, organization, other_organization
    ):
        own = baker.make(OrganizationInvitation, organization=organization, email="a@example.com")
        foreign = baker.make(
            OrganizationInvitation, organization=other_organization, email="b@example.com"
        )
        req = request_for(factory, acting_in(admin, organization))

        assert self.permission.has_object_permission(req, _View(), own) is True
        assert self.permission.has_object_permission(req, _View(), foreign) is False


# ---------------------------------------------------------------------------
# 3. IsOrganizationAdmin -- organizations.manage_members
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestIsOrganizationAdminParity:
    permission = IsOrganizationAdmin()

    @pytest.mark.parametrize(
        ("caller", "expected"),
        [
            ("admin", True),
            ("member", False),
            ("billing_owner", False),
            ("deactivated_admin", False),
            ("ungrouped_admin", False),
            ("stranger", False),
        ],
    )
    def test_collection_level(self, request, factory, organization, caller, expected):
        user = request.getfixturevalue(caller)
        assert self.permission.has_permission(request_for(factory, user), _View("list")) is expected

    def test_an_admin_of_another_organization_is_refused_here(
        self, factory, foreign_admin, organization, other_organization
    ):
        """The row a bare ``has_perm`` under an ambient binding gets wrong."""
        request = request_for(factory, foreign_admin)

        assert (
            self.permission.has_object_permission(request, _View("retrieve"), organization) is False
        )
        assert (
            self.permission.has_object_permission(request, _View("retrieve"), other_organization)
            is True
        )

    def test_an_admin_of_both_is_answered_per_organization(
        self, factory, admin, organization, other_organization
    ):
        make_membership(
            user=admin,
            organization=other_organization,
        )

        assert (
            self.permission.has_object_permission(
                request_for(factory, acting_in(admin, organization)),
                _View("retrieve"),
                organization,
            )
            is True
        )

        # Same user, same process, the other organization: a plain member there.
        assert (
            self.permission.has_object_permission(
                request_for(factory, acting_in(admin, other_organization)),
                _View("retrieve"),
                other_organization,
            )
            is False
        )

    def test_object_access_needs_the_object_and_membership_to_agree(
        self, factory, admin, organization, other_organization
    ):
        request = request_for(factory, acting_in(admin, organization))
        foreign_membership = make_membership(
            user=baker.make(User),
            organization=other_organization,
        )

        assert (
            self.permission.has_object_permission(request, _View("retrieve"), organization) is True
        )
        assert (
            self.permission.has_object_permission(request, _View("retrieve"), foreign_membership)
            is False
        )

    def test_an_unauthenticated_caller_is_refused(self, factory):
        assert self.permission.has_permission(request_for(factory, None), _View("list")) is False

    def test_a_global_grant_still_needs_a_membership(self, factory, organization):
        """A Django-admin-assigned ``organizations.manage_members`` on a caller
        who belongs to no organization admits nothing. Two independent things
        stop it -- the active-membership check in this class, and the fact that
        the grant is not resolved from a membership at all
        (``TestOnlyAMembershipGrants`` below covers the second on its own).
        Pinned so dropping either is a red test."""
        outsider = baker.make(User)
        outsider.user_permissions.add(Permission.objects.get(codename="manage_members"))

        assert (
            self.permission.has_permission(request_for(factory, outsider), _View("list")) is False
        )


# ---------------------------------------------------------------------------
# 4. IsBillingOwnerOrAdmin -- payments.manage_billing (+ the hand-written walk)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestIsBillingOwnerOrAdminParity:
    permission = IsBillingOwnerOrAdmin()

    @pytest.mark.parametrize(
        ("caller", "expected"),
        [
            ("admin", True),
            ("billing_owner", True),
            ("member", False),
            ("deactivated_admin", False),
            ("ungrouped_admin", False),
            ("stranger", False),
        ],
    )
    def test_collection_level(self, request, factory, organization, caller, expected):
        user = request.getfixturevalue(caller)
        assert self.permission.has_permission(request_for(factory, user), _View()) is expected

    @pytest.mark.parametrize(
        ("caller", "expected"),
        [("admin", True), ("billing_owner", True), ("member", False)],
    )
    def test_object_level_against_the_billing_root(
        self, request, factory, organization, caller, expected
    ):
        user = request.getfixturevalue(caller)
        assert (
            self.permission.has_object_permission(request_for(factory, user), _View(), organization)
            is expected
        )

    def test_an_admin_of_another_organization_cannot_manage_this_ones_billing(
        self, factory, foreign_admin, organization, other_organization
    ):
        """``other_organization`` is not a reseller root and ``organization`` is
        not in its subtree, so neither branch admits."""
        assert (
            self.permission.has_object_permission(
                request_for(factory, foreign_admin), _View(), organization
            )
            is False
        )

    def test_a_target_that_carries_an_organization_resolves_through_it(
        self, factory, admin, organization
    ):
        class _CarriesOrganization:
            def __init__(self, org):
                self.organization = org

        assert (
            self.permission.has_object_permission(
                request_for(factory, admin), _View(), _CarriesOrganization(organization)
            )
            is True
        )


# ---------------------------------------------------------------------------
# 5. user_administers_branding_eligible_organization -- the S3Direct auth gate
#    (covered in organizations/tests/test_branding_gate_parity.py, which owns
#    the entitlement axis this class composes with)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 6. BookingPolicyPermission
# ---------------------------------------------------------------------------


@pytest.fixture
def booking_policy_permission():
    return BookingPolicyPermission(
        booking_policy_permission_service=BookingPolicyPermissionService()
    )


@pytest.mark.django_db
class TestBookingPolicyPermissionParity:
    """Reads are open to any authenticated caller; writes need the admin
    capability, or a target the caller personally owns.

    Only the admin short-circuit in ``has_permission`` moved.
    The per-target decision still lives in ``BookingPolicyPermissionService``,
    which is a service rather than a permission class and is therefore out of
    this phase's scope -- but it no longer *derives* privilege: this class hands
    it the answer it already computed (``is_privileged``), so the two cannot
    disagree about the same caller. The service's remaining readers elsewhere
    moved onto ``membership_holds_permission`` in Phase 6.
    """

    def test_reads_stay_open_to_a_plain_member(
        self, factory, booking_policy_permission, member, organization
    ):
        request = request_for(factory, acting_in(member, organization))

        assert booking_policy_permission.has_permission(request, _View("list")) is True

    def test_an_admin_may_create_any_policy(
        self, factory, booking_policy_permission, admin, organization
    ):
        request = request_for(
            factory, acting_in(admin, organization), method="post", data={"calendar_group": 1}
        )

        assert booking_policy_permission.has_permission(request, _View("create")) is True

    def test_a_plain_member_may_not_create_a_group_policy(
        self, factory, booking_policy_permission, member, organization
    ):
        group = CalendarGroup.objects.create(organization=organization, name="Pool")
        request = request_for(
            factory,
            acting_in(member, organization),
            method="post",
            data={"calendar_group": group.id},
        )

        assert booking_policy_permission.has_permission(request, _View("create")) is False

    def test_a_deactivated_admin_may_not_create(
        self, factory, booking_policy_permission, deactivated_admin, organization
    ):
        request = request_for(factory, deactivated_admin, method="post", data={})

        assert booking_policy_permission.has_permission(request, _View("create")) is False

    def test_an_ungrouped_admin_is_refused_by_the_service_too(
        self, factory, booking_policy_permission, ungrouped_admin, organization
    ):
        """The two representations no longer disagree on this path.

        This class's short-circuit reads ``organizations.manage_members`` as of
        Phase 4, so it does not fire for a membership carrying no groups. The
        create path then falls through to
        ``BookingPolicyPermissionService.can_member_manage_target``, which used
        to re-derive privilege from the membership's own ``role`` column and
        admit the caller after all -- one line below a gate that had just
        refused them by permission. It now takes the *same* answer the class
        computed, passed in as ``is_privileged``, so a caller refused at
        ``has_permission`` is refused at ``has_object_permission`` as well and
        cannot create a policy they are then unable to edit.

        Phase 6 dropped the column, so the two representations can no longer
        disagree at all; what stays pinned here is that a group-less membership
        is refused at both ends.
        """
        group = CalendarGroup.objects.create(organization=organization, name="Pool 2")
        request = request_for(
            factory, ungrouped_admin, method="post", data={"calendar_group": group.id}
        )

        assert booking_policy_permission.has_permission(request, _View("create")) is False

    def test_privilege_by_permission_alone_survives_to_the_object_gate(
        self, factory, booking_policy_permission, member, organization
    ):
        """Create and update must agree about the same caller.

        A membership carrying the admin group while its (now dropped) ``role``
        column still said MEMBER was the mirror image of ``ungrouped_admin`` --
        and the half Phase 5 made reachable, since clients assign groups
        directly from then on. Such a caller passed ``has_permission``'s
        capability short-circuit and was then refused by the service on
        ``has_object_permission``, which re-derived privilege from the column:
        they could create a group policy and not edit the row they had just
        created. Both ends now read the same answer.
        """
        membership = OrganizationMembership.objects.get(user=member, organization=organization)
        membership.groups.add(Group.objects.get(name=GROUP_ORGANIZATION_ADMIN))
        group = CalendarGroup.objects.create(organization=organization, name="Pool 3")
        assert (
            booking_policy_permission.has_permission(
                request_for(factory, member, method="post", data={"calendar_group": group.id}),
                _View("create"),
            )
            is True
        )

        policy = BookingPolicy.objects.create(organization=organization, calendar_group=group)

        assert (
            booking_policy_permission.has_object_permission(
                request_for(factory, member, method="patch"), _View("partial_update"), policy
            )
            is True
        )

    def test_an_unauthenticated_caller_is_refused_outright(
        self, factory, booking_policy_permission
    ):
        assert (
            booking_policy_permission.has_permission(request_for(factory, None), _View()) is False
        )


# ---------------------------------------------------------------------------
# 7-9. The three classes that read no membership capability at all
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestExternalEventChangeRequestPermissionParity:
    """Membership presence, no capability -- unchanged by Phase 4, and pinned
    so a later reader does not assume it was overlooked."""

    permission = ExternalEventChangeRequestPermission()

    @pytest.mark.parametrize(
        ("caller", "expected"), [("admin", True), ("member", True), ("stranger", False)]
    )
    def test_membership_presence_decides(self, request, factory, organization, caller, expected):
        user = request.getfixturevalue(caller)
        assert self.permission.has_permission(request_for(factory, user), _View()) is expected

    def test_a_deactivated_admin_is_refused(self, factory, deactivated_admin, organization):
        assert (
            self.permission.has_permission(request_for(factory, deactivated_admin), _View())
            is False
        )


@pytest.mark.django_db
class TestCalendarEventPermissionParity:
    """Authentication plus calendar ownership. No role check, before or after."""

    permission = CalendarEventPermission()

    def test_any_authenticated_caller_passes_the_collection_gate(self, factory, stranger):
        assert self.permission.has_permission(request_for(factory, stranger), _View()) is True

    def test_object_access_needs_ownership_not_admin(self, factory, admin, member, organization):
        calendar = Calendar.objects.create(
            organization=organization,
            name="Owned",
            external_id="owned-1",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.PERSONAL,
        )
        CalendarOwnership.objects.create(
            organization=organization, calendar=calendar, membership_user_id=member.id
        )
        event = baker.make(
            "calendar_integration.CalendarEvent",
            organization=organization,
            calendar=calendar,
            # baker would otherwise invent a timezone string the Postgres
            # conversion function rejects.
            timezone="UTC",
        )

        with organization_context(organization):
            assert (
                self.permission.has_object_permission(
                    request_for(factory, acting_in(member, organization)), _View(), event
                )
                is True
            )
            # An org admin who owns nothing is still refused -- this class has
            # never had an admin branch, and Phase 4 did not add one.
            assert (
                self.permission.has_object_permission(
                    request_for(factory, acting_in(admin, organization)), _View(), event
                )
                is False
            )


@pytest.mark.django_db
class TestCalendarAvailabilityPermissionParity:
    """Authentication only."""

    permission = CalendarAvailabilityPermission()

    def test_authentication_is_the_whole_rule(self, factory, stranger):
        assert self.permission.has_permission(request_for(factory, stranger), _View()) is True


# ---------------------------------------------------------------------------
# 10-13. The four group-scoped classes
# ---------------------------------------------------------------------------


@pytest.fixture
def group_fixture(organization):
    """A group with one slot holding one calendar, owned by nobody yet."""
    calendar = Calendar.objects.create(
        organization=organization,
        name="Dr. A",
        external_id="phys-a-parity",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
    )
    group = CalendarGroup.objects.create(organization=organization, name="Appointments")
    slot = CalendarGroupSlot.objects.create(
        organization=organization, group=group, name="Physicians"
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=calendar
    )
    return group, slot, calendar


@pytest.fixture
def calendar_group_permission():
    return CalendarGroupPermission(calendar_permission_service=CalendarPermissionService())


@pytest.mark.django_db
class TestCalendarGroupPermissionParity:
    """Composes an admin check with group-scoped object logic -- one of the four
    the plan keeps hand-written, and the one where a mechanical swap could
    quietly widen.

    Three separate decisions, each pinned:

    1. ``create`` (collection level) -- admin only.
    2. ``update`` / ``destroy`` -- admin only, through
       ``can_manage_calendar_group``. **Owning a pool calendar is not enough**;
       that is the widening this class is most exposed to.
    3. every other object action -- admin *or* a member who owns a calendar
       somewhere in the group. The member half never read a role and is
       untouched, so it is asserted here as the control: if it had been
       collapsed into the admin check, (2) would have gone green for a
       non-admin owner.
    """

    @pytest.mark.parametrize(
        ("caller", "expected"),
        [
            ("admin", True),
            ("member", False),
            ("deactivated_admin", False),
            ("ungrouped_admin", False),
        ],
    )
    def test_create_is_admin_only(
        self, request, factory, calendar_group_permission, organization, caller, expected
    ):
        user = request.getfixturevalue(caller)
        assert (
            calendar_group_permission.has_permission(
                request_for(factory, user, method="post"), _View("create")
            )
            is expected
        )

    def test_a_non_admin_owner_may_view_but_not_manage(
        self, factory, calendar_group_permission, member, organization, group_fixture
    ):
        group, _slot, calendar = group_fixture
        CalendarOwnership.objects.create(
            organization=organization, calendar=calendar, membership_user_id=member.id
        )
        request = request_for(factory, acting_in(member, organization))

        with organization_context(organization):
            assert (
                calendar_group_permission.has_object_permission(request, _View("retrieve"), group)
                is True
            )
            assert (
                calendar_group_permission.has_object_permission(request, _View("destroy"), group)
                is False
            )

    def test_an_admin_who_owns_nothing_may_both_view_and_manage(
        self, factory, calendar_group_permission, admin, organization, group_fixture
    ):
        group, _slot, _calendar = group_fixture
        request = request_for(factory, acting_in(admin, organization))

        with organization_context(organization):
            assert (
                calendar_group_permission.has_object_permission(request, _View("retrieve"), group)
                is True
            )
            assert (
                calendar_group_permission.has_object_permission(request, _View("destroy"), group)
                is True
            )

    def test_a_member_who_owns_nothing_may_do_neither(
        self, factory, calendar_group_permission, member, organization, group_fixture
    ):
        group, _slot, _calendar = group_fixture
        request = request_for(factory, acting_in(member, organization))

        with organization_context(organization):
            assert (
                calendar_group_permission.has_object_permission(request, _View("retrieve"), group)
                is False
            )
            assert (
                calendar_group_permission.has_object_permission(request, _View("destroy"), group)
                is False
            )

    def test_admin_rights_do_not_cross_organizations(
        self, factory, calendar_group_permission, foreign_admin, other_organization, group_fixture
    ):
        """The organization-match gate runs first and is what keeps an admin of
        another organization out. Pinned because the capability check that
        follows it is now organization-aware too, and a reader could conclude
        either one is redundant -- neither is."""
        group, _slot, _calendar = group_fixture
        request = request_for(factory, acting_in(foreign_admin, other_organization))

        with organization_context(other_organization):
            assert (
                calendar_group_permission.has_object_permission(request, _View("retrieve"), group)
                is False
            )

    def test_an_ungrouped_admin_who_owns_nothing_is_refused(
        self, factory, calendar_group_permission, ungrouped_admin, organization, group_fixture
    ):
        group, _slot, _calendar = group_fixture
        request = request_for(factory, acting_in(ungrouped_admin, organization))

        with organization_context(organization):
            assert (
                calendar_group_permission.has_object_permission(request, _View("retrieve"), group)
                is False
            )


GROUP_SCOPED_CLASSES = [
    GroupScopedAvailabilityWindowPermission,
    GroupScopedBlockedTimePermission,
    GroupScopedQuotaRulePermission,
]


@pytest.mark.django_db
@pytest.mark.parametrize("permission_class", GROUP_SCOPED_CLASSES)
class TestGroupScopedRouteGatesParity:
    """The three nested-route gates, each asserted separately.

    They share an implementation, so a single shared test would have been
    cheaper -- and would have let a divergence in one of the three pass unseen.
    Parametrising over the class keeps one row per class in the report while
    stating the rule once.

    The rule composes the admin capability with group-scoped object logic: a
    caller sees the ``(group, slot)`` if they administer the organization **or**
    own a calendar somewhere in the group. Every refusal is an ``Http404``, not
    a 403 -- a member must not learn a group exists from the error shape.
    """

    def _permission(self, permission_class):
        return permission_class(calendar_permission_service=CalendarPermissionService())

    def _view(self, group, slot):
        return _View(group_id=group.id, slot_id=slot.id)

    def test_an_admin_who_owns_nothing_passes(
        self, permission_class, factory, admin, organization, group_fixture
    ):
        group, slot, _calendar = group_fixture
        request = request_for(factory, acting_in(admin, organization))

        assert self._permission(permission_class).has_permission(request, self._view(group, slot))

    def test_a_non_admin_owner_passes(
        self, permission_class, factory, member, organization, group_fixture
    ):
        group, slot, calendar = group_fixture
        CalendarOwnership.objects.create(
            organization=organization, calendar=calendar, membership_user_id=member.id
        )
        request = request_for(factory, acting_in(member, organization))

        assert self._permission(permission_class).has_permission(request, self._view(group, slot))

    def test_a_member_who_owns_nothing_gets_a_404(
        self, permission_class, factory, member, organization, group_fixture
    ):
        group, slot, _calendar = group_fixture
        request = request_for(factory, acting_in(member, organization))

        with pytest.raises(Http404):
            self._permission(permission_class).has_permission(request, self._view(group, slot))

    def test_a_deactivated_admin_is_refused_before_the_group_is_reached(
        self, permission_class, factory, deactivated_admin, organization, group_fixture
    ):
        group, slot, _calendar = group_fixture
        request = request_for(factory, deactivated_admin)

        assert (
            self._permission(permission_class).has_permission(request, self._view(group, slot))
            is False
        )

    def test_an_ungrouped_admin_gets_a_404(
        self, permission_class, factory, ungrouped_admin, organization, group_fixture
    ):
        group, slot, _calendar = group_fixture
        request = request_for(factory, acting_in(ungrouped_admin, organization))

        with pytest.raises(Http404):
            self._permission(permission_class).has_permission(request, self._view(group, slot))

    def test_an_admin_of_another_organization_gets_a_404(
        self, permission_class, factory, foreign_admin, other_organization, group_fixture
    ):
        """The slot lookup is organization-scoped, so a foreign admin cannot
        even resolve it -- and the capability check that follows would refuse
        them too, because it names *this* group's organization."""
        group, slot, _calendar = group_fixture
        request = request_for(factory, acting_in(foreign_admin, other_organization))

        with pytest.raises(Http404):
            self._permission(permission_class).has_permission(request, self._view(group, slot))

    def test_a_slot_belonging_to_a_different_group_gets_a_404(
        self, permission_class, factory, admin, organization, group_fixture
    ):
        _group, slot, _calendar = group_fixture
        decoy = CalendarGroup.objects.create(organization=organization, name="Decoy")
        request = request_for(factory, acting_in(admin, organization))

        with pytest.raises(Http404):
            self._permission(permission_class).has_permission(request, self._view(decoy, slot))

    def test_an_unauthenticated_caller_is_refused(self, permission_class, factory, group_fixture):
        group, slot, _calendar = group_fixture

        assert (
            self._permission(permission_class).has_permission(
                request_for(factory, AnonymousUser()), self._view(group, slot)
            )
            is False
        )


# ---------------------------------------------------------------------------
# Where a grant may come from: an active membership in the organization named
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOnlyAMembershipGrants:
    """Three grants that were inert under ``role`` and must stay inert.

    ``user.has_perm`` answers from the **union** of the organization half with a
    global half (``user.user_permissions`` plus the user's own ``auth.Group``
    rows), after ``PermissionsMixin`` short-circuits ``is_superuser`` ahead of
    every backend. None of the three is a statement about the organization
    named, and none of them satisfied ``membership.role == ADMIN``. So
    ``organizations.authorization.has_organization_permission`` asks the
    membership half alone -- ``vinta_orgs.authorization
    .has_organization_permission`` with ``include_global`` and
    ``allow_superuser`` both off -- and this class is what keeps it doing so: put
    ``user.has_perm(permission)`` back in that helper, or flip either flag on,
    and every row below turns green-to-red.

    This class asserts the escalations through the **permission classes**, which
    is where they would be exploited. ``TestTheEscalationFlagsStayOff`` below
    asserts the same three against the helper directly, and additionally proves
    each flag is load-bearing by flipping it on.

    Each row is a live escalation path, not a hypothetical:

    * a single ``user_permissions`` row on a plain member;
    * ``users/admin.py`` puts ``groups`` in ``filter_horizontal`` on the *user*
      form and leaves ``GroupAdmin`` registered, so the seeded, global
      ``organization_admin`` group is one click away in the picker -- and it
      carries ``manage_members`` / ``manage_organization`` / ``manage_branding``
      / ``manage_billing``;
    * a superuser whose only membership is a plain one. "They already reach
      everything through the Django admin" is not an argument on
      ``IsBillingOwnerOrAdmin``: passing it changes a plan, buys an add-on or
      cancels a subscription **at Stripe / MercadoPago**.
    """

    admin_permission = IsOrganizationAdmin()
    billing_permission = IsBillingOwnerOrAdmin()

    def test_a_global_user_permission_does_not_make_a_member_an_admin(
        self, factory, member, organization
    ):
        member.user_permissions.add(Permission.objects.get(codename="manage_members"))
        assert (
            self.admin_permission.has_permission(request_for(factory, member), _View("list"))
            is False
        )
        assert member.is_organization_admin(organization) is False

    def test_the_global_organization_admin_group_does_not_either(
        self, factory, member, organization, other_organization
    ):
        """The group Phase 3 seeded is a plain global ``auth.Group``. Adding a
        *user* to it must grant nothing anywhere -- including in organizations
        they have never been a member of."""
        member.groups.add(Group.objects.get(name=GROUP_ORGANIZATION_ADMIN))
        assert (
            self.admin_permission.has_permission(request_for(factory, member), _View("list"))
            is False
        )
        assert member.is_organization_admin(organization) is False
        assert member.is_organization_admin(other_organization) is False

    def test_a_global_grant_does_not_reach_the_billing_endpoints(
        self, factory, member, organization
    ):
        """Same shape on the surface that spends money."""
        member.user_permissions.add(Permission.objects.get(codename="manage_billing"))
        assert (
            self.billing_permission.has_permission(request_for(factory, member), _View()) is False
        )
        assert (
            self.billing_permission.has_object_permission(
                request_for(factory, member), _View(), organization
            )
            is False
        )

    def test_a_superuser_holding_a_plain_membership_is_still_a_plain_member(
        self, factory, organization
    ):
        superuser = baker.make(User, is_superuser=True, is_active=True)
        make_membership(
            user=superuser, organization=organization, groups=[GROUP_ORGANIZATION_MEMBER]
        )
        assert (
            self.admin_permission.has_permission(request_for(factory, superuser), _View("list"))
            is False
        )
        assert (
            self.billing_permission.has_object_permission(
                request_for(factory, superuser), _View(), organization
            )
            is False
        )
        assert superuser.is_organization_admin(organization) is False

    def test_a_superuser_with_an_admin_membership_passes_on_that_membership(
        self, factory, organization
    ):
        """The control. Without it every assertion above would also hold against
        a helper that refused superusers outright, which is not the rule --
        the rule is that a superuser is answered from their memberships like
        anybody else."""
        superuser = baker.make(User, is_superuser=True, is_active=True)
        make_membership(
            user=superuser, organization=organization, groups=[GROUP_ORGANIZATION_ADMIN]
        )
        assert (
            self.admin_permission.has_permission(request_for(factory, superuser), _View("list"))
            is True
        )
        assert superuser.is_organization_admin(organization) is True

    def test_is_organization_admin_is_false_where_there_is_no_membership(
        self, admin, organization, other_organization
    ):
        """``is_organization_admin`` is asked with an organization the caller may
        have no relationship to at all -- ``IsOrganizationAdmin
        .has_object_permission`` and ``CalendarPermissionService`` both do it.
        Every current caller happens to guard it first; the method must be
        membership-bounded on its own regardless, because that is what its
        docstring promises and what ``role == ADMIN`` structurally was.

        Both non-membership shapes are covered: the global group (which
        ``has_perm`` would union in) and ``is_superuser`` (which
        ``PermissionsMixin`` would short-circuit ahead of any backend). Both
        answer ``True`` under a bare ``has_perm`` for an organization the caller
        has never belonged to, and both must answer ``False`` here."""
        admin.groups.add(Group.objects.get(name=GROUP_ORGANIZATION_ADMIN))

        assert admin.is_organization_admin(organization) is True
        assert admin.is_organization_admin(other_organization) is False
        assert admin.is_organization_admin(other_organization.id) is False

        # A superuser who is a member of nothing at all -- the widest shape.
        superuser = baker.make(User, is_superuser=True, is_active=True)

        assert superuser.is_organization_admin(organization) is False
        assert superuser.is_organization_admin(organization.id) is False


# ---------------------------------------------------------------------------
# The two escalation flags: off, passed explicitly, and load-bearing
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheEscalationFlagsStayOff:
    """``include_global=False`` and ``allow_superuser=False``, asserted not assumed.

    Package ``0.3.0`` owns the rule this repository asks
    (``vinta_orgs.authorization.has_organization_permission``) and defaults both
    widening parameters off. **A default is not a guarantee** -- it is a
    dependency's decision, revisable in a release we do not control, and the two
    things it would admit are precisely the escalations this phase's review found:
    a permission granted once in the Django user admin, and one click in that same
    form's ``groups`` picker, each becoming all four capabilities in *every*
    organization in the database.

    **The two are not independent, and ``include_global`` is the larger of
    them.** The global half is fetched through
    ``OrganizationModelBackend.get_all_global_permissions``, which applies its
    own ``is_superuser`` short-circuit (``_get_global_permissions``), so turning
    ``INCLUDE_GLOBAL_PERMISSIONS`` on re-admits every superuser to all four
    capabilities in every organization **whatever ``ALLOW_SUPERUSER`` says** --
    ``ALLOW_SUPERUSER`` guards only the organization half's short-circuit, which
    never runs once the global one has answered. Mutating each constant in turn
    shows it as a **strict superset** rather than merely a bigger number:
    ``ALLOW_SUPERUSER = True`` alone turns 5 rows red,
    ``INCLUDE_GLOBAL_PERMISSIONS = True`` alone turns 10, and the 10 contain all
    5 -- including the ``test_allow_superuser_is_what_refuses_...`` row below,
    whose name would otherwise suggest one flag owns that refusal. Read either
    constant's comment in
    ``organizations/authorization.py`` for the same statement at the point of
    declaration.

    So ``organizations/authorization.py`` passes both explicitly, and this class
    pins three separate things, none of which the others imply:

    1. the adapter **passes** them rather than inheriting them -- so an upstream
       change of default is inert in production, and deleting the kwargs to "rely
       on the defaults" is red here;
    2. with them off, each of the three escalation shapes is refused;
    3. with each one on, the *same* fixture is admitted. That is what makes (2)
       non-vacuous: without it every assertion in (2) would also hold against a
       package function that refused everybody, and against a fixture that never
       carried the grant in the first place.

    The last method is the control in the other direction: both flags off must
    still admit an ordinary admin.
    """

    def test_the_adapter_passes_both_flags_rather_than_inheriting_them(
        self, monkeypatch, member, organization
    ):
        recorded: dict[str, object] = {}

        def spy(user, permission, organization, **kwargs):
            recorded.update(kwargs)
            return False

        monkeypatch.setattr(vinta_orgs_authorization, "has_organization_permission", spy)

        has_organization_permission(member, MANAGE_MEMBERS, organization)

        assert recorded == {"include_global": False, "allow_superuser": False}

    def test_include_global_is_what_refuses_a_django_admin_granted_permission(
        self, member, organization
    ):
        member.user_permissions.add(Permission.objects.get(codename="manage_members"))

        assert has_organization_permission(member, MANAGE_MEMBERS, organization) is False
        # The same call with the flag on: the grant is real, and the flag is the
        # only thing standing between it and every organization.
        assert (
            vinta_orgs_authorization.has_organization_permission(
                member, MANAGE_MEMBERS, organization, include_global=True
            )
            is True
        )

    def test_include_global_is_what_refuses_the_seeded_global_admin_group(
        self, member, organization, other_organization
    ):
        member.groups.add(Group.objects.get(name=GROUP_ORGANIZATION_ADMIN))

        assert has_organization_permission(member, MANAGE_MEMBERS, organization) is False
        assert has_organization_permission(member, MANAGE_BILLING, organization) is False
        # Not even in an organization they have never belonged to.
        assert has_organization_permission(member, MANAGE_MEMBERS, other_organization) is False
        assert (
            vinta_orgs_authorization.has_organization_permission(
                member, MANAGE_MEMBERS, organization, include_global=True
            )
            is True
        )

    def test_allow_superuser_is_what_refuses_a_superuser_holding_no_admin_membership(
        self, organization
    ):
        superuser = baker.make(User, is_superuser=True, is_active=True)
        make_membership(user=superuser, organization=organization)

        assert has_organization_permission(superuser, MANAGE_MEMBERS, organization) is False
        assert has_organization_permission(superuser, MANAGE_BILLING, organization) is False
        assert (
            vinta_orgs_authorization.has_organization_permission(
                superuser, MANAGE_BILLING, organization, allow_superuser=True
            )
            is True
        )

    def test_both_flags_off_still_admits_an_ordinary_admin(self, admin, organization):
        """The control, in the other direction. Without it every row above is
        satisfied by an adapter that answers ``False`` unconditionally."""
        assert has_organization_permission(admin, MANAGE_MEMBERS, organization) is True
        assert has_organization_permission(admin, MANAGE_BILLING, organization) is True
        assert authorization.INCLUDE_GLOBAL_PERMISSIONS is False
        assert authorization.ALLOW_SUPERUSER is False


# ---------------------------------------------------------------------------
# The auth backend's is_active gate, on the paths where it is the only gate
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheBackendsIsActiveGate:
    """Where a deactivated admin's refusal actually comes from.

    Every permission class resolves a membership from the request first, and
    that resolver filters ``is_active`` -- so in those classes a deactivated
    admin is refused before ``has_perm`` is ever called, and a row asserting
    the refusal there proves nothing about the backend.

    ``User.is_organization_admin(organization)`` is the counter-example, and it
    is the shape most of ``calendar_integration`` reaches admin-ness through
    (``CalendarPermissionService``, ``calendar_integration/views.py``,
    ``CalendarGroupPermission``'s DI fallback). It names an organization and
    resolves no membership of its own, so the permission lookup is the whole
    decision -- and ``has_perm`` does not carry an ``is_active`` filter. What
    supplies it is ``vinta_orgs.auth_backends.OrganizationModelBackend
    ._get_membership``, which filters ``is_active=True`` as part of the lookup
    rather than on its result, so the per-organization cache never holds a row
    nothing may use (package ``0.3.0``; see the plan's **Package owns the
    authorization substrate** Guiding Decision).

    Before Phase 4, ``is_organization_admin`` filtered ``is_active=True`` in its
    own query. This class is what keeps that outcome after the filter moved into
    the backend: delete the gate from ``_get_membership`` and every assertion
    below flips to ``True`` -- a deactivated admin with full rights, the widest
    regression this migration can produce.
    """

    def test_a_deactivated_admin_is_not_an_organization_admin(
        self, deactivated_admin, organization
    ):
        assert deactivated_admin.is_organization_admin(organization) is False
        assert deactivated_admin.is_organization_admin(organization.id) is False

    def test_reactivation_restores_it(self, deactivated_admin, organization):
        membership = OrganizationMembership.objects.get(
            user=deactivated_admin, organization=organization
        )
        membership.is_active = True
        membership.save(update_fields=["is_active"])

        # A fresh instance: the backend caches its answer on the user object.
        reloaded = User.objects.get(pk=deactivated_admin.pk)

        assert reloaded.is_organization_admin(organization) is True

    def test_a_deactivated_admin_cannot_see_or_manage_a_calendar_group(
        self, deactivated_admin, organization, group_fixture
    ):
        """The reachable consequence: the group-visibility service asks
        ``is_organization_admin`` directly, with no membership resolution in
        front of it."""
        group, _slot, _calendar = group_fixture
        service = CalendarPermissionService()

        with organization_context(organization):
            assert service.can_view_calendar_group(user=deactivated_admin, group=group) is False
            assert service.can_manage_calendar_group(user=deactivated_admin, group=group) is False

    def test_an_active_admin_can(self, admin, organization, group_fixture):
        """The control. Without it the assertions above would also pass against a
        service that refused everyone."""
        group, _slot, _calendar = group_fixture
        service = CalendarPermissionService()

        with organization_context(organization):
            assert service.can_view_calendar_group(user=admin, group=group) is True
            assert service.can_manage_calendar_group(user=admin, group=group) is True
