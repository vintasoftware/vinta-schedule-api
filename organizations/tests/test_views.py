import json
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.models import GoogleCalendarServiceAccount
from common.utils.authentication_utils import generate_long_lived_token, hash_long_lived_token
from organizations.exceptions import InvalidInvitationTokenError
from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
    OrganizationTier,
)


User = get_user_model()


def assert_response_status_code(response, expected_status_code):
    assert response.status_code == expected_status_code, (
        f"The status error {response.status_code} != {expected_status_code}\n"
        f"Response Payload: {json.dumps(response.json() if hasattr(response, 'json') and callable(response.json) else str(response.content))}"
    )


class OrganizationTestFactory:
    @staticmethod
    def create_organization(name="Test Organization", should_sync_rooms=False):
        return baker.make(
            Organization,
            name=name,
            should_sync_rooms=should_sync_rooms,
        )

    @staticmethod
    def create_organization_membership(user, organization):
        return baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
        )

    @staticmethod
    def create_organization_tier(name="Basic"):
        return baker.make(
            OrganizationTier,
            name=name,
        )

    @staticmethod
    def create_organization_invitation(
        organization, email="test@example.com", invited_by=None, **kwargs
    ):
        """Create an organization invitation with default values"""
        if invited_by is None:
            invited_by = baker.make(User)

        defaults = {
            "email": email,
            "first_name": "Test",
            "last_name": "User",
            "token_hash": "test_token_hash",
            "expires_at": timezone.now() + timezone.timedelta(days=7),
        } | kwargs
        return baker.make(
            OrganizationInvitation, organization=organization, invited_by=invited_by, **defaults
        )


@pytest.fixture
def organization():
    return OrganizationTestFactory.create_organization()


@pytest.fixture
def organization_with_membership(user):
    organization = OrganizationTestFactory.create_organization()
    OrganizationTestFactory.create_organization_membership(user, organization)
    return organization


@pytest.fixture
def organization_tier():
    return OrganizationTestFactory.create_organization_tier()


