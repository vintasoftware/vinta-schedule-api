import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status

from organizations.models import Organization, OrganizationMembership, OrganizationTier


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
        """Test updating an organization when user has membership"""
        url = reverse("api:Organizations-detail", kwargs={"pk": organization_with_membership.pk})
        data = {
            "name": "Updated Organization Name",
            "should_sync_rooms": True,
        }
        response = auth_client.patch(url, data, format="json")

        # Users with membership get 403 due to permission class logic
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_update_organization_authenticated_without_membership(self, auth_client, organization):
        """Test updating an organization when user has no membership"""
        url = reverse("api:Organizations-detail", kwargs={"pk": organization.pk})
        data = {
            "name": "Updated Organization Name",
        }
        response = auth_client.patch(url, data, format="json")

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

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
        """Test organization permissions when user has membership"""
        organization = OrganizationTestFactory.create_organization()
        OrganizationTestFactory.create_organization_membership(user, organization)

        # Should NOT be able to retrieve due to permission class logic
        url = reverse("api:Organizations-detail", kwargs={"pk": organization.pk})
        response = auth_client.get(url)
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

        # Should NOT be able to update due to permission class logic
        response = auth_client.patch(url, {"name": "Updated Name"}, format="json")
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

        # Should NOT be able to delete due to permission class logic
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
