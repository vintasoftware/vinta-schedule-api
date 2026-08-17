from django.contrib.auth import get_user_model

import pytest
from model_bakery import baker
from rest_framework.test import APIRequestFactory

from calendar_integration.models import Calendar
from common.organization_services import memberships
from organizations.models import (
    Organization,
    OrganizationMembership,
)
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN
from organizations.permissions import IsOrganizationAdmin
from organizations.tests.helpers import make_membership


User = get_user_model()


def _request_for_user(factory, user):
    """Build the request state the package mixin provides before permissions run."""
    request = factory.get("/")
    request.user = user
    request.organization_membership = memberships.resolve_for_user(user)
    return request


@pytest.mark.django_db
class TestIsOrganizationAdminPermission:
    """Test suite for IsOrganizationAdmin permission."""

    @pytest.fixture
    def factory(self):
        return APIRequestFactory()

    @pytest.fixture
    def admin_user(self):
        """Create a user with admin role in an organization."""
        user = baker.make(User)
        organization = baker.make(Organization)
        make_membership(
            user=user,
            organization=organization,
            groups=[GROUP_ORGANIZATION_ADMIN],
        )
        return user

    @pytest.fixture
    def member_user(self):
        """Create a user with member role in an organization."""
        user = baker.make(User)
        organization = baker.make(Organization)
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
        )
        return user

    @pytest.fixture
    def membership_less_user(self):
        """Create a user with no organization membership."""
        return baker.make(User)

    @pytest.fixture
    def different_org_admin(self):
        """Create an admin user in a different organization."""
        user = baker.make(User)
        organization = baker.make(Organization)
        make_membership(
            user=user,
            organization=organization,
            groups=[GROUP_ORGANIZATION_ADMIN],
        )
        return user

    @pytest.fixture
    def permission(self):
        return IsOrganizationAdmin()

    @pytest.fixture
    def view_mock(self):
        """Mock view object."""
        return None

    def test_has_permission_admin_user(self, factory, admin_user, permission, view_mock):
        """Admin user with membership should have permission."""
        request = _request_for_user(factory, admin_user)
        assert permission.has_permission(request, view_mock) is True

    def test_has_permission_member_user(self, factory, member_user, permission, view_mock):
        """Member user without admin role should not have permission."""
        request = _request_for_user(factory, member_user)
        assert permission.has_permission(request, view_mock) is False

    def test_has_permission_membership_less_user(
        self, factory, membership_less_user, permission, view_mock
    ):
        """User without membership should not have permission."""
        request = _request_for_user(factory, membership_less_user)
        assert permission.has_permission(request, view_mock) is False

    def test_has_permission_unauthenticated_user(self, factory, permission, view_mock):
        """Unauthenticated user should not have permission."""
        request = _request_for_user(factory, None)
        assert permission.has_permission(request, view_mock) is False

    def test_has_object_permission_admin_same_org(self, factory, admin_user, permission, view_mock):
        """Admin user should have object permission for an object in their organization."""
        org = admin_user.memberships.get().organization
        request = _request_for_user(factory, admin_user)
        assert permission.has_object_permission(request, view_mock, org) is True

    def test_has_object_permission_member_same_org(
        self, factory, member_user, permission, view_mock
    ):
        """Member user should not have object permission for an object in their organization."""
        org = member_user.memberships.get().organization
        request = _request_for_user(factory, member_user)
        assert permission.has_object_permission(request, view_mock, org) is False

    def test_has_object_permission_admin_different_org(
        self, factory, admin_user, different_org_admin, permission, view_mock
    ):
        """Admin user should not have object permission for an object in a different organization."""
        different_org = different_org_admin.memberships.get().organization
        request = _request_for_user(factory, admin_user)
        assert permission.has_object_permission(request, view_mock, different_org) is False

    def test_has_object_permission_membership_less_user(
        self, factory, membership_less_user, permission, view_mock
    ):
        """User without membership should not have object permission."""
        org = baker.make(Organization)
        request = _request_for_user(factory, membership_less_user)
        assert permission.has_object_permission(request, view_mock, org) is False

    def test_has_object_permission_with_organization_model_subclass(
        self, factory, admin_user, permission, view_mock
    ):
        """Admin user should have object permission for organization-scoped models."""
        org = admin_user.memberships.get().organization
        calendar = baker.make(Calendar, organization=org)
        request = _request_for_user(factory, admin_user)
        assert permission.has_object_permission(request, view_mock, calendar) is True

    def test_has_object_permission_member_with_organization_model_subclass(
        self, factory, member_user, permission, view_mock
    ):
        """Member user should not have object permission for organization-scoped models."""
        org = member_user.memberships.get().organization
        calendar = baker.make(Calendar, organization=org)
        request = _request_for_user(factory, member_user)
        assert permission.has_object_permission(request, view_mock, calendar) is False

    def test_has_object_permission_cross_org_organization_model(
        self, factory, admin_user, permission, view_mock
    ):
        """Admin user should not have object permission for an organization-scoped model in a different org."""
        different_org = baker.make(Organization)
        calendar = baker.make(Calendar, organization=different_org)
        request = _request_for_user(factory, admin_user)
        assert permission.has_object_permission(request, view_mock, calendar) is False

    def test_has_permission_inactive_admin_denied(self, factory, permission, view_mock):
        """Admin with an inactive membership is denied — is_active=False gates all access."""
        user = baker.make(User)
        org = baker.make(Organization)
        make_membership(
            user=user,
            organization=org,
            groups=[GROUP_ORGANIZATION_ADMIN],
            is_active=False,
        )
        request = _request_for_user(factory, user)
        assert permission.has_permission(request, view_mock) is False

    def test_has_object_permission_inactive_admin_denied(self, factory, permission, view_mock):
        """Inactive admin is denied object permission even for their own org."""
        user = baker.make(User)
        org = baker.make(Organization)
        make_membership(
            user=user,
            organization=org,
            groups=[GROUP_ORGANIZATION_ADMIN],
            is_active=False,
        )
        request = _request_for_user(factory, user)
        assert permission.has_object_permission(request, view_mock, org) is False

    def test_has_permission_inactive_member_denied(self, factory, permission, view_mock):
        """Member with an inactive membership is denied."""
        user = baker.make(User)
        org = baker.make(Organization)
        baker.make(
            OrganizationMembership,
            user=user,
            organization=org,
            is_active=False,
        )
        request = _request_for_user(factory, user)
        assert permission.has_permission(request, view_mock) is False

    def test_has_permission_active_member_denied(self, factory, permission, view_mock):
        """Active member without admin role is denied at has_permission level."""
        user = baker.make(User)
        org = baker.make(Organization)
        baker.make(
            OrganizationMembership,
            user=user,
            organization=org,
            is_active=True,
        )
        request = _request_for_user(factory, user)
        assert permission.has_permission(request, view_mock) is False