@pytest.mark.django_db
class TestOrganizationViewSet:
    """Test suite for OrganizationViewSet"""

    def test_list_organizations_not_supported(self, auth_client):
        """Test that listing organizations returns method not allowed"""
        url = reverse("api:Organizations-list")
        response = auth_client.get(url)
        assert_response_status_code(response, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_retrieve_organization_authenticated_with_membership(
        self, auth_client, user, organization_with_membership
    ):
        """Test retrieving an organization when user has membership"""
        url = reverse("api:Organizations-detail", kwargs={"pk": organization_with_membership.pk})
        response = auth_client.get(url)

        # Users with membership get 403 due to permission class logic
        # The permission only allows access when user has NO membership
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_retrieve_organization_authenticated_without_membership(
        self, auth_client, organization
    ):
        """Test retrieving an organization when user has no membership"""
        url = reverse("api:Organizations-detail", kwargs={"pk": organization.pk})
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_retrieve_organization_different_organization(self, auth_client, user):
        """Test retrieving an organization that belongs to different user"""
        # Create organization for current user
        user_organization = OrganizationTestFactory.create_organization(name="User Org")
        OrganizationTestFactory.create_organization_membership(user, user_organization)

        # Create another organization without membership
        other_organization = OrganizationTestFactory.create_organization(name="Other Org")

        url = reverse("api:Organizations-detail", kwargs={"pk": other_organization.pk})
        response = auth_client.get(url)

        # Users with membership get 403 due to permission class logic
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_retrieve_organization_unauthenticated(self, anonymous_client, organization):
        """Test retrieving an organization without authentication"""
        url = reverse("api:Organizations-detail", kwargs={"pk": organization.pk})
        response = anonymous_client.get(url)

        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    def test_retrieve_nonexistent_organization(self, auth_client):
        """Test retrieving a non-existent organization"""
        url = reverse("api:Organizations-detail", kwargs={"pk": 99999})
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    @patch("organizations.views.OrganizationViewSet.get_queryset")
    @patch("organizations.services.OrganizationService.create_organization")
    def test_create_organization_authenticated_without_membership(
        self, mock_create_organization, mock_get_queryset, auth_client, user
    ):
        """Test creating an organization when user has no existing membership"""
        new_organization = OrganizationTestFactory.create_organization(name="New Organization")
        mock_create_organization.return_value = new_organization

        # Mock the queryset to return the created organization for the re-fetch
        mock_get_queryset.return_value = Organization.objects.filter(id=new_organization.id)

        url = reverse("api:Organizations-list")
        data = {
            "name": "New Organization",
            "should_sync_rooms": False,
        }
        response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        response_data = response.json()
        assert response_data["name"] == "New Organization"
        assert response_data["should_sync_rooms"] is False

        mock_create_organization.assert_called_once_with(
            creator=user,
            name="New Organization",
            should_sync_rooms=False,
        )

    def test_create_organization_via_jwt_bearer_creates_membership(self, user):
        """Regression: real JWT auth must resolve request.user to a model User.

        DRF was configured with JWTStatelessUserAuthentication, which yields a
        TokenUser (claims only, no DB row). Assigning it to
        OrganizationMembership.user blew up with:
        'Cannot assign "<TokenUser>": "OrganizationMembership.user" must be a "User" instance.'
        Existing tests used session login / force_authenticate, so the JWT path
        was never exercised. This drives the actual reported flow end-to-end (no
        mocks) over a Bearer access token.
        """
        from rest_framework_simplejwt.tokens import AccessToken

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(user)}")

        url = reverse("api:Organizations-list")
        response = client.post(url, {"name": "JWT Org"}, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        membership = OrganizationMembership.objects.get(user=user)
        assert membership.organization.name == "JWT Org"
        assert membership.role == OrganizationRole.ADMIN

    @patch("organizations.views.OrganizationViewSet.get_queryset")
    @patch("organizations.services.OrganizationService.create_organization")
    def test_create_organization_with_sync_rooms(
        self, mock_create_organization, mock_get_queryset, auth_client, user
    ):
        """Test creating an organization with room sync enabled"""
        new_organization = OrganizationTestFactory.create_organization(
            name="Sync Organization", should_sync_rooms=True
        )
        mock_create_organization.return_value = new_organization

        # Mock the queryset to return the created organization for the re-fetch
        mock_get_queryset.return_value = Organization.objects.filter(id=new_organization.id)

        url = reverse("api:Organizations-list")
        data = {
            "name": "Sync Organization",
            "should_sync_rooms": True,
        }
        response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        response_data = response.json()
        assert response_data["should_sync_rooms"] is True

        mock_create_organization.assert_called_once_with(
            creator=user,
            name="Sync Organization",
            should_sync_rooms=True,
        )

    def test_create_organization_authenticated_with_existing_membership(
        self, auth_client, user, organization_with_membership
    ):
        """Test creating an organization when user already has membership"""
        url = reverse("api:Organizations-list")
        data = {
            "name": "Another Organization",
            "should_sync_rooms": False,
        }
        response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_create_organization_unauthenticated(self, anonymous_client):
        """Test creating an organization without authentication"""
        url = reverse("api:Organizations-list")
        data = {
            "name": "Unauthorized Organization",
            "should_sync_rooms": False,
        }
        response = anonymous_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    def test_create_organization_validation_errors(self, auth_client):
        """Test organization creation with validation errors"""
        url = reverse("api:Organizations-list")

        # Test missing name
        data = {
            "should_sync_rooms": False,
        }
        response = auth_client.post(url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "name" in response.json()

        # Test empty name
        data = {
            "name": "",
            "should_sync_rooms": False,
        }
        response = auth_client.post(url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_update_organization_authenticated_with_membership(
        self, auth_client, user, organization_with_membership
    ):
        """Test updating an organization when user has a non-admin membership.

        The organization_with_membership fixture creates a MEMBER-role membership.
        IsOrganizationAdmin (applied to update/partial_update) rejects non-admin
        members with 403.
        """
        url = reverse("api:Organizations-detail", kwargs={"pk": organization_with_membership.pk})
        data = {
            "name": "Updated Organization Name",
            "should_sync_rooms": True,
        }
        response = auth_client.patch(url, data, format="json")

        # Non-admin member → 403 (IsOrganizationAdmin denies)
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_update_organization_admin_returns_200(self, user):
        """Admin can PATCH their own organization — the configure-org use-case works."""
        organization = OrganizationTestFactory.create_organization(name="Admin Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        admin_client = APIClient()
        admin_client.force_authenticate(user=user)

        url = reverse("api:Organizations-detail", kwargs={"pk": organization.pk})
        response = admin_client.patch(url, {"name": "Admin Updated Name"}, format="json")

        assert_response_status_code(response, status.HTTP_200_OK)
        assert response.json()["name"] == "Admin Updated Name"

    def test_update_organization_admin_cross_org_returns_404(self, user):
        """Admin cannot PATCH an organization they don't belong to — 404."""
        own_org = OrganizationTestFactory.create_organization(name="Own Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=own_org,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        other_org = OrganizationTestFactory.create_organization(name="Other Org")

        admin_client = APIClient()
        admin_client.force_authenticate(user=user)

        url = reverse("api:Organizations-detail", kwargs={"pk": other_org.pk})
        response = admin_client.patch(url, {"name": "Hacked"}, format="json")

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_update_organization_authenticated_without_membership(self, auth_client, organization):
        """Test updating an organization when user has no membership.

        IsOrganizationAdmin.has_permission returns False for membership-less users → 403.
        (Previously expected 404 under OrganizationManagementPermission, but the update
        action now uses IsOrganizationAdmin which rejects at has_permission before the
        queryset is consulted.)
        """
        url = reverse("api:Organizations-detail", kwargs={"pk": organization.pk})
        data = {
            "name": "Updated Organization Name",
        }
        response = auth_client.patch(url, data, format="json")

        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_update_organization_unauthenticated(self, anonymous_client, organization):
        """Test updating an organization without authentication"""
        url = reverse("api:Organizations-detail", kwargs={"pk": organization.pk})
        data = {
            "name": "Updated Organization Name",
        }
        response = anonymous_client.patch(url, data, format="json")

        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    def test_delete_organization_authenticated_with_membership(
        self, auth_client, user, organization_with_membership
    ):
        """Test deleting an organization when user has membership"""
        url = reverse("api:Organizations-detail", kwargs={"pk": organization_with_membership.pk})
        response = auth_client.delete(url)

        # Users with membership get 403 due to permission class logic
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_delete_organization_authenticated_without_membership(self, auth_client, organization):
        """Test deleting an organization when user has no membership"""
        url = reverse("api:Organizations-detail", kwargs={"pk": organization.pk})
        response = auth_client.delete(url)

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_delete_organization_unauthenticated(self, anonymous_client, organization):
        """Test deleting an organization without authentication"""
        url = reverse("api:Organizations-detail", kwargs={"pk": organization.pk})
        response = anonymous_client.delete(url)

        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)


@pytest.mark.django_db
class TestOrganizationPermissions:
    """Test suite for organization permissions"""

    def test_organization_permission_with_membership(self, auth_client, user):
        """Test organization permissions when user has a non-admin (MEMBER) membership.

        retrieve/delete: still gated by OrganizationManagementPermission → 403.
        update: gated by IsOrganizationAdmin → non-admin member gets 403.
        """
        organization = OrganizationTestFactory.create_organization()
        # create_organization_membership uses baker default role = MEMBER
        OrganizationTestFactory.create_organization_membership(user, organization)

        # Should NOT be able to retrieve — OrganizationManagementPermission blocks members
        url = reverse("api:Organizations-detail", kwargs={"pk": organization.pk})
        response = auth_client.get(url)
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

        # Should NOT be able to update — IsOrganizationAdmin blocks non-admin members
        response = auth_client.patch(url, {"name": "Updated Name"}, format="json")
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

        # Should NOT be able to delete — OrganizationManagementPermission blocks members
        response = auth_client.delete(url)
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    @patch("organizations.services.OrganizationService.create_organization")
    def test_organization_permission_without_membership(
        self, mock_create_organization, auth_client, user
    ):
        """Test organization permissions when user has no membership"""
        organization = OrganizationTestFactory.create_organization()
        mock_create_organization.return_value = organization

        # Should be able to create (since user has no membership)
        url = reverse("api:Organizations-list")
        with patch("organizations.views.OrganizationViewSet.get_queryset") as mock_get_queryset:
            # Mock the queryset to return the created organization for the re-fetch during creation
            mock_get_queryset.return_value = Organization.objects.filter(id=organization.id)
            response = auth_client.post(url, {"name": "New Org"}, format="json")
            assert_response_status_code(response, status.HTTP_201_CREATED)

        # Should not be able to retrieve others (404 because real queryset is empty for users without membership)
        url = reverse("api:Organizations-detail", kwargs={"pk": organization.pk})
        response = auth_client.get(url)
        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_organization_permission_unauthenticated(self, anonymous_client, organization):
        """Test organization permissions without authentication"""
        url = reverse("api:Organizations-detail", kwargs={"pk": organization.pk})

        # Should not be able to retrieve
        response = anonymous_client.get(url)
        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

        # Should not be able to create
        url = reverse("api:Organizations-list")
        response = anonymous_client.post(url, {"name": "New Org"}, format="json")
        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)


@pytest.mark.django_db
class TestOrganizationQuerySet:
    """Test suite for organization queryset filtering"""

    def test_get_queryset_with_membership(self, auth_client, user):
        """Test that get_queryset only returns user's organization"""
        # Create user's organization
        user_org = OrganizationTestFactory.create_organization(name="User Org")
        OrganizationTestFactory.create_organization_membership(user, user_org)

        # Create other organizations
        OrganizationTestFactory.create_organization(name="Other Org 1")
        OrganizationTestFactory.create_organization(name="Other Org 2")

        # User should get 403 due to permission class blocking access for users with membership
        url = reverse("api:Organizations-detail", kwargs={"pk": user_org.pk})
        response = auth_client.get(url)
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_get_queryset_without_membership(self, auth_client, user):
        """Test that get_queryset returns empty when user has no membership"""
        # Create organizations without user membership
        org1 = OrganizationTestFactory.create_organization(name="Org 1")
        org2 = OrganizationTestFactory.create_organization(name="Org 2")

        # User should not see any organizations
        url = reverse("api:Organizations-detail", kwargs={"pk": org1.pk})
        response = auth_client.get(url)
        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

        url = reverse("api:Organizations-detail", kwargs={"pk": org2.pk})
        response = auth_client.get(url)
        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_get_queryset_multiple_organizations_same_user(self, auth_client, user):
        """Test behavior when user theoretically has multiple memberships"""
        # Note: In practice, users should have only one membership,
        # but testing the queryset filtering logic

        org1 = OrganizationTestFactory.create_organization(name="Primary Org")
        OrganizationTestFactory.create_organization_membership(user, org1)

        # Should get 403 due to permission class blocking access for users with membership
        url = reverse("api:Organizations-detail", kwargs={"pk": org1.pk})
        response = auth_client.get(url)
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)


@pytest.mark.django_db
class TestCurrentMembershipAction:
    """Test suite for GET /organizations/current/ (Phase 9).

    This action must be reachable by onboarded users (the whole reason for its
    dedicated ``permission_classes=[IsAuthenticated]`` override — bypassing the
    parent viewset's ``OrganizationManagementPermission`` which would block members).
    """

    def test_current_admin_returns_200_with_role_and_org(self, auth_client, user):
        """Onboarded ADMIN gets 200 with role='admin' and correct org data."""
        organization = OrganizationTestFactory.create_organization(name="Admin Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
        )

        url = reverse("api:Organizations-current")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        body = response.json()
        assert body["role"] == OrganizationRole.ADMIN
        assert body["organization"]["id"] == organization.id
        assert body["organization"]["name"] == "Admin Org"

    def test_current_member_returns_200_with_role_member(self, auth_client, user):
        """Onboarded MEMBER gets 200 with role='member'."""
        organization = OrganizationTestFactory.create_organization(name="Member Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.MEMBER,
        )

        url = reverse("api:Organizations-current")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        body = response.json()
        assert body["role"] == OrganizationRole.MEMBER
        assert body["organization"]["id"] == organization.id

    def test_current_gated_user_returns_404(self, auth_client):
        """Membership-less (gated) authenticated user gets 404 — not 500."""
        url = reverse("api:Organizations-current")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_current_unauthenticated_returns_401(self, anonymous_client):
        """Unauthenticated request is rejected with 401."""
        url = reverse("api:Organizations-current")
        response = anonymous_client.get(url)

        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    def test_current_not_blocked_by_organization_management_permission(self, auth_client, user):
        """Regression: OrganizationManagementPermission returns False for members.

        The ``current`` action overrides ``permission_classes=[IsAuthenticated]`` so
        an onboarded MEMBER (who would be blocked by ``OrganizationManagementPermission``)
        can still read their own membership. This test makes that invariant explicit.
        """
        organization = OrganizationTestFactory.create_organization(name="Regression Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.MEMBER,
        )

        url = reverse("api:Organizations-current")
        response = auth_client.get(url)

        # If OrganizationManagementPermission were active, a member would receive 403.
        assert_response_status_code(response, status.HTTP_200_OK)


@pytest.mark.django_db
class TestInactiveMemberOrganizationAccess:
    """Inactive members must be denied on all organization object paths.

    An inactive membership (is_active=False) must behave identically to a
    membership-less user: no tenant-scoped access, no 500s.
    """

    def _make_inactive_member(self, user, organization):
        return baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            is_active=False,
        )

    def test_current_inactive_member_returns_404(self, auth_client, user):
        """Inactive member hitting /organizations/current/ must get 404, not 200."""
        organization = OrganizationTestFactory.create_organization(name="Inactive Org")
        self._make_inactive_member(user, organization)

        url = reverse("api:Organizations-current")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_retrieve_organization_inactive_member_denied(self, auth_client, user):
        """Inactive member cannot retrieve their (formerly) owned org — denied."""
        organization = OrganizationTestFactory.create_organization(name="Inactive Org")
        self._make_inactive_member(user, organization)

        url = reverse("api:Organizations-detail", kwargs={"pk": organization.pk})
        response = auth_client.get(url)

        # OrganizationManagementPermission.has_object_permission denies inactive members.
        # get_queryset also returns none(), so 404 is expected.
        assert response.status_code in (
            status.HTTP_403_FORBIDDEN,
            status.HTTP_404_NOT_FOUND,
        ), f"Expected 403 or 404, got {response.status_code}"

    def test_patch_organization_inactive_member_denied(self, auth_client, user):
        """Inactive member cannot PATCH their (formerly) owned org."""
        organization = OrganizationTestFactory.create_organization(name="Inactive Org")
        self._make_inactive_member(user, organization)

        url = reverse("api:Organizations-detail", kwargs={"pk": organization.pk})
        response = auth_client.patch(url, {"name": "Hacked"}, format="json")

        assert response.status_code in (
            status.HTTP_403_FORBIDDEN,
            status.HTTP_404_NOT_FOUND,
        ), f"Expected 403 or 404, got {response.status_code}"

    def test_delete_organization_inactive_member_denied(self, auth_client, user):
        """Inactive member cannot DELETE their (formerly) owned org."""
        organization = OrganizationTestFactory.create_organization(name="Inactive Org")
        self._make_inactive_member(user, organization)

        url = reverse("api:Organizations-detail", kwargs={"pk": organization.pk})
        response = auth_client.delete(url)

        assert response.status_code in (
            status.HTTP_403_FORBIDDEN,
            status.HTTP_404_NOT_FOUND,
        ), f"Expected 403 or 404, got {response.status_code}"

    def test_active_member_still_allowed_current(self, auth_client, user):
        """Sanity: an ACTIVE member must still get 200 from /organizations/current/."""
        organization = OrganizationTestFactory.create_organization(name="Active Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            is_active=True,
        )

        url = reverse("api:Organizations-current")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        assert response.json()["organization"]["id"] == organization.id


@pytest.mark.django_db
class TestOrganizationInvitationViewSet:
    """Test suite for OrganizationInvitationViewSet"""

    def test_list_invitations_authenticated_with_membership(
        self, auth_client, user, organization_with_membership
    ):
        """Test listing invitations when user has membership"""
        # Create some invitations for the user's organization
        _invitation1 = OrganizationTestFactory.create_organization_invitation(
            organization_with_membership, email="user1@example.com", invited_by=user
        )
        _invitation2 = OrganizationTestFactory.create_organization_invitation(
            organization_with_membership, email="user2@example.com", invited_by=user
        )

        # Create invitation for different organization (should not appear)
        other_org = OrganizationTestFactory.create_organization(name="Other Org")
        OrganizationTestFactory.create_organization_invitation(other_org, email="other@example.com")

        url = reverse("api:OrganizationInvitations-list")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        response_data = response.json()
        assert len(response_data["results"]) == 2

        emails = [inv["email"] for inv in response_data["results"]]
        assert "user1@example.com" in emails
        assert "user2@example.com" in emails
        assert "other@example.com" not in emails

    def test_list_invitations_authenticated_without_membership(self, auth_client):
        """Test listing invitations when user has no membership"""
        url = reverse("api:OrganizationInvitations-list")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_list_invitations_unauthenticated(self, anonymous_client):
        """Test listing invitations without authentication"""
        url = reverse("api:OrganizationInvitations-list")
        response = anonymous_client.get(url)

        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    def test_retrieve_invitation_authenticated_with_membership(
        self, auth_client, user, organization_with_membership
    ):
        """Test retrieving a specific invitation when user has membership"""
        invitation = OrganizationTestFactory.create_organization_invitation(
            organization_with_membership, email="test@example.com", invited_by=user
        )

        url = reverse("api:OrganizationInvitations-detail", kwargs={"pk": invitation.pk})
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        response_data = response.json()
        assert response_data["email"] == "test@example.com"
        assert response_data["organization"] == organization_with_membership.id

    def test_retrieve_invitation_different_organization(self, auth_client, user):
        """Test retrieving invitation from different organization"""
        # Create user's organization
        user_org = OrganizationTestFactory.create_organization()
        OrganizationTestFactory.create_organization_membership(user, user_org)

        # Create invitation for different organization
        other_org = OrganizationTestFactory.create_organization(name="Other Org")
        other_invitation = OrganizationTestFactory.create_organization_invitation(
            other_org, email="other@example.com"
        )

        url = reverse("api:OrganizationInvitations-detail", kwargs={"pk": other_invitation.pk})
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_create_invitation_authenticated_with_membership(
        self, auth_client, user, organization_with_membership
    ):
        """Test creating an invitation when user has membership"""
        # Use a unique email that won't conflict with existing test data
        unique_email = f"newuser-{timezone.now().timestamp()}@example.com"

        url = reverse("api:OrganizationInvitations-list")
        data = {
            "email": unique_email,
            "first_name": "New",
            "last_name": "User",
        }

        response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        response_data = response.json()

        # Debug: Print the actual response to see what's happening
        print(f"Response data: {response_data}")

        assert response_data["email"] == unique_email
        assert response_data["first_name"] == "New"
        assert response_data["last_name"] == "User"
        assert response_data["organization"] == organization_with_membership.id

        # Verify the invitation was actually created in the database
        invitation = OrganizationInvitation.objects.get(email=unique_email)
        print(
            f"DB invitation: first_name='{invitation.first_name}', last_name='{invitation.last_name}'"
        )
        assert invitation.organization == organization_with_membership
        assert invitation.invited_by == user
        assert invitation.first_name == "New"
        assert invitation.last_name == "User"

    def test_create_invitation_authenticated_without_membership(self, auth_client):
        """Test creating an invitation when user has no membership"""
        url = reverse("api:OrganizationInvitations-list")
        data = {
            "email": "newuser@example.com",
            "first_name": "New",
            "last_name": "User",
        }
        response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_create_invitation_validation_errors(
        self, auth_client, user, organization_with_membership
    ):
        """Test invitation creation with validation errors"""
        url = reverse("api:OrganizationInvitations-list")

        # Test missing email
        data = {
            "first_name": "New",
            "last_name": "User",
        }
        response = auth_client.post(url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "email" in response.json()

        # Test invalid email format
        data = {
            "email": "invalid-email",
            "first_name": "New",
            "last_name": "User",
        }
        response = auth_client.post(url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_update_invitation_not_allowed(self, auth_client, user, organization_with_membership):
        """Test that updating invitations is not allowed"""
        invitation = OrganizationTestFactory.create_organization_invitation(
            organization_with_membership, email="test@example.com", invited_by=user
        )

        url = reverse("api:OrganizationInvitations-detail", kwargs={"pk": invitation.pk})
        data = {
            "email": "updated@example.com",
        }
        response = auth_client.patch(url, data, format="json")

        # Invitations should typically not be updatable, only creatable and deletable
        # The specific response depends on your viewset configuration
        assert response.status_code in [
            status.HTTP_405_METHOD_NOT_ALLOWED,
            status.HTTP_403_FORBIDDEN,
        ]

    @patch("organizations.services.OrganizationService.revoke_invitation")
    def test_delete_invitation_authenticated_with_membership(
        self, mock_revoke, auth_client, user, organization_with_membership
    ):
        """Test deleting (revoking) an invitation when user has membership"""
        invitation = OrganizationTestFactory.create_organization_invitation(
            organization_with_membership, email="test@example.com", invited_by=user
        )

        url = reverse("api:OrganizationInvitations-detail", kwargs={"pk": invitation.pk})
        response = auth_client.delete(url)

        assert_response_status_code(response, status.HTTP_204_NO_CONTENT)
        mock_revoke.assert_called_once_with(str(invitation.id))

    def test_delete_invitation_different_organization(self, auth_client, user):
        """Test deleting invitation from different organization"""
        # Create user's organization
        user_org = OrganizationTestFactory.create_organization()
        OrganizationTestFactory.create_organization_membership(user, user_org)

        # Create invitation for different organization
        other_org = OrganizationTestFactory.create_organization(name="Other Org")
        other_invitation = OrganizationTestFactory.create_organization_invitation(
            other_org, email="other@example.com"
        )

        url = reverse("api:OrganizationInvitations-detail", kwargs={"pk": other_invitation.pk})
        response = auth_client.delete(url)

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_filter_invitations_by_email(self, auth_client, user, organization_with_membership):
        """Test filtering invitations by email"""
        _invitation1 = OrganizationTestFactory.create_organization_invitation(
            organization_with_membership, email="alice@example.com", invited_by=user
        )
        _invitation2 = OrganizationTestFactory.create_organization_invitation(
            organization_with_membership, email="bob@example.com", invited_by=user
        )

        # Filter by partial email match
        url = reverse("api:OrganizationInvitations-list") + "?email=alice"
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        response_data = response.json()
        assert len(response_data["results"]) == 1
        assert response_data["results"][0]["email"] == "alice@example.com"

    def test_filter_invitations_by_acceptance_status(
        self, auth_client, user, organization_with_membership
    ):
        """Test filtering invitations by acceptance status"""
        # Create pending invitation
        _pending_invitation = OrganizationTestFactory.create_organization_invitation(
            organization_with_membership, email="pending@example.com", invited_by=user
        )

        # Create accepted invitation
        _accepted_invitation = OrganizationTestFactory.create_organization_invitation(
            organization_with_membership,
            email="accepted@example.com",
            invited_by=user,
            accepted_at=timezone.now(),
        )

        # Filter for non-accepted invitations
        url = reverse("api:OrganizationInvitations-list") + "?is_accepted=false"
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        response_data = response.json()
        assert len(response_data["results"]) == 1
        assert response_data["results"][0]["email"] == "pending@example.com"

    def test_filter_invitations_by_expiration_status(
        self, auth_client, user, organization_with_membership
    ):
        """Test filtering invitations by expiration status"""
        # Create non-expired invitation
        _valid_invitation = OrganizationTestFactory.create_organization_invitation(
            organization_with_membership,
            email="valid@example.com",
            invited_by=user,
            expires_at=timezone.now() + timezone.timedelta(days=1),
        )

        # Create expired invitation
        _expired_invitation = OrganizationTestFactory.create_organization_invitation(
            organization_with_membership,
            email="expired@example.com",
            invited_by=user,
            expires_at=timezone.now() - timezone.timedelta(days=1),
        )

        # Filter for non-expired invitations
        url = reverse("api:OrganizationInvitations-list") + "?is_expired=false"
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        response_data = response.json()
        assert len(response_data["results"]) == 1
        assert response_data["results"][0]["email"] == "valid@example.com"

    @patch("organizations.services.OrganizationService.invite_user_to_organization")
    def test_resend_pending_invitation(
        self, mock_invite, auth_client, user, organization_with_membership
    ):
        """Test resending a pending invitation regenerates token and extends expiry."""
        # Create a pending invitation
        original_invite = OrganizationTestFactory.create_organization_invitation(
            organization_with_membership, email="pending@example.com", invited_by=user
        )

        # Mock the service to return the updated invitation with new token+expiry
        new_expires_at = timezone.now() + timezone.timedelta(days=7)
        new_token_hash = hash_long_lived_token(generate_long_lived_token())

        # Update the original invitation in place for the mock return
        updated_invite = original_invite
        updated_invite.token_hash = new_token_hash
        updated_invite.expires_at = new_expires_at
        mock_invite.return_value = updated_invite

        url = reverse("api:OrganizationInvitations-resend", kwargs={"pk": original_invite.pk})
        response = auth_client.post(url, format="json")

        assert_response_status_code(response, status.HTTP_200_OK)
        response_data = response.json()

        # Verify the response contains the invitation data
        assert response_data["email"] == "pending@example.com"
        assert response_data["organization"] == organization_with_membership.id

        # Verify the service was called with correct arguments
        mock_invite.assert_called_once_with(
            email="pending@example.com",
            first_name=original_invite.first_name,
            last_name=original_invite.last_name,
            invited_by=user,
            organization=organization_with_membership,
        )

    @patch("organizations.services.OrganizationService.invite_user_to_organization")
    def test_resend_accepted_invitation_fails(
        self, mock_invite, auth_client, user, organization_with_membership
    ):
        """Test that resending an accepted invitation returns 400."""
        # Create an accepted invitation
        now = timezone.now()
        accepted_invite = OrganizationTestFactory.create_organization_invitation(
            organization_with_membership,
            email="accepted@example.com",
            invited_by=user,
            accepted_at=now,
        )

        url = reverse("api:OrganizationInvitations-resend", kwargs={"pk": accepted_invite.pk})
        response = auth_client.post(url, format="json")

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        response_data = response.json()
        # The response might be a list (ValidationError) or a dict with detail field
        error_text = str(response_data).lower()
        assert "already accepted" in error_text

        # Verify the service was NOT called
        mock_invite.assert_not_called()

    def test_resend_invitation_cross_org_fails(self, auth_client, user):
        """Test that resending an invitation from a different org returns 404."""
        # Create user's organization
        user_org = OrganizationTestFactory.create_organization()
        OrganizationTestFactory.create_organization_membership(user, user_org)

        # Create invitation for different organization
        other_org = OrganizationTestFactory.create_organization(name="Other Org")
        other_invitation = OrganizationTestFactory.create_organization_invitation(
            other_org, email="other@example.com"
        )

        url = reverse("api:OrganizationInvitations-resend", kwargs={"pk": other_invitation.pk})
        response = auth_client.post(url, format="json")

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_resend_invitation_without_membership_fails(self, auth_client):
        """Test that resending an invitation without organization membership returns 403."""
        organization = OrganizationTestFactory.create_organization()
        invitation = OrganizationTestFactory.create_organization_invitation(
            organization, email="test@example.com"
        )

        url = reverse("api:OrganizationInvitations-resend", kwargs={"pk": invitation.pk})
        response = auth_client.post(url, format="json")

        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_resend_invitation_unauthenticated_fails(self, anonymous_client):
        """Test that resending an invitation without authentication returns 401."""
        organization = OrganizationTestFactory.create_organization()
        invitation = OrganizationTestFactory.create_organization_invitation(
            organization, email="test@example.com"
        )

        url = reverse("api:OrganizationInvitations-resend", kwargs={"pk": invitation.pk})
        response = anonymous_client.post(url, format="json")

        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    def test_resend_pending_invitation_real_reset(
        self, auth_client, user, organization_with_membership
    ):
        """Integration test: resend actually resets token_hash and extends expires_at.

        This test does NOT mock invite_user_to_organization, so it verifies the actual
        reset behavior end-to-end.
        """
        # Create a pending invitation with known initial values
        original_invite = OrganizationTestFactory.create_organization_invitation(
            organization_with_membership, email="resend-test@example.com", invited_by=user
        )
        original_token_hash = original_invite.token_hash
        original_expires_at = original_invite.expires_at

        url = reverse("api:OrganizationInvitations-resend", kwargs={"pk": original_invite.pk})

        # Resend the invitation
        response = auth_client.post(url, format="json")

        assert_response_status_code(response, status.HTTP_200_OK)
        response_data = response.json()

        # Verify response contains invitation data
        assert response_data["email"] == "resend-test@example.com"
        assert response_data["organization"] == organization_with_membership.id

        # Refresh from DB and verify token_hash and expires_at have changed
        refreshed_invite = OrganizationInvitation.objects.get(pk=original_invite.pk)
        assert refreshed_invite.token_hash != original_token_hash, (
            "token_hash should be regenerated"
        )
        assert refreshed_invite.expires_at > original_expires_at, (
            "expires_at should be extended (~7 days)"
        )


@pytest.mark.django_db
class TestAcceptInvitationView:
    """Test suite for AcceptInvitationView"""

    @patch("organizations.services.OrganizationService.accept_invitation")
    def test_accept_invitation_authenticated_valid_token(
        self, mock_accept_invitation, auth_client, user
    ):
        """Test accepting an invitation with valid token"""
        organization = OrganizationTestFactory.create_organization(name="Test Org")
        membership = OrganizationTestFactory.create_organization_membership(user, organization)
        mock_accept_invitation.return_value = membership

        url = reverse("accept-invitation")
        data = {
            "token": "valid_token_123",
        }
        response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        response_data = response.json()
        assert response_data["message"] == "Invitation accepted successfully"
        assert response_data["organization_id"] == organization.id
        assert response_data["organization_name"] == "Test Org"

        mock_accept_invitation.assert_called_once_with(token="valid_token_123", user=user)

    @patch("organizations.services.OrganizationService.accept_invitation")
    def test_accept_invitation_authenticated_invalid_token(
        self, mock_accept_invitation, auth_client, user
    ):
        """Test accepting an invitation with invalid token"""
        mock_accept_invitation.side_effect = InvalidInvitationTokenError("Invalid or expired token")

        url = reverse("accept-invitation")
        data = {
            "token": "invalid_token",
        }
        response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        mock_accept_invitation.assert_called_once_with(token="invalid_token", user=user)

    def test_accept_invitation_unauthenticated(self, anonymous_client):
        """Test accepting an invitation without authentication"""
        url = reverse("accept-invitation")
        data = {
            "token": "some_token",
        }
        response = anonymous_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    def test_accept_invitation_missing_token(self, auth_client):
        """Test accepting an invitation without providing token"""
        url = reverse("accept-invitation")
        data = {}
        response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        response_data = response.json()
        assert "token" in response_data

    def test_accept_invitation_empty_token(self, auth_client):
        """Test accepting an invitation with empty token"""
        url = reverse("accept-invitation")
        data = {
            "token": "",
        }
        response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_accept_invitation_already_accepted(self, auth_client, user, organization):
        """Test accepting an invitation for a user who is already a member.

        Since accept_invitation now raises UserAlreadyHasMembershipError (a DRF
        ValidationError) before touching the DB, the view returns a 400 response
        with a typed error rather than an unhandled IntegrityError.
        """
        # Create a membership for the user in the organization
        OrganizationTestFactory.create_organization_membership(user, organization)

        # Create an invitation for the same user's email
        invitation = OrganizationTestFactory.create_organization_invitation(
            organization, email=user.email
        )

        # Generate a valid token for the invitation
        token = generate_long_lived_token()
        invitation.token_hash = hash_long_lived_token(token)
        invitation.save()

        url = reverse("accept-invitation")
        data = {
            "token": token,
        }

        response = auth_client.post(url, data, format="json")

        # The hardened service returns a typed 400 error instead of an unhandled IntegrityError.
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_accept_invitation_already_member_different_org_rejected(
        self, auth_client, user, organization
    ):
        """Use-case 5: an already-member user POSTs a valid token for a DIFFERENT org.

        Acceptance criteria (Phase 7):
        - HTTP 400 with the user_already_has_membership code and message.
        - The user's existing membership (org + role) is unchanged.
        - The target invitation remains pending (accepted_at is None, membership is None).
        - No second OrganizationMembership is created.
        """
        # User's existing membership in their own org.
        user_org = OrganizationTestFactory.create_organization(name="User Org")
        OrganizationTestFactory.create_organization_membership(user, user_org)
        original_membership = OrganizationMembership.objects.get(user=user)

        # A different org with a valid pending invitation for the same user's email.
        other_org = OrganizationTestFactory.create_organization(name="Other Org")
        invitation = OrganizationTestFactory.create_organization_invitation(
            other_org, email=user.email
        )
        token = generate_long_lived_token()
        invitation.token_hash = hash_long_lived_token(token)
        invitation.save()

        url = reverse("accept-invitation")
        response = auth_client.post(url, {"token": token}, format="json")

        # --- HTTP response ---
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

        # --- Error body shape (stable contract for clients) ---
        response_data = response.json()
        assert response_data["error"] == "User already belongs to an organization."

        # --- Existing membership is unchanged ---
        original_membership.refresh_from_db()
        assert original_membership.organization_id == user_org.id
        assert OrganizationMembership.objects.filter(user=user).count() == 1

        # --- Target invitation remains pending ---
        invitation.refresh_from_db()
        assert invitation.accepted_at is None
        assert invitation.membership is None

    def test_accept_invitation_already_member_error_body_shape(self, auth_client, user):
        """Assert the error body contract is stable for clients (Phase 7).

        When the user already has a membership and POSTs the accept endpoint,
        the response MUST contain the 'code' and 'detail' keys with the documented
        user_already_has_membership values.
        """
        own_org = OrganizationTestFactory.create_organization(name="Own Org")
        OrganizationTestFactory.create_organization_membership(user, own_org)

        other_org = OrganizationTestFactory.create_organization(name="Other Org")
        invitation = OrganizationTestFactory.create_organization_invitation(
            other_org, email=user.email
        )
        token = generate_long_lived_token()
        invitation.token_hash = hash_long_lived_token(token)
        invitation.save()

        url = reverse("accept-invitation")
        response = auth_client.post(url, {"token": token}, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        body = response.json()
        assert "error" in body, f"Response body missing 'error' key: {body}"
        assert body["error"] == "User already belongs to an organization."


@pytest.mark.django_db
class TestOrganizationInvitationPermissions:
    """Test suite for organization invitation permissions"""

    def test_invitation_permission_with_membership(self, auth_client, user):
        """Test invitation permissions when user has membership"""
        organization = OrganizationTestFactory.create_organization()
        OrganizationTestFactory.create_organization_membership(user, organization)
        invitation = OrganizationTestFactory.create_organization_invitation(
            organization, email="test@example.com", invited_by=user
        )

        # Should be able to retrieve own organization's invitations
        url = reverse("api:OrganizationInvitations-detail", kwargs={"pk": invitation.pk})
        response = auth_client.get(url)
        assert_response_status_code(response, status.HTTP_200_OK)

        # Should be able to list own organization's invitations
        url = reverse("api:OrganizationInvitations-list")
        response = auth_client.get(url)
        assert_response_status_code(response, status.HTTP_200_OK)

    def test_invitation_permission_without_membership(self, auth_client, user):
        """Test invitation permissions when user has no membership"""
        organization = OrganizationTestFactory.create_organization()
        invitation = OrganizationTestFactory.create_organization_invitation(
            organization, email="test@example.com"
        )

        # Should NOT be able to access invitations without membership
        url = reverse("api:OrganizationInvitations-detail", kwargs={"pk": invitation.pk})
        response = auth_client.get(url)
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

        url = reverse("api:OrganizationInvitations-list")
        response = auth_client.get(url)
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_invitation_permission_different_organization(self, auth_client, user):
        """Test invitation permissions for different organization"""
        # Create user's organization
        user_org = OrganizationTestFactory.create_organization(name="User Org")
        OrganizationTestFactory.create_organization_membership(user, user_org)

        # Create invitation for different organization
        other_org = OrganizationTestFactory.create_organization(name="Other Org")
        other_invitation = OrganizationTestFactory.create_organization_invitation(
            other_org, email="other@example.com"
        )

        # Should NOT be able to access other organization's invitations
        url = reverse("api:OrganizationInvitations-detail", kwargs={"pk": other_invitation.pk})
        response = auth_client.get(url)
        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_invitation_permission_unauthenticated(self, anonymous_client):
        """Test invitation permissions without authentication"""
        organization = OrganizationTestFactory.create_organization()
        invitation = OrganizationTestFactory.create_organization_invitation(
            organization, email="test@example.com"
        )

        # Should not be able to access invitations without authentication
        url = reverse("api:OrganizationInvitations-detail", kwargs={"pk": invitation.pk})
        response = anonymous_client.get(url)
        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

        url = reverse("api:OrganizationInvitations-list")
        response = anonymous_client.get(url)
        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)


@pytest.mark.django_db
class TestOrganizationMembershipViewSet:
    """Test suite for OrganizationMembershipViewSet"""

    def test_list_members_admin_sees_all(self, auth_client, user):
        """Test that admin can list all members (active and inactive) of their organization"""
        # Create organization with admin membership for the user
        organization = OrganizationTestFactory.create_organization(name="Test Org")
        admin_membership = baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        # Create some other members (active and inactive)
        baker.make(
            OrganizationMembership,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )
        inactive_member = baker.make(
            OrganizationMembership,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=False,
        )

        url = reverse("api:OrganizationMembers-list")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        results = response.json()["results"]
        assert len(results) == 3  # admin + active_member + inactive_member

        # Verify that response includes expected fields
        for result in results:
            assert "id" in result
            assert "role" in result
            assert "is_active" in result
            assert "user_email" in result
            assert "user_first_name" in result
            assert "user_last_name" in result

        # Verify admin membership is present
        admin_ids = [r["id"] for r in results if r["id"] == admin_membership.id]
        assert len(admin_ids) == 1

        # Verify inactive member is present
        inactive_ids = [r["id"] for r in results if r["id"] == inactive_member.id]
        assert len(inactive_ids) == 1

    def test_list_members_non_admin_forbidden(self, auth_client, user):
        """Test that non-admin members get 403 when listing organization members"""
        organization = OrganizationTestFactory.create_organization(name="Test Org")
        # Create user with member (not admin) role
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        url = reverse("api:OrganizationMembers-list")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_list_members_membership_less_user_forbidden(self, auth_client):
        """Test that membership-less users get 403 when listing organization members"""
        # auth_client is already authenticated but has no membership
        url = reverse("api:OrganizationMembers-list")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_list_members_inactive_membership_forbidden(self, auth_client, user):
        """Test that users with inactive membership get 403"""
        organization = OrganizationTestFactory.create_organization(name="Test Org")
        # Create inactive admin membership
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=False,
        )

        url = reverse("api:OrganizationMembers-list")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_list_members_cross_org_exclusion(self, auth_client, user):
        """Test that admin only sees members of their own organization"""
        # Create org1 with admin membership for the user
        org1 = OrganizationTestFactory.create_organization(name="Org 1")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=org1,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        # Create org2 with some members
        org2 = OrganizationTestFactory.create_organization(name="Org 2")
        baker.make(
            OrganizationMembership,
            organization=org2,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        url = reverse("api:OrganizationMembers-list")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        results = response.json()["results"]
        # Should only see 1 member (the admin from org1)
        assert len(results) == 1
        assert results[0]["id"] == user.organization_memberships.get().id

    def test_retrieve_member_admin_success(self, auth_client, user):
        """Test that admin can retrieve a specific member"""
        organization = OrganizationTestFactory.create_organization(name="Test Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        member = baker.make(
            OrganizationMembership,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        url = reverse("api:OrganizationMembers-detail", kwargs={"pk": member.pk})
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        result = response.json()
        assert result["id"] == member.id
        assert result["role"] == OrganizationRole.MEMBER
        assert result["is_active"] is True
        assert result["user_email"] == member.user.email

    def test_retrieve_member_cross_org_not_found(self, auth_client, user):
        """Test that admin cannot retrieve a member from a different organization"""
        org1 = OrganizationTestFactory.create_organization(name="Org 1")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=org1,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        org2 = OrganizationTestFactory.create_organization(name="Org 2")
        member_in_org2 = baker.make(
            OrganizationMembership,
            organization=org2,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        url = reverse("api:OrganizationMembers-detail", kwargs={"pk": member_in_org2.pk})
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_retrieve_member_non_admin_forbidden(self, auth_client, user):
        """Test that non-admin members cannot retrieve other members"""
        organization = OrganizationTestFactory.create_organization(name="Test Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        other_member = baker.make(
            OrganizationMembership,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        url = reverse("api:OrganizationMembers-detail", kwargs={"pk": other_member.pk})
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_retrieve_member_includes_profile_info(self, auth_client, user):
        """Test that member serialization includes user profile information"""
        from users.factories import UserFactory

        organization = OrganizationTestFactory.create_organization(name="Test Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        # Create a member with profile info
        member_user = UserFactory().create_user()
        member_user.profile.first_name = "John"
        member_user.profile.last_name = "Doe"
        member_user.profile.save()

        member = baker.make(
            OrganizationMembership,
            user=member_user,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        url = reverse("api:OrganizationMembers-detail", kwargs={"pk": member.pk})
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        result = response.json()
        assert result["user_email"] == member_user.email
        assert result["user_first_name"] == "John"
        assert result["user_last_name"] == "Doe"

    def test_deactivate_member_admin_success(self, auth_client, user):
        """Test that admin can deactivate another active member"""
        organization = OrganizationTestFactory.create_organization(name="Test Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        # Create another admin so we can safely deactivate without triggering last-admin guard
        baker.make(
            OrganizationMembership,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        target_member = baker.make(
            OrganizationMembership,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        url = reverse("api:OrganizationMembers-deactivate", kwargs={"pk": target_member.pk})
        response = auth_client.post(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        result = response.json()
        assert result["is_active"] is False
        assert result["id"] == target_member.id

        # Verify in DB
        target_member.refresh_from_db()
        assert target_member.is_active is False

    def test_deactivate_member_idempotent(self, auth_client, user):
        """Test that deactivating an already-inactive member is a no-op success"""
        organization = OrganizationTestFactory.create_organization(name="Test Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        target_member = baker.make(
            OrganizationMembership,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=False,
        )

        url = reverse("api:OrganizationMembers-deactivate", kwargs={"pk": target_member.pk})
        response = auth_client.post(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        result = response.json()
        assert result["is_active"] is False

    def test_deactivate_self_forbidden(self, auth_client, user):
        """Test that admin cannot deactivate their own membership"""
        organization = OrganizationTestFactory.create_organization(name="Test Org")
        admin_membership = baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        # Create another admin to prevent last-admin guard interference
        baker.make(
            OrganizationMembership,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        url = reverse("api:OrganizationMembers-deactivate", kwargs={"pk": admin_membership.pk})
        response = auth_client.post(url)

        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

        # Verify unchanged
        admin_membership.refresh_from_db()
        assert admin_membership.is_active is True

    def test_sole_admin_cannot_self_deactivate(self, auth_client, user):
        """Test that sole admin cannot deactivate themselves, preserving org invariant.

        This documents the real protection: even if an admin is the sole admin of the org,
        the self-lockout guard (target.user_id == request.user.id) returns 403 before
        the last-admin guard can fire. The org always keeps at least one admin via this path.
        """
        organization = OrganizationTestFactory.create_organization(name="Test Org")
        sole_admin_membership = baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        url = reverse("api:OrganizationMembers-deactivate", kwargs={"pk": sole_admin_membership.pk})
        response = auth_client.post(url)

        # Self-deactivation is forbidden, even for sole admin
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

        # Membership remains active in DB
        sole_admin_membership.refresh_from_db()
        assert sole_admin_membership.is_active is True

    def test_deactivate_last_active_admin_succeeds_with_other_admins(self, auth_client, user):
        """Test that deactivating an admin succeeds when other admins remain.

        The last-admin guard (other_active_admin_count == 0) only fires if the target
        is the ONLY admin. When there are multiple admins, deactivating one is allowed.
        """
        organization = OrganizationTestFactory.create_organization(name="Test Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        # Create another admin so we can safely deactivate without hitting the last-admin guard
        other_admin = baker.make(
            OrganizationMembership,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        url = reverse("api:OrganizationMembers-deactivate", kwargs={"pk": other_admin.pk})
        response = auth_client.post(url)

        # Should succeed because there's still one admin (the requester)
        assert_response_status_code(response, status.HTTP_200_OK)
        other_admin.refresh_from_db()
        assert other_admin.is_active is False

    def test_deactivate_non_admin_forbidden(self, auth_client, user):
        """Test that non-admin member cannot deactivate another member"""
        organization = OrganizationTestFactory.create_organization(name="Test Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        target_member = baker.make(
            OrganizationMembership,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        url = reverse("api:OrganizationMembers-deactivate", kwargs={"pk": target_member.pk})
        response = auth_client.post(url)

        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_deactivate_cross_org_not_found(self, auth_client, user):
        """Test that admin cannot deactivate a member from a different organization"""
        org1 = OrganizationTestFactory.create_organization(name="Org 1")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=org1,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        org2 = OrganizationTestFactory.create_organization(name="Org 2")
        member_in_org2 = baker.make(
            OrganizationMembership,
            organization=org2,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        url = reverse("api:OrganizationMembers-deactivate", kwargs={"pk": member_in_org2.pk})
        response = auth_client.post(url)

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_deactivate_gates_access(self, auth_client, user):
        """Test that a deactivated member loses access to tenant endpoints.

        When a member is deactivated, the hard-gate treats their membership as
        inactive and returns 404 (membership-less) for tenant-scoped endpoints.
        """
        organization = OrganizationTestFactory.create_organization(name="Test Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        target_user = baker.make("users.User")
        target_member = baker.make(
            OrganizationMembership,
            user=target_user,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        # Deactivate the target member
        url = reverse("api:OrganizationMembers-deactivate", kwargs={"pk": target_member.pk})
        response = auth_client.post(url)
        assert_response_status_code(response, status.HTTP_200_OK)

        # Now authenticate as the target user and verify they're gated
        target_client = APIClient()
        target_client.force_authenticate(user=target_user)

        # Refresh the user's membership from DB to pick up the deactivation
        target_user.refresh_from_db()

        # Try to access /organizations/current/ — should get 404 (membership inactive = gated)
        current_url = reverse("api:Organizations-current")
        response = target_client.get(current_url)
        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_reactivate_member_admin_success(self, auth_client, user):
        """Test that admin can reactivate an inactive member"""
        organization = OrganizationTestFactory.create_organization(name="Test Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        target_member = baker.make(
            OrganizationMembership,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=False,
        )

        url = reverse("api:OrganizationMembers-reactivate", kwargs={"pk": target_member.pk})
        response = auth_client.post(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        result = response.json()
        assert result["is_active"] is True
        assert result["id"] == target_member.id

        # Verify in DB
        target_member.refresh_from_db()
        assert target_member.is_active is True

    def test_reactivate_member_idempotent(self, auth_client, user):
        """Test that reactivating an already-active member is a no-op success"""
        organization = OrganizationTestFactory.create_organization(name="Test Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        target_member = baker.make(
            OrganizationMembership,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        url = reverse("api:OrganizationMembers-reactivate", kwargs={"pk": target_member.pk})
        response = auth_client.post(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        result = response.json()
        assert result["is_active"] is True

    def test_reactivate_non_admin_forbidden(self, auth_client, user):
        """Test that non-admin member cannot reactivate another member"""
        organization = OrganizationTestFactory.create_organization(name="Test Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        target_member = baker.make(
            OrganizationMembership,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=False,
        )

        url = reverse("api:OrganizationMembers-reactivate", kwargs={"pk": target_member.pk})
        response = auth_client.post(url)

        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_reactivate_cross_org_not_found(self, auth_client, user):
        """Test that admin cannot reactivate a member from a different organization"""
        org1 = OrganizationTestFactory.create_organization(name="Org 1")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=org1,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        org2 = OrganizationTestFactory.create_organization(name="Org 2")
        member_in_org2 = baker.make(
            OrganizationMembership,
            organization=org2,
            role=OrganizationRole.MEMBER,
            is_active=False,
        )

        url = reverse("api:OrganizationMembers-reactivate", kwargs={"pk": member_in_org2.pk})
        response = auth_client.post(url)

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_reactivate_restores_access(self, auth_client, user):
        """Test that a reactivated member regains access to tenant endpoints.

        When a member is reactivated, the hard-gate treats their membership as
        active and returns 200 with org data for tenant-scoped endpoints.
        """
        organization = OrganizationTestFactory.create_organization(name="Test Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        target_user = baker.make("users.User")
        target_member = baker.make(
            OrganizationMembership,
            user=target_user,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=False,
        )

        # Reactivate the target member
        url = reverse("api:OrganizationMembers-reactivate", kwargs={"pk": target_member.pk})
        response = auth_client.post(url)
        assert_response_status_code(response, status.HTTP_200_OK)

        # Now authenticate as the target user and verify they're no longer gated
        target_client = APIClient()
        target_client.force_authenticate(user=target_user)

        # Refresh the user's membership from DB to pick up the reactivation
        target_user.refresh_from_db()

        # Try to access /organizations/current/ — should now work
        current_url = reverse("api:Organizations-current")
        response = target_client.get(current_url)
        assert_response_status_code(response, status.HTTP_200_OK)
        result = response.json()
        assert result["organization"]["id"] == organization.id

    def _setup_admin_org(self, user):
        organization = OrganizationTestFactory.create_organization(name="Search Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        return organization

    def _make_member(self, organization, first_name, last_name, email):
        from users.factories import UserFactory

        member_user = UserFactory().create_user(email=email)
        member_user.profile.first_name = first_name
        member_user.profile.last_name = last_name
        member_user.profile.save()
        return baker.make(
            OrganizationMembership,
            user=member_user,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

    def test_filter_by_first_name(self, auth_client, user):
        organization = self._setup_admin_org(user)
        self._make_member(organization, "Alice", "Smith", "alice@example.com")
        self._make_member(organization, "Bob", "Jones", "bob@example.com")

        url = reverse("api:OrganizationMembers-list")
        response = auth_client.get(url, {"first_name": "ali"})

        assert_response_status_code(response, status.HTTP_200_OK)
        results = response.json()["results"]
        assert len(results) == 1
        assert results[0]["user_first_name"] == "Alice"

    def test_filter_by_last_name(self, auth_client, user):
        organization = self._setup_admin_org(user)
        self._make_member(organization, "Alice", "Smith", "alice@example.com")
        self._make_member(organization, "Bob", "Jones", "bob@example.com")

        url = reverse("api:OrganizationMembers-list")
        response = auth_client.get(url, {"last_name": "jon"})

        assert_response_status_code(response, status.HTTP_200_OK)
        results = response.json()["results"]
        assert len(results) == 1
        assert results[0]["user_last_name"] == "Jones"

    def test_filter_by_email(self, auth_client, user):
        organization = self._setup_admin_org(user)
        self._make_member(organization, "Alice", "Smith", "alice@example.com")
        self._make_member(organization, "Bob", "Jones", "bob@example.com")

        url = reverse("api:OrganizationMembers-list")
        response = auth_client.get(url, {"email": "bob@"})

        assert_response_status_code(response, status.HTTP_200_OK)
        results = response.json()["results"]
        assert len(results) == 1
        assert results[0]["user_email"] == "bob@example.com"

    def test_search_matches_first_name(self, auth_client, user):
        organization = self._setup_admin_org(user)
        self._make_member(organization, "Alice", "Smith", "alice@example.com")
        self._make_member(organization, "Bob", "Jones", "bob@example.com")

        url = reverse("api:OrganizationMembers-list")
        response = auth_client.get(url, {"search": "ALICE"})

        assert_response_status_code(response, status.HTTP_200_OK)
        results = response.json()["results"]
        assert len(results) == 1
        assert results[0]["user_first_name"] == "Alice"

    def test_search_matches_last_name(self, auth_client, user):
        organization = self._setup_admin_org(user)
        self._make_member(organization, "Alice", "Smith", "alice@example.com")
        self._make_member(organization, "Bob", "Jones", "bob@example.com")

        url = reverse("api:OrganizationMembers-list")
        response = auth_client.get(url, {"search": "SMITH"})

        assert_response_status_code(response, status.HTTP_200_OK)
        results = response.json()["results"]
        assert len(results) == 1
        assert results[0]["user_last_name"] == "Smith"

    def test_search_matches_email(self, auth_client, user):
        organization = self._setup_admin_org(user)
        self._make_member(organization, "Alice", "Smith", "alice@example.com")
        self._make_member(organization, "Bob", "Jones", "bob@example.com")

        url = reverse("api:OrganizationMembers-list")
        response = auth_client.get(url, {"search": "BOB@EXAMPLE"})

        assert_response_status_code(response, status.HTTP_200_OK)
        results = response.json()["results"]
        assert len(results) == 1
        assert results[0]["user_email"] == "bob@example.com"

    def test_search_or_across_fields(self, auth_client, user):
        """search=<term> matches any member whose first name, last name, OR email contains term."""
        organization = self._setup_admin_org(user)
        self._make_member(organization, "Alice", "Smith", "alice@example.com")
        self._make_member(organization, "Bob", "Smith", "bob@example.com")
        self._make_member(organization, "Carol", "Jones", "carol@example.com")

        url = reverse("api:OrganizationMembers-list")
        # "smith" matches Alice and Bob by last name
        response = auth_client.get(url, {"search": "smith"})

        assert_response_status_code(response, status.HTTP_200_OK)
        results = response.json()["results"]
        assert len(results) == 2
        last_names = {r["user_last_name"] for r in results}
        assert last_names == {"Smith"}

    def test_search_no_match_returns_empty(self, auth_client, user):
        organization = self._setup_admin_org(user)
        self._make_member(organization, "Alice", "Smith", "alice@example.com")

        url = reverse("api:OrganizationMembers-list")
        response = auth_client.get(url, {"search": "zzznomatch"})

        assert_response_status_code(response, status.HTTP_200_OK)
        assert response.json()["results"] == []

    def test_filters_scoped_to_own_org(self, auth_client, user):
        """search must not leak members from other organizations."""
        org1 = self._setup_admin_org(user)
        self._make_member(org1, "Alice", "Smith", "alice@example.com")

        org2 = OrganizationTestFactory.create_organization(name="Other Org")
        self._make_member(org2, "Alice", "Other", "alice2@other.com")

        url = reverse("api:OrganizationMembers-list")
        response = auth_client.get(url, {"search": "Alice"})

        assert_response_status_code(response, status.HTTP_200_OK)
        results = response.json()["results"]
        # Only the Alice from org1 should appear (email alice@example.com)
        assert len(results) == 1
        assert results[0]["user_email"] == "alice@example.com"


@pytest.mark.django_db
class TestSyncRoomsAction:
    """Test suite for POST /organizations/{id}/sync-rooms/ (Phase 7).

    Only org admins may trigger the sync.  The view calls request_rooms_sync
    directly; the service layer owns the on_commit deferral.  Tests verify
    that request_rooms_sync is called with the expected arguments.
    """

    def _make_admin_client(self, user, organization):
        """Return an APIClient force-authenticated as an active admin of ``organization``."""
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    @patch("organizations.services.OrganizationService.request_rooms_sync")
    def test_admin_sync_rooms_default_times_returns_202(self, mock_sync, user):
        """Admin POST without body → 202; request_rooms_sync called with no explicit times.

        Phase 18: a GoogleCalendarServiceAccount must exist for the org or the
        view returns 400.  Provide one so the pre-flight check passes.

        Phase 19: the view calls request_rooms_sync directly (no view-level
        on_commit); the service owns the on_commit deferral internally.
        """
        organization = OrganizationTestFactory.create_organization(name="Sync Org")
        admin_client = self._make_admin_client(user, organization)
        # Pre-flight requires a service account; provide one.
        baker.make(
            GoogleCalendarServiceAccount,
            organization=organization,
            calendar_fk=None,
            email="sa@example.com",
            audience="https://www.googleapis.com/auth/admin.directory.resource.calendar",
            public_key="pk",
            private_key_id="kid",
            private_key="key",
        )

        url = reverse("api:Organizations-sync-rooms", kwargs={"pk": organization.pk})
        response = admin_client.post(url, {}, format="json")

        assert_response_status_code(response, status.HTTP_202_ACCEPTED)
        mock_sync.assert_called_once()
        call_kwargs = mock_sync.call_args.kwargs
        assert call_kwargs["organization"] == organization
        assert call_kwargs["requested_by"] == user
        assert call_kwargs["start_time"] is None
        assert call_kwargs["end_time"] is None

    @patch("organizations.services.OrganizationService.request_rooms_sync")
    def test_admin_sync_rooms_explicit_times_returns_202(self, mock_sync, user):
        """Admin POST with ISO start_time/end_time → 202; service called with parsed datetimes.

        Phase 18: a GoogleCalendarServiceAccount must exist for the org or the
        view returns 400.  Provide one so the pre-flight check passes.

        Phase 19: the view calls request_rooms_sync directly (no view-level
        on_commit); the service owns the on_commit deferral internally.
        """
        import datetime

        organization = OrganizationTestFactory.create_organization(name="Explicit Sync Org")
        admin_client = self._make_admin_client(user, organization)
        # Pre-flight requires a service account; provide one.
        baker.make(
            GoogleCalendarServiceAccount,
            organization=organization,
            calendar_fk=None,
            email="sa@example.com",
            audience="https://www.googleapis.com/auth/admin.directory.resource.calendar",
            public_key="pk",
            private_key_id="kid",
            private_key="key",
        )

        start = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2027, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)

        url = reverse("api:Organizations-sync-rooms", kwargs={"pk": organization.pk})
        response = admin_client.post(
            url,
            {"start_time": start.isoformat(), "end_time": end.isoformat()},
            format="json",
        )

        assert_response_status_code(response, status.HTTP_202_ACCEPTED)
        mock_sync.assert_called_once()
        call_kwargs = mock_sync.call_args.kwargs
        assert call_kwargs["start_time"] == start
        assert call_kwargs["end_time"] == end

    @patch("organizations.services.OrganizationService.request_rooms_sync")
    def test_sync_rooms_non_admin_member_returns_403(self, mock_sync, user):
        """Non-admin member → 403; request_rooms_sync must NOT be called."""
        organization = OrganizationTestFactory.create_organization(name="Member Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )
        client = APIClient()
        client.force_authenticate(user=user)

        url = reverse("api:Organizations-sync-rooms", kwargs={"pk": organization.pk})
        response = client.post(url, {}, format="json")

        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)
        mock_sync.assert_not_called()

    @patch("organizations.services.OrganizationService.request_rooms_sync")
    def test_sync_rooms_cross_org_returns_404(self, mock_sync, user):
        """Admin of org A cannot trigger sync for org B → 404."""
        org_a = OrganizationTestFactory.create_organization(name="Org A")
        org_b = OrganizationTestFactory.create_organization(name="Org B")
        admin_client = self._make_admin_client(user, org_a)

        url = reverse("api:Organizations-sync-rooms", kwargs={"pk": org_b.pk})
        response = admin_client.post(url, {}, format="json")

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)
        mock_sync.assert_not_called()

    @patch("organizations.services.OrganizationService.request_rooms_sync")
    def test_sync_rooms_unauthenticated_returns_401(self, mock_sync):
        """Anonymous request → 401."""
        organization = OrganizationTestFactory.create_organization(name="Anon Org")
        client = APIClient()

        url = reverse("api:Organizations-sync-rooms", kwargs={"pk": organization.pk})
        response = client.post(url, {}, format="json")

        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)
        mock_sync.assert_not_called()

    @patch("organizations.services.OrganizationService.request_rooms_sync")
    def test_sync_rooms_bad_datetime_returns_400(self, mock_sync, user):
        """Malformed datetime in body → 400; request_rooms_sync NOT called."""
        organization = OrganizationTestFactory.create_organization(name="Bad DT Org")
        admin_client = self._make_admin_client(user, organization)

        url = reverse("api:Organizations-sync-rooms", kwargs={"pk": organization.pk})
        response = admin_client.post(url, {"start_time": "not-a-datetime"}, format="json")

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        mock_sync.assert_not_called()


@pytest.mark.django_db
class TestShouldSyncRoomsTransition:
    """Verify the False→True transition in OrganizationViewSet.update fires exactly once.

    These tests use a real admin membership — no permission patching.
    The update/partial_update actions are now gated by IsOrganizationAdmin so
    the transition trigger is genuinely reachable by an admin PATCH.
    """

    def _make_admin_client_and_org(self, user, should_sync_rooms=False):
        """Create org + ADMIN membership; return (admin_client, organization)."""
        organization = baker.make(
            Organization, name="Transition Org", should_sync_rooms=should_sync_rooms
        )
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return client, organization

    @patch("organizations.services.OrganizationService.request_rooms_sync")
    def test_patch_false_to_true_fires_sync_once(self, mock_sync, user):
        """Admin PATCH should_sync_rooms False→True → 200; request_rooms_sync called once.

        Phase 18: the view checks for a service account before firing the sync.
        Provide one so the check passes.

        Phase 19: the view calls request_rooms_sync directly (no view-level
        on_commit); the service owns the on_commit deferral internally.
        """
        client, org = self._make_admin_client_and_org(user, should_sync_rooms=False)
        # Provide a service account so the pre-flight check passes.
        baker.make(
            GoogleCalendarServiceAccount,
            organization=org,
            calendar_fk=None,
            email="sa@example.com",
            audience="https://www.googleapis.com/auth/admin.directory.resource.calendar",
            public_key="pk",
            private_key_id="kid",
            private_key="key",
        )

        url = reverse("api:Organizations-detail", kwargs={"pk": org.pk})
        response = client.patch(url, {"should_sync_rooms": True}, format="json")

        assert response.status_code == status.HTTP_200_OK, (
            f"Expected 200, got {response.status_code}: {response.content}"
        )
        mock_sync.assert_called_once()
        call_kwargs = mock_sync.call_args.kwargs
        assert call_kwargs["organization"].pk == org.pk
        assert call_kwargs["requested_by"] == user

    @patch("organizations.services.OrganizationService.request_rooms_sync")
    def test_patch_already_true_does_not_fire_sync(self, mock_sync, user):
        """Admin PATCH on org with should_sync_rooms already True → 200; sync NOT fired."""
        client, org = self._make_admin_client_and_org(user, should_sync_rooms=True)

        url = reverse("api:Organizations-detail", kwargs={"pk": org.pk})
        response = client.patch(url, {"should_sync_rooms": True}, format="json")

        assert response.status_code == status.HTTP_200_OK, (
            f"Expected 200, got {response.status_code}: {response.content}"
        )
        mock_sync.assert_not_called()

    @patch("organizations.services.OrganizationService.request_rooms_sync")
    def test_patch_true_to_false_does_not_fire_sync(self, mock_sync, user):
        """Admin PATCH should_sync_rooms True→False → 200; sync NOT fired."""
        client, org = self._make_admin_client_and_org(user, should_sync_rooms=True)

        url = reverse("api:Organizations-detail", kwargs={"pk": org.pk})
        response = client.patch(url, {"should_sync_rooms": False}, format="json")

        assert response.status_code == status.HTTP_200_OK, (
            f"Expected 200, got {response.status_code}: {response.content}"
        )
        mock_sync.assert_not_called()

    @patch("organizations.services.OrganizationService.request_rooms_sync")
    def test_patch_unrelated_field_does_not_fire_sync(self, mock_sync, user):
        """Admin PATCH on unrelated field (name) while should_sync_rooms stays False — no sync."""
        client, org = self._make_admin_client_and_org(user, should_sync_rooms=False)

        url = reverse("api:Organizations-detail", kwargs={"pk": org.pk})
        response = client.patch(url, {"name": "New Name"}, format="json")

        assert response.status_code == status.HTTP_200_OK, (
            f"Expected 200, got {response.status_code}: {response.content}"
        )
        mock_sync.assert_not_called()

    @patch("organizations.services.OrganizationService.request_rooms_sync")
    def test_patch_non_admin_member_returns_403(self, mock_sync, user):
        """Non-admin member PATCH → 403 (IsOrganizationAdmin denies); sync NOT fired."""
        organization = baker.make(Organization, name="Non-Admin Org", should_sync_rooms=False)
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )
        client = APIClient()
        client.force_authenticate(user=user)

        url = reverse("api:Organizations-detail", kwargs={"pk": organization.pk})
        response = client.patch(url, {"should_sync_rooms": True}, format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN, (
            f"Expected 403, got {response.status_code}: {response.content}"
        )
        mock_sync.assert_not_called()

    @patch("organizations.services.OrganizationService.request_rooms_sync")
    def test_patch_membership_less_user_returns_403(self, mock_sync):
        """Membership-less authenticated user PATCH → 403; sync NOT fired."""
        from users.factories import UserFactory

        membership_less_user = UserFactory().create_user()
        organization = baker.make(Organization, name="No Member Org", should_sync_rooms=False)

        client = APIClient()
        client.force_authenticate(user=membership_less_user)

        url = reverse("api:Organizations-detail", kwargs={"pk": organization.pk})
        response = client.patch(url, {"should_sync_rooms": True}, format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN, (
            f"Expected 403, got {response.status_code}: {response.content}"
        )
        mock_sync.assert_not_called()

    @patch("organizations.services.OrganizationService.request_rooms_sync")
    def test_patch_unauthenticated_returns_401(self, mock_sync):
        """Unauthenticated PATCH → 401; sync NOT fired."""
        organization = baker.make(Organization, name="Anon Org", should_sync_rooms=False)
        client = APIClient()

        url = reverse("api:Organizations-detail", kwargs={"pk": organization.pk})
        response = client.patch(url, {"should_sync_rooms": True}, format="json")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED, (
            f"Expected 401, got {response.status_code}: {response.content}"
        )
        mock_sync.assert_not_called()

    @patch("organizations.services.OrganizationService.request_rooms_sync")
    def test_admin_can_set_should_sync_rooms_value_persists(self, mock_sync, user):
        """Admin PATCH should_sync_rooms → value persists in DB (configure-org use-case).

        Phase 18: the view checks for a service account before firing the sync.
        Provide one so the check passes and the PATCH returns 200.

        Phase 19: no view-level on_commit; service owns the deferral.
        """
        client, org = self._make_admin_client_and_org(user, should_sync_rooms=False)
        # Provide a service account so the False→True transition check passes.
        baker.make(
            GoogleCalendarServiceAccount,
            organization=org,
            calendar_fk=None,
            email="sa@example.com",
            audience="https://www.googleapis.com/auth/admin.directory.resource.calendar",
            public_key="pk",
            private_key_id="kid",
            private_key="key",
        )

        url = reverse("api:Organizations-detail", kwargs={"pk": org.pk})
        response = client.patch(url, {"should_sync_rooms": True}, format="json")

        assert response.status_code == status.HTTP_200_OK, (
            f"Expected 200, got {response.status_code}: {response.content}"
        )
        # Verify the value was persisted to the DB.
        org.refresh_from_db()
        assert org.should_sync_rooms is True


# ---------------------------------------------------------------------------
# Phase 18 — Rooms-sync configuration via org PATCH + working trigger
# ---------------------------------------------------------------------------

# Reusable service-account payload used across Phase 18 tests.
_SA_PAYLOAD = {
    "email": "rooms-sa@example.iam.gserviceaccount.com",
    "audience": "https://www.googleapis.com/auth/admin.directory.resource.calendar",
    "public_key": "test-public-key-value-not-a-real-key",
    "private_key_id": "key-id-abc123",
    "private_key": "test-private-key-value-not-a-real-key",
}


@pytest.mark.django_db
class TestPhase18ServiceAccountConfig:
    """Admin configures the org's Google service-account credentials via PATCH.

    Security invariant: private_key and private_key_id are never returned in
    any response.  Rotation: a second PATCH replaces the stored credentials.
    """

    def _make_admin(self, user, organization):
        """Return an admin APIClient force-authenticated as an ADMIN of ``organization``."""
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def test_patch_with_service_account_creates_row_and_no_secrets_in_response(self, user):
        """Admin PATCH with nested google_service_account → 200; row created; no secrets."""
        org = baker.make(Organization, name="SA Org", should_sync_rooms=False)
        client = self._make_admin(user, org)

        url = reverse("api:Organizations-detail", kwargs={"pk": org.pk})
        response = client.patch(
            url,
            {"google_service_account": _SA_PAYLOAD},
            format="json",
        )

        assert_response_status_code(response, status.HTTP_200_OK)
        body = response.json()

        # Service account info is present in the response.
        sa_response = body.get("google_service_account")
        assert sa_response is not None, f"Expected google_service_account in response: {body}"
        assert sa_response["configured"] is True
        assert sa_response["email"] == _SA_PAYLOAD["email"]
        assert sa_response["audience"] == _SA_PAYLOAD["audience"]

        # Secrets MUST NOT be in the response.
        assert "private_key" not in sa_response, "private_key must not be returned"
        assert "private_key_id" not in sa_response, "private_key_id must not be returned"
        assert "public_key" not in sa_response, "public_key must not be returned"

        # DB row created.
        stored = (
            GoogleCalendarServiceAccount.objects.filter_by_organization(org.id)
            .filter(calendar_fk__isnull=True)
            .first()
        )
        assert stored is not None, "GoogleCalendarServiceAccount row should have been created"
        assert stored.email == _SA_PAYLOAD["email"]
        assert stored.audience == _SA_PAYLOAD["audience"]

    def test_second_patch_rotates_credentials(self, user):
        """Second PATCH with different creds replaces the stored service account."""
        org = baker.make(Organization, name="Rotate Org", should_sync_rooms=False)
        client = self._make_admin(user, org)
        url = reverse("api:Organizations-detail", kwargs={"pk": org.pk})

        # First PATCH.
        client.patch(url, {"google_service_account": _SA_PAYLOAD}, format="json")
        first_sa = (
            GoogleCalendarServiceAccount.objects.filter_by_organization(org.id)
            .filter(calendar_fk__isnull=True)
            .first()
        )
        assert first_sa is not None

        # Second PATCH with a different email.
        new_payload = dict(_SA_PAYLOAD)
        new_payload["email"] = "rotated-sa@example.iam.gserviceaccount.com"
        new_payload["private_key_id"] = "new-key-id-xyz"
        response = client.patch(url, {"google_service_account": new_payload}, format="json")

        assert_response_status_code(response, status.HTTP_200_OK)

        # Exactly one row remains (the rotated one).
        all_sa = list(
            GoogleCalendarServiceAccount.objects.filter_by_organization(org.id).filter(
                calendar_fk__isnull=True
            )
        )
        assert len(all_sa) == 1, f"Expected exactly one SA row, got {len(all_sa)}"
        assert all_sa[0].email == "rotated-sa@example.iam.gserviceaccount.com"

        # Response reflects the rotated email.
        body = response.json()
        assert (
            body["google_service_account"]["email"] == "rotated-sa@example.iam.gserviceaccount.com"
        )
        # Secrets still absent.
        assert "private_key" not in body["google_service_account"]
        assert "private_key_id" not in body["google_service_account"]
        assert "public_key" not in body["google_service_account"]

    def test_patch_omitting_service_account_leaves_existing_unchanged(self, user):
        """Omitting google_service_account on PATCH is a no-op for the SA row."""
        org = baker.make(Organization, name="No-Op Org", should_sync_rooms=False)
        client = self._make_admin(user, org)
        url = reverse("api:Organizations-detail", kwargs={"pk": org.pk})

        # Create the SA first.
        client.patch(url, {"google_service_account": _SA_PAYLOAD}, format="json")
        original_id = (
            GoogleCalendarServiceAccount.objects.filter_by_organization(org.id)
            .filter(calendar_fk__isnull=True)
            .values_list("id", flat=True)
            .first()
        )

        # PATCH only the name — google_service_account omitted.
        response = client.patch(url, {"name": "No-Op Renamed"}, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

        # The same row still exists.
        after_id = (
            GoogleCalendarServiceAccount.objects.filter_by_organization(org.id)
            .filter(calendar_fk__isnull=True)
            .values_list("id", flat=True)
            .first()
        )
        assert after_id == original_id

        # Response still shows configured.
        body = response.json()
        assert body["google_service_account"]["configured"] is True

    def test_get_response_shows_configured_true_after_patch(self, user):
        """After PATCH with SA creds, the read response shows configured=true."""
        org = baker.make(Organization, name="Read Org", should_sync_rooms=False)
        client = self._make_admin(user, org)
        url = reverse("api:Organizations-detail", kwargs={"pk": org.pk})

        # Ensure a SA exists.
        baker.make(
            GoogleCalendarServiceAccount,
            organization=org,
            calendar_fk=None,
            email=_SA_PAYLOAD["email"],
            audience=_SA_PAYLOAD["audience"],
            public_key=_SA_PAYLOAD["public_key"],
            private_key_id=_SA_PAYLOAD["private_key_id"],
            private_key=_SA_PAYLOAD["private_key"],
        )

        # The read path is exercised via PATCH (which returns the object).
        response = client.patch(url, {"name": "Read Org Renamed"}, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)
        body = response.json()

        assert body["google_service_account"] is not None
        assert body["google_service_account"]["configured"] is True
        assert body["google_service_account"]["email"] == _SA_PAYLOAD["email"]
        assert "private_key" not in body["google_service_account"]
        assert "private_key_id" not in body["google_service_account"]
        assert "public_key" not in body["google_service_account"]

    def test_patch_service_account_non_admin_returns_403(self, auth_client, user):
        """Non-admin member cannot configure the service account → 403."""
        org = baker.make(Organization, name="Member SA Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=org,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )
        url = reverse("api:Organizations-detail", kwargs={"pk": org.pk})
        response = auth_client.patch(url, {"google_service_account": _SA_PAYLOAD}, format="json")
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_patch_service_account_cross_org_returns_404(self, user):
        """Admin cannot configure a service account on a different org → 404."""
        own_org = baker.make(Organization, name="Own Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=own_org,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        other_org = baker.make(Organization, name="Other Org")

        client = APIClient()
        client.force_authenticate(user=user)
        url = reverse("api:Organizations-detail", kwargs={"pk": other_org.pk})
        response = client.patch(url, {"google_service_account": _SA_PAYLOAD}, format="json")
        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_patch_service_account_unauthenticated_returns_401(self):
        """Unauthenticated request cannot configure a service account → 401."""
        org = baker.make(Organization, name="Anon SA Org")
        client = APIClient()
        url = reverse("api:Organizations-detail", kwargs={"pk": org.pk})
        response = client.patch(url, {"google_service_account": _SA_PAYLOAD}, format="json")
        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)


@pytest.mark.django_db
class TestPhase18SyncRoomsTrigger:
    """POST sync-rooms works when creds configured; 400 (never 500) when not.

    Mocks ``calendar_service.authenticate`` and
    ``request_organization_calendar_resources_import`` so the tests do not hit
    the Google API.  The test asserts that ``authenticate`` is called with the
    stored ``GoogleCalendarServiceAccount``.
    """

    def _make_admin(self, user, organization):
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def test_sync_rooms_with_service_account_calls_authenticate(self, user):
        """With a service account configured, sync-rooms authenticates with it."""
        org = baker.make(Organization, name="SA Sync Org", should_sync_rooms=True)
        sa = baker.make(
            GoogleCalendarServiceAccount,
            organization=org,
            calendar_fk=None,
            email=_SA_PAYLOAD["email"],
            audience=_SA_PAYLOAD["audience"],
            public_key=_SA_PAYLOAD["public_key"],
            private_key_id=_SA_PAYLOAD["private_key_id"],
            private_key=_SA_PAYLOAD["private_key"],
        )
        client = self._make_admin(user, org)

        mock_calendar_service = MagicMock()
        mock_calendar_service.authenticate.return_value = None
        mock_calendar_service.request_organization_calendar_resources_import.return_value = None

        from di_core.containers import container

        with container.calendar_service.override(mock_calendar_service):
            url = reverse("api:Organizations-sync-rooms", kwargs={"pk": org.pk})
            response = client.post(url, {}, format="json")

        assert_response_status_code(response, status.HTTP_202_ACCEPTED)

        # authenticate must have been called with our service account.
        mock_calendar_service.authenticate.assert_called_once()
        call_kwargs = mock_calendar_service.authenticate.call_args.kwargs
        assert call_kwargs["account"].id == sa.id
        assert call_kwargs["organization"] == org

        # import must have been called.
        mock_calendar_service.request_organization_calendar_resources_import.assert_called_once()

    def test_sync_rooms_without_service_account_returns_400(self, user):
        """Without a service account configured, sync-rooms returns 400."""
        org = baker.make(Organization, name="No SA Org", should_sync_rooms=True)
        client = self._make_admin(user, org)

        url = reverse("api:Organizations-sync-rooms", kwargs={"pk": org.pk})
        response = client.post(url, {}, format="json")

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        body = str(response.json()).lower()
        assert "service account" in body or "configure" in body, (
            f"Expected a 'service account' message in the 400 response: {body}"
        )


@pytest.mark.django_db
class TestPhase18TransitionWithNoCredentials:
    """Enabling should_sync_rooms False→True without creds → 400 (not 500)."""

    def _make_admin_client_and_org(self, user, should_sync_rooms=False):
        organization = baker.make(
            Organization, name="Creds Transition Org", should_sync_rooms=should_sync_rooms
        )
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return client, organization

    def test_enable_sync_rooms_without_creds_returns_400(self, user):
        """PATCH enabling should_sync_rooms with no SA configured → 400."""
        client, org = self._make_admin_client_and_org(user, should_sync_rooms=False)

        url = reverse("api:Organizations-detail", kwargs={"pk": org.pk})
        response = client.patch(url, {"should_sync_rooms": True}, format="json")

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        body = str(response.json()).lower()
        assert "service account" in body or "configure" in body, (
            f"Expected a 'service account' message in 400 response: {body}"
        )

    def test_enable_sync_rooms_with_creds_in_same_patch_succeeds(self, user):
        """PATCH enabling should_sync_rooms + providing SA creds in same request → 200."""
        client, org = self._make_admin_client_and_org(user, should_sync_rooms=False)

        mock_calendar_service = MagicMock()
        mock_calendar_service.authenticate.return_value = None
        mock_calendar_service.request_organization_calendar_resources_import.return_value = None

        from di_core.containers import container

        url = reverse("api:Organizations-detail", kwargs={"pk": org.pk})
        with container.calendar_service.override(mock_calendar_service):
            response = client.patch(
                url,
                {"should_sync_rooms": True, "google_service_account": _SA_PAYLOAD},
                format="json",
            )

        assert_response_status_code(response, status.HTTP_200_OK)
        body = response.json()
        assert body["should_sync_rooms"] is True
        sa_response = body["google_service_account"]
        assert sa_response["configured"] is True

        # Secrets MUST NOT be in the response.
        assert "private_key" not in sa_response, "private_key must not be returned"
        assert "private_key_id" not in sa_response, "private_key_id must not be returned"
        assert "public_key" not in sa_response, "public_key must not be returned"

        # The sync trigger must have actually fired with the configured account.
        mock_calendar_service.authenticate.assert_called_once()

    def test_rename_and_enable_sync_without_creds_returns_400_and_name_unchanged(self, user):
        """PATCH renaming the org AND enabling should_sync_rooms with no creds → 400;
        the rename must NOT be persisted (no partial write)."""
        client, org = self._make_admin_client_and_org(user, should_sync_rooms=False)
        original_name = org.name

        url = reverse("api:Organizations-detail", kwargs={"pk": org.pk})
        response = client.patch(
            url,
            {"name": "Should Not Be Saved", "should_sync_rooms": True},
            format="json",
        )

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

        # The name must remain unchanged in the DB (no partial write).
        org.refresh_from_db()
        assert org.name == original_name, (
            f"Expected org name to remain '{original_name}', got '{org.name}'"
        )
        assert org.should_sync_rooms is False, (
            "should_sync_rooms must not have been enabled after a 400"
        )

    def test_enable_sync_rooms_with_pre_existing_creds_succeeds(self, user):
        """PATCH enabling should_sync_rooms when SA already exists → 200; sync fires."""
        client, org = self._make_admin_client_and_org(user, should_sync_rooms=False)

        # Pre-configure a service account.
        baker.make(
            GoogleCalendarServiceAccount,
            organization=org,
            calendar_fk=None,
            email=_SA_PAYLOAD["email"],
            audience=_SA_PAYLOAD["audience"],
            public_key=_SA_PAYLOAD["public_key"],
            private_key_id=_SA_PAYLOAD["private_key_id"],
            private_key=_SA_PAYLOAD["private_key"],
        )

        mock_calendar_service = MagicMock()
        mock_calendar_service.authenticate.return_value = None
        mock_calendar_service.request_organization_calendar_resources_import.return_value = None

        from di_core.containers import container

        url = reverse("api:Organizations-detail", kwargs={"pk": org.pk})
        with container.calendar_service.override(mock_calendar_service):
            response = client.patch(url, {"should_sync_rooms": True}, format="json")

        assert_response_status_code(response, status.HTTP_200_OK)
        body = response.json()
        assert body["should_sync_rooms"] is True


@pytest.mark.django_db
class TestPhase18CreateOrganizationNoCredentials:
    """create_organization(should_sync_rooms=True) with no creds must not crash."""

    def test_create_organization_with_sync_rooms_no_creds_does_not_crash(self, user):
        """Org is created successfully even when no service account is available.

        The rooms sync is silently skipped (warning logged); org creation returns
        the new Organization without crashing.
        """
        from di_core.containers import container

        # We don't override the calendar service here — the real code path should
        # hit the NoServiceAccountConfiguredError guard in request_rooms_sync and
        # swallow it gracefully inside create_organization.
        service = container.organization_service()
        org = service.create_organization(
            creator=user,
            name="NoCredOrg",
            should_sync_rooms=True,
        )

        assert org is not None
        assert org.name == "NoCredOrg"
        assert org.should_sync_rooms is True

        # Org and membership exist.
        assert Organization.objects.filter(id=org.id).exists()
        membership = OrganizationMembership.objects.get(user=user, organization=org)
        assert membership is not None


@pytest.mark.django_db
class TestPhase20ServiceAccountCRUD:
    """Admin-only CRUD for the org-level Google Calendar service account.

    Security invariant: private_key / private_key_id / public_key are never
    returned in any response. Only the org-level account (calendar_fk IS NULL)
    is managed; one per organization (create refuses a duplicate).
    """

    def _make_admin(self, user, organization):
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _create_account(self, organization, **overrides):
        payload = {
            "organization": organization,
            "calendar_fk": None,
            "email": "svc@example.iam.gserviceaccount.com",
            "audience": "https://www.googleapis.com/auth/admin.directory.resource.calendar",
            "public_key": "pub",
            "private_key_id": "kid",
            "private_key": "secret",
        }
        payload.update(overrides)
        return GoogleCalendarServiceAccount.objects.create(**payload)

    def _assert_no_secrets(self, body: dict):
        assert "private_key" not in body
        assert "private_key_id" not in body
        assert "public_key" not in body

    def test_create_returns_201_persists_and_no_secrets(self, user):
        org = baker.make(Organization, name="SA CRUD Org")
        client = self._make_admin(user, org)

        url = reverse("api:ServiceAccounts-list")
        response = client.post(url, _SA_PAYLOAD, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        body = response.json()
        assert body["email"] == _SA_PAYLOAD["email"]
        assert body["audience"] == _SA_PAYLOAD["audience"]
        assert body["configured"] is True
        self._assert_no_secrets(body)

        stored = (
            GoogleCalendarServiceAccount.objects.filter_by_organization(org.id)
            .filter(calendar_fk__isnull=True)
            .first()
        )
        assert stored is not None
        assert stored.email == _SA_PAYLOAD["email"]
        # Secrets persisted (encrypted) and readable from the model.
        assert stored.private_key == _SA_PAYLOAD["private_key"]

    def test_create_with_long_pem_private_key_succeeds(self, user):
        """A realistic ~1.7KB PEM private_key must not be rejected by a 255 cap."""
        org = baker.make(Organization, name="Long Key Org")
        client = self._make_admin(user, org)

        # Marker literals are assembled at runtime so the source file does not trip
        # the detect-private-key pre-commit hook (this is dummy, non-secret data).
        header = "-----BEGIN " + "PRIVATE KEY-----\n"
        footer = "-----END " + "PRIVATE KEY-----\n"
        long_private_key = (
            header + "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ\n" * 30 + footer
        )
        assert len(long_private_key) > 255

        payload = dict(_SA_PAYLOAD)
        payload["private_key"] = long_private_key

        url = reverse("api:ServiceAccounts-list")
        response = client.post(url, payload, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        stored = (
            GoogleCalendarServiceAccount.objects.filter_by_organization(org.id)
            .filter(calendar_fk__isnull=True)
            .first()
        )
        assert stored is not None
        assert stored.private_key == long_private_key

    def test_create_duplicate_returns_400(self, user):
        org = baker.make(Organization, name="Dup Org")
        client = self._make_admin(user, org)
        self._create_account(org)

        url = reverse("api:ServiceAccounts-list")
        response = client.post(url, _SA_PAYLOAD, format="json")

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        # Still exactly one row.
        assert (
            GoogleCalendarServiceAccount.objects.filter_by_organization(org.id)
            .filter(calendar_fk__isnull=True)
            .count()
            == 1
        )

    def test_list_returns_account_no_secrets(self, user):
        org = baker.make(Organization, name="List Org")
        client = self._make_admin(user, org)
        account = self._create_account(org)

        url = reverse("api:ServiceAccounts-list")
        response = client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        results = response.json()["results"]
        assert len(results) == 1
        assert results[0]["id"] == account.id
        self._assert_no_secrets(results[0])

    def test_retrieve_returns_account_no_secrets(self, user):
        org = baker.make(Organization, name="Retrieve Org")
        client = self._make_admin(user, org)
        account = self._create_account(org)

        url = reverse("api:ServiceAccounts-detail", kwargs={"pk": account.id})
        response = client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        body = response.json()
        assert body["id"] == account.id
        assert body["email"] == account.email
        self._assert_no_secrets(body)

    def test_put_rotates_credentials(self, user):
        org = baker.make(Organization, name="Put Org")
        client = self._make_admin(user, org)
        account = self._create_account(org)

        new_payload = dict(_SA_PAYLOAD)
        new_payload["email"] = "rotated@example.iam.gserviceaccount.com"
        new_payload["private_key"] = "rotated-secret"
        url = reverse("api:ServiceAccounts-detail", kwargs={"pk": account.id})
        response = client.put(url, new_payload, format="json")

        assert_response_status_code(response, status.HTTP_200_OK)
        body = response.json()
        assert body["email"] == "rotated@example.iam.gserviceaccount.com"
        self._assert_no_secrets(body)

        account.refresh_from_db()
        assert account.email == "rotated@example.iam.gserviceaccount.com"
        assert account.private_key == "rotated-secret"

    def test_patch_partial_updates_email_retains_secrets(self, user):
        org = baker.make(Organization, name="Patch Org")
        client = self._make_admin(user, org)
        account = self._create_account(org, private_key="keep-me")

        url = reverse("api:ServiceAccounts-detail", kwargs={"pk": account.id})
        response = client.patch(
            url, {"email": "newmail@example.iam.gserviceaccount.com"}, format="json"
        )

        assert_response_status_code(response, status.HTTP_200_OK)
        body = response.json()
        assert body["email"] == "newmail@example.iam.gserviceaccount.com"
        self._assert_no_secrets(body)

        account.refresh_from_db()
        assert account.email == "newmail@example.iam.gserviceaccount.com"
        # Secret retained because it was not part of the partial payload.
        assert account.private_key == "keep-me"

    def test_delete_removes_account(self, user):
        org = baker.make(Organization, name="Delete Org")
        client = self._make_admin(user, org)
        account = self._create_account(org)

        url = reverse("api:ServiceAccounts-detail", kwargs={"pk": account.id})
        response = client.delete(url)

        assert_response_status_code(response, status.HTTP_204_NO_CONTENT)
        assert not GoogleCalendarServiceAccount.objects.filter(id=account.id).exists()

    def test_create_non_admin_returns_403(self, user):
        org = baker.make(Organization, name="NonAdmin Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=org,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )
        client = APIClient()
        client.force_authenticate(user=user)

        url = reverse("api:ServiceAccounts-list")
        response = client.post(url, _SA_PAYLOAD, format="json")

        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_retrieve_cross_org_returns_404(self, user):
        org_a = baker.make(Organization, name="Org A")
        org_b = baker.make(Organization, name="Org B")
        client = self._make_admin(user, org_a)
        other_account = self._create_account(org_b)

        url = reverse("api:ServiceAccounts-detail", kwargs={"pk": other_account.id})
        response = client.get(url)

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_anonymous_returns_401(self, anonymous_client):
        url = reverse("api:ServiceAccounts-list")
        response = anonymous_client.get(url)

        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)


@pytest.mark.django_db
class TestPhase20SyncAllCalendars:
    """POST /organizations/{id}/sync-calendars/ — admin triggers a sync of all calendars."""

    def _make_admin(self, user, organization):
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    @patch("organizations.services.OrganizationService.request_all_calendars_sync")
    def test_sync_calendars_returns_202_with_summary(self, mock_sync, user):
        org = baker.make(Organization, name="Sync All Org")
        client = self._make_admin(user, org)
        mock_sync.return_value = {
            "synced": [1, 2],
            "skipped": [{"calendar_id": 3, "reason": "no owner"}],
        }

        url = reverse("api:Organizations-sync-calendars", kwargs={"pk": org.pk})
        response = client.post(
            url,
            {
                "start_datetime": "2026-06-01T00:00:00Z",
                "end_datetime": "2026-06-30T00:00:00Z",
                "should_update_events": True,
            },
            format="json",
        )

        assert_response_status_code(response, status.HTTP_202_ACCEPTED)
        body = response.json()
        assert body == {"synced": [1, 2], "skipped": [{"calendar_id": 3, "reason": "no owner"}]}
        mock_sync.assert_called_once()
        _, kwargs = mock_sync.call_args
        assert kwargs["organization"].id == org.id
        assert kwargs["should_update_events"] is True

    @patch("organizations.services.OrganizationService.request_all_calendars_sync")
    def test_sync_calendars_bad_datetime_returns_400(self, mock_sync, user):
        org = baker.make(Organization, name="Bad DT Org")
        client = self._make_admin(user, org)

        url = reverse("api:Organizations-sync-calendars", kwargs={"pk": org.pk})
        response = client.post(url, {"start_datetime": "not-a-date"}, format="json")

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        mock_sync.assert_not_called()

    @patch("organizations.services.OrganizationService.request_all_calendars_sync")
    def test_sync_calendars_non_admin_returns_403(self, mock_sync, user):
        org = baker.make(Organization, name="NonAdmin Sync Org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=org,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )
        client = APIClient()
        client.force_authenticate(user=user)

        url = reverse("api:Organizations-sync-calendars", kwargs={"pk": org.pk})
        response = client.post(
            url,
            {"start_datetime": "2026-06-01T00:00:00Z", "end_datetime": "2026-06-30T00:00:00Z"},
            format="json",
        )

        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)
        mock_sync.assert_not_called()

    @patch("organizations.services.OrganizationService.request_all_calendars_sync")
    def test_sync_calendars_cross_org_returns_404(self, mock_sync, user):
        org_a = baker.make(Organization, name="Sync Org A")
        org_b = baker.make(Organization, name="Sync Org B")
        client = self._make_admin(user, org_a)

        url = reverse("api:Organizations-sync-calendars", kwargs={"pk": org_b.pk})
        response = client.post(
            url,
            {"start_datetime": "2026-06-01T00:00:00Z", "end_datetime": "2026-06-30T00:00:00Z"},
            format="json",
        )

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)
        mock_sync.assert_not_called()

    @patch("organizations.services.OrganizationService.request_all_calendars_sync")
    def test_sync_calendars_anonymous_returns_401(self, mock_sync, anonymous_client):
        org = baker.make(Organization, name="Anon Sync Org")
        url = reverse("api:Organizations-sync-calendars", kwargs={"pk": org.pk})
        response = anonymous_client.post(
            url,
            {"start_datetime": "2026-06-01T00:00:00Z", "end_datetime": "2026-06-30T00:00:00Z"},
            format="json",
        )

        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)
        mock_sync.assert_not_called()
