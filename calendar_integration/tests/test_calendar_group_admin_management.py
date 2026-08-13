"""Tests for CalendarGroup management by organization admins.

Covers the use-case where a non-owner (e.g. a clinic administrator, scheduler,
or ops user) needs to manage a CalendarGroup without being listed as a
`CalendarOwnership` on any of its pool calendars. Organization admins can
manage every group in their own organization; org-admin privileges don't cross
organization boundaries.
"""

from unittest.mock import Mock

import pytest

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    Calendar,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
)
from calendar_integration.permissions import CalendarGroupPermission
from calendar_integration.services.calendar_permission_service import (
    CalendarPermissionService,
)
from organizations.authorization import membership_holds_permission
from organizations.models import (
    Organization,
    OrganizationMembership,
)
from organizations.permission_catalog import GROUP_ORGANIZATION_MEMBER, MANAGE_MEMBERS
from organizations.tests.helpers import grant_membership_groups
from users.models import User


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Clinic", should_sync_rooms=False)


@pytest.fixture
def other_org(db):
    return Organization.objects.create(name="Other", should_sync_rooms=False)


@pytest.fixture
def group(organization):
    calendar = Calendar.objects.create(
        organization=organization,
        name="Dr. A",
        external_id="phys_a",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
    )
    g = CalendarGroup.objects.create(organization=organization, name="Clinic Appointments")
    slot = CalendarGroupSlot.objects.create(organization=organization, group=g, name="Physicians")
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=calendar
    )
    return g


# ---------------------------------------------------------------------------
# Membership capability, read from groups
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_membership_defaults_to_no_capability(organization):
    user = User.objects.create_user(email="default@example.com")
    membership = OrganizationMembership.objects.create(user=user, organization=organization)
    assert membership_holds_permission(membership, MANAGE_MEMBERS) is False


@pytest.mark.django_db
def test_membership_in_the_admin_group_holds_manage_members(organization):
    user = User.objects.create_user(email="admin@example.com")
    membership = grant_membership_groups(
        OrganizationMembership.objects.create(
            user=user,
            organization=organization,
        )
    )
    assert membership_holds_permission(membership, MANAGE_MEMBERS) is True


# ---------------------------------------------------------------------------
# User.is_organization_admin helper
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_user_is_organization_admin_true_for_admin(organization):
    user = User.objects.create_user(email="user-admin@example.com")
    grant_membership_groups(
        OrganizationMembership.objects.create(
            user=user,
            organization=organization,
        )
    )
    assert user.is_organization_admin(organization) is True
    # Also accepts an id directly.
    assert user.is_organization_admin(organization.id) is True


@pytest.mark.django_db
def test_user_is_organization_admin_false_for_member(organization):
    user = User.objects.create_user(email="user-member@example.com")
    OrganizationMembership.objects.create(
        user=user,
        organization=organization,
    )
    assert user.is_organization_admin(organization) is False


@pytest.mark.django_db
def test_user_is_organization_admin_false_for_other_org(organization, other_org):
    user = User.objects.create_user(email="cross-org@example.com")
    grant_membership_groups(
        OrganizationMembership.objects.create(
            user=user,
            organization=organization,
        )
    )
    # Admin in `organization`, no membership in `other_org` → False
    assert user.is_organization_admin(other_org) is False


@pytest.mark.django_db
def test_user_is_organization_admin_false_without_membership(organization):
    user = User.objects.create_user(email="nomembership@example.com")
    assert user.is_organization_admin(organization) is False


# ---------------------------------------------------------------------------
# can_manage_calendar_group admin override
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_admin_can_manage_group_without_ownership(organization, group):
    admin = User.objects.create_user(email="noowner-admin@example.com")
    grant_membership_groups(
        OrganizationMembership.objects.create(
            user=admin,
            organization=organization,
        )
    )
    # Intentionally no CalendarOwnership → before the admin override was added, this was False.
    svc = CalendarPermissionService()
    assert svc.can_manage_calendar_group(user=admin, group=group) is True


@pytest.mark.django_db
def test_admin_of_other_org_cannot_manage_group(organization, other_org, group):
    admin_elsewhere = User.objects.create_user(email="xorg-admin@example.com")
    grant_membership_groups(
        OrganizationMembership.objects.create(
            user=admin_elsewhere,
            organization=other_org,
        )
    )
    svc = CalendarPermissionService()
    assert svc.can_manage_calendar_group(user=admin_elsewhere, group=group) is False


@pytest.mark.django_db
def test_demoted_admin_cannot_manage_group(organization, group):
    user = User.objects.create_user(email="demoted@example.com")
    membership = grant_membership_groups(
        OrganizationMembership.objects.create(
            user=user,
            organization=organization,
        )
    )
    svc = CalendarPermissionService()
    assert svc.can_manage_calendar_group(user=user, group=group) is True
    # Downgrade to member and re-check — permission is revoked. This is exactly
    # what a live demotion does: ``OrganizationMembershipViewSet.assign_groups``
    # routes the new group set through ``assign_membership_groups``, and the
    # authorization decision reads those groups.
    grant_membership_groups(membership, [GROUP_ORGANIZATION_MEMBER])
    # A *fresh* user object, not ``refresh_from_db()``: both auth backends stash
    # resolved permission sets as attributes on the user instance, and
    # ``refresh_from_db`` reloads columns without touching them. Every real
    # request builds the user from scratch, so this is what a request sees --
    # and it is the one behavioural difference from the old per-call
    # ``.exists()`` check worth stating out loud.
    user = User.objects.get(pk=user.pk)
    assert svc.can_manage_calendar_group(user=user, group=group) is False


@pytest.mark.django_db
def test_calendar_group_permission_passes_for_admin_without_ownership(organization, group):
    admin = User.objects.create_user(email="perm-admin@example.com")
    membership = grant_membership_groups(
        OrganizationMembership.objects.create(
            user=admin,
            organization=organization,
        )
    )
    perm = CalendarGroupPermission(calendar_permission_service=CalendarPermissionService())
    request = Mock()
    request.user = admin
    request.organization_membership = membership
    assert perm.has_permission(request, view=Mock()) is True
    assert perm.has_object_permission(request, view=Mock(), obj=group) is True


# ---------------------------------------------------------------------------
# OrganizationService.create_organization assigns ADMIN to creator
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_create_organization_grants_admin_to_creator():
    from unittest.mock import Mock

    from di_core.containers import container
    from organizations.services import OrganizationService

    mock_calendar_service = Mock()
    with container.calendar_service.override(mock_calendar_service):
        svc = OrganizationService()
    creator = User.objects.create_user(email="org-creator@example.com")
    organization = svc.create_organization(
        creator=creator, name="Fresh Org", should_sync_rooms=False
    )
    membership = OrganizationMembership.objects.get(user=creator, organization=organization)
    assert membership_holds_permission(membership, MANAGE_MEMBERS) is True
    assert creator.is_organization_admin(organization) is True
