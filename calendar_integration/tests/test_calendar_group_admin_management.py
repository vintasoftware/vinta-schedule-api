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
from organizations.models import (
    Organization,
    OrganizationMembership,
    OrganizationRole,
)
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
# OrganizationRole + OrganizationMembership.is_admin
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_membership_defaults_to_member(organization):
    user = User.objects.create_user(email="default@example.com")
    membership = OrganizationMembership.objects.create(user=user, organization=organization)
    assert membership.role == OrganizationRole.MEMBER
    assert membership.is_admin is False


@pytest.mark.django_db
def test_membership_is_admin_when_role_admin(organization):
    user = User.objects.create_user(email="admin@example.com")
    membership = OrganizationMembership.objects.create(
        user=user, organization=organization, role=OrganizationRole.ADMIN
    )
    assert membership.is_admin is True


# ---------------------------------------------------------------------------
# User.is_organization_admin helper
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_user_is_organization_admin_true_for_admin(organization):
    user = User.objects.create_user(email="user-admin@example.com")
    OrganizationMembership.objects.create(
        user=user, organization=organization, role=OrganizationRole.ADMIN
    )
    assert user.is_organization_admin(organization) is True
    # Also accepts an id directly.
    assert user.is_organization_admin(organization.id) is True


@pytest.mark.django_db
def test_user_is_organization_admin_false_for_member(organization):
    user = User.objects.create_user(email="user-member@example.com")
    OrganizationMembership.objects.create(
        user=user, organization=organization, role=OrganizationRole.MEMBER
    )
    assert user.is_organization_admin(organization) is False


@pytest.mark.django_db
def test_user_is_organization_admin_false_for_other_org(organization, other_org):
    user = User.objects.create_user(email="cross-org@example.com")
    OrganizationMembership.objects.create(
        user=user, organization=organization, role=OrganizationRole.ADMIN
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
    OrganizationMembership.objects.create(
        user=admin, organization=organization, role=OrganizationRole.ADMIN
    )
    # Intentionally no CalendarOwnership → prior to PR7 this would be False.
    svc = CalendarPermissionService()
    assert svc.can_manage_calendar_group(user=admin, group=group) is True


@pytest.mark.django_db
def test_admin_of_other_org_cannot_manage_group(organization, other_org, group):
    admin_elsewhere = User.objects.create_user(email="xorg-admin@example.com")
    OrganizationMembership.objects.create(
        user=admin_elsewhere, organization=other_org, role=OrganizationRole.ADMIN
    )
    svc = CalendarPermissionService()
    assert svc.can_manage_calendar_group(user=admin_elsewhere, group=group) is False


@pytest.mark.django_db
def test_demoted_admin_cannot_manage_group(organization, group):
    user = User.objects.create_user(email="demoted@example.com")
    membership = OrganizationMembership.objects.create(
        user=user, organization=organization, role=OrganizationRole.ADMIN
    )
    svc = CalendarPermissionService()
    assert svc.can_manage_calendar_group(user=user, group=group) is True
    # Downgrade to member and re-check — permission is revoked.
    membership.role = OrganizationRole.MEMBER
    membership.save(update_fields=["role"])
    # Reload user so the related-object cache drops the stale membership.
    user.refresh_from_db()
    assert svc.can_manage_calendar_group(user=user, group=group) is False


@pytest.mark.django_db
def test_calendar_group_permission_passes_for_admin_without_ownership(organization, group):
    admin = User.objects.create_user(email="perm-admin@example.com")
    OrganizationMembership.objects.create(
        user=admin, organization=organization, role=OrganizationRole.ADMIN
    )
    perm = CalendarGroupPermission(calendar_permission_service=CalendarPermissionService())
    request = Mock()
    request.user = admin
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
    assert membership.role == OrganizationRole.ADMIN
    assert creator.is_organization_admin(organization) is True
