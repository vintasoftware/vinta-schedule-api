from django.contrib.auth import get_user_model

import pytest
from model_bakery import baker
from rest_framework.test import APIRequestFactory

from organizations.models import (
    Organization,
    OrganizationMembership,
    OrganizationRole,
)
from organizations.permissions import IsOrganizationAdmin


User = get_user_model()


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
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
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
            role=OrganizationRole.MEMBER,
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
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
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
        request = factory.get("/")
        request.user = admin_user
        assert permission.has_permission(request, view_mock) is True

    def test_has_permission_member_user(self, factory, member_user, permission, view_mock):
        """Member user with membership should have permission."""
        request = factory.get("/")
        request.user = member_user
        assert permission.has_permission(request, view_mock) is True

    def test_has_permission_membership_less_user(
        self, factory, membership_less_user, permission, view_mock
    ):
        """User without membership should not have permission."""
        request = factory.get("/")
        request.user = membership_less_user
        assert permission.has_permission(request, view_mock) is False

    def test_has_permission_unauthenticated_user(self, factory, permission, view_mock):
        """Unauthenticated user should not have permission."""
        request = factory.get("/")
        request.user = None
        assert permission.has_permission(request, view_mock) is False

    def test_has_object_permission_admin_same_org(self, factory, admin_user, permission, view_mock):
        """Admin user should have object permission for an object in their organization."""
        org = admin_user.organization_membership.organization
        request = factory.get("/")
        request.user = admin_user
        assert permission.has_object_permission(request, view_mock, org) is True

    def test_has_object_permission_member_same_org(
        self, factory, member_user, permission, view_mock
    ):
        """Member user should not have object permission for an object in their organization."""
        org = member_user.organization_membership.organization
        request = factory.get("/")
        request.user = member_user
        assert permission.has_object_permission(request, view_mock, org) is False

    def test_has_object_permission_admin_different_org(
        self, factory, admin_user, different_org_admin, permission, view_mock
    ):
        """Admin user should not have object permission for an object in a different organization."""
        different_org = different_org_admin.organization_membership.organization
        request = factory.get("/")
        request.user = admin_user
        assert permission.has_object_permission(request, view_mock, different_org) is False

    def test_has_object_permission_membership_less_user(
        self, factory, membership_less_user, permission, view_mock
    ):
        """User without membership should not have object permission."""
        org = baker.make(Organization)
        request = factory.get("/")
        request.user = membership_less_user
        assert permission.has_object_permission(request, view_mock, org) is False

    def test_has_object_permission_with_organization_model_subclass(
        self, factory, admin_user, permission, view_mock
    ):
        """Admin user should have object permission for OrganizationModel subclasses."""
        from calendar_integration.models import Calendar

        org = admin_user.organization_membership.organization
        calendar = baker.make(Calendar, organization=org)
        request = factory.get("/")
        request.user = admin_user
        assert permission.has_object_permission(request, view_mock, calendar) is True

    def test_has_object_permission_member_with_organization_model_subclass(
        self, factory, member_user, permission, view_mock
    ):
        """Member user should not have object permission for OrganizationModel subclasses."""
        from calendar_integration.models import Calendar

        org = member_user.organization_membership.organization
        calendar = baker.make(Calendar, organization=org)
        request = factory.get("/")
        request.user = member_user
        assert permission.has_object_permission(request, view_mock, calendar) is False

    def test_has_object_permission_cross_org_organization_model(
        self, factory, admin_user, permission, view_mock
    ):
        """Admin user should not have object permission for OrganizationModel in different org."""
        from calendar_integration.models import Calendar

        different_org = baker.make(Organization)
        calendar = baker.make(Calendar, organization=different_org)
        request = factory.get("/")
        request.user = admin_user
        assert permission.has_object_permission(request, view_mock, calendar) is False
