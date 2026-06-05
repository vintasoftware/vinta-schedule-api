import json

from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from organizations.models import Organization, OrganizationMembership, OrganizationRole
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess, SystemUser
from users.models import Profile


User = get_user_model()


def assert_response_status_code(response, expected_status_code):
    assert response.status_code == expected_status_code, (
        f"Status {response.status_code} != {expected_status_code}\n"
        f"Response: {json.dumps(response.json() if hasattr(response, 'json') and callable(response.json) else str(response.content))}"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization():
    """A test organization."""
    return baker.make(Organization, name="Test Org")


@pytest.fixture
def other_organization():
    """A second organization for cross-org isolation tests."""
    return baker.make(Organization, name="Other Org")


@pytest.fixture
def admin_user(organization):
    """A User with an ADMIN membership in *organization*."""
    user = baker.make(User, email="admin@example.com")
    baker.make(Profile, user=user)
    baker.make(
        OrganizationMembership,
        user=user,
        organization=organization,
        role=OrganizationRole.ADMIN,
        is_active=True,
    )
    return user


@pytest.fixture
def member_user(organization):
    """A User with a MEMBER membership in *organization*."""
    user = baker.make(User, email="member@example.com")
    baker.make(Profile, user=user)
    baker.make(
        OrganizationMembership,
        user=user,
        organization=organization,
        role=OrganizationRole.MEMBER,
        is_active=True,
    )
    return user


@pytest.fixture
def membership_less_user():
    """A User with no OrganizationMembership at all."""
    user = baker.make(User, email="gated@example.com")
    baker.make(Profile, user=user)
    return user


@pytest.fixture
def admin_client(admin_user):
    """APIClient authenticated as the admin user."""
    client = APIClient()
    client.force_authenticate(user=admin_user)
    return client


@pytest.fixture
def member_client(member_user):
    """APIClient authenticated as the member user."""
    client = APIClient()
    client.force_authenticate(user=member_user)
    return client


@pytest.fixture
def anonymous_client():
    """Unauthenticated APIClient."""
    return APIClient()


@pytest.fixture
def membership_less_client(membership_less_user):
    """APIClient authenticated as a user with no membership."""
    client = APIClient()
    client.force_authenticate(user=membership_less_user)
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSystemUserTokenViewSetCreate:
    """POST /public-api-tokens/ — Phase 12 create-only endpoint."""

    CREATE_URL = "api:PublicAPITokens-list"

    def _url(self):
        return reverse(self.CREATE_URL)

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_admin_creates_token_returns_201(self, admin_client, organization):
        """Admin can create a token; 201 returned with full payload."""
        payload = {
            "integration_name": "my_integration",
            "available_resources": [
                PublicAPIResources.CALENDAR,
                PublicAPIResources.CALENDAR_EVENT,
            ],
        }
        response = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)

    def test_response_includes_plaintext_token(self, admin_client, organization):
        """Response body contains a non-empty plaintext ``token``."""
        payload = {
            "integration_name": "token_test",
            "available_resources": [PublicAPIResources.CALENDAR],
        }
        response = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)
        data = response.json()
        assert "token" in data
        assert data["token"]  # non-empty string

    def test_response_includes_id_and_is_active(self, admin_client, organization):
        """Response body includes id and is_active fields."""
        payload = {
            "integration_name": "id_active_test",
            "available_resources": [PublicAPIResources.CALENDAR],
        }
        response = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)
        data = response.json()
        assert "id" in data
        assert data["id"] is not None
        assert "is_active" in data
        assert data["is_active"] is True

    def test_response_includes_integration_name(self, admin_client, organization):
        """Response body contains the submitted integration_name."""
        payload = {
            "integration_name": "name_check",
            "available_resources": [PublicAPIResources.USER],
        }
        response = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)
        assert response.json()["integration_name"] == "name_check"

    def test_response_includes_available_resources(self, admin_client, organization):
        """Response body lists all requested resources."""
        resources = [PublicAPIResources.CALENDAR, PublicAPIResources.USER]
        payload = {
            "integration_name": "resource_check",
            "available_resources": resources,
        }
        response = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)
        returned_resources = set(response.json()["available_resources"])
        assert returned_resources == set(resources)

    def test_response_is_active_true(self, admin_client, organization):
        """Newly created token is active."""
        payload = {
            "integration_name": "active_check",
            "available_resources": [PublicAPIResources.CALENDAR],
        }
        response = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)
        assert response.json()["is_active"] is True

    def test_system_user_row_created_for_caller_org(self, admin_client, admin_user, organization):
        """A SystemUser row is created and scoped to the caller's organisation."""
        payload = {
            "integration_name": "org_scope_check",
            "available_resources": [PublicAPIResources.CALENDAR],
        }
        admin_client.post(self._url(), payload, format="json")
        system_user = SystemUser.objects.get(integration_name="org_scope_check")
        assert system_user.organization_id == organization.id
        assert system_user.is_active is True

    def test_resource_access_rows_created(self, admin_client, organization):
        """A ResourceAccess row is persisted for each requested resource."""
        resources = [
            PublicAPIResources.CALENDAR,
            PublicAPIResources.CALENDAR_EVENT,
            PublicAPIResources.USER,
        ]
        payload = {
            "integration_name": "ra_check",
            "available_resources": resources,
        }
        admin_client.post(self._url(), payload, format="json")
        system_user = SystemUser.objects.get(integration_name="ra_check")
        persisted = set(
            ResourceAccess.objects.filter(system_user=system_user).values_list(
                "resource_name", flat=True
            )
        )
        assert persisted == set(resources)

    def test_long_lived_token_hash_not_in_response(self, admin_client, organization):
        """The ``long_lived_token_hash`` field must never appear in the response."""
        payload = {
            "integration_name": "no_hash_check",
            "available_resources": [PublicAPIResources.CALENDAR],
        }
        response = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)
        assert "long_lived_token_hash" not in response.json()

    # ------------------------------------------------------------------
    # Validation errors
    # ------------------------------------------------------------------

    def test_invalid_resource_value_returns_400(self, admin_client, organization):
        """An unrecognised resource value yields HTTP 400."""
        payload = {
            "integration_name": "bad_resource",
            "available_resources": ["not_a_real_resource"],
        }
        response = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_empty_available_resources_returns_400(self, admin_client, organization):
        """An empty available_resources list yields HTTP 400."""
        payload = {
            "integration_name": "empty_resources",
            "available_resources": [],
        }
        response = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_missing_integration_name_returns_400(self, admin_client, organization):
        """Missing integration_name in request body yields HTTP 400."""
        payload = {
            "available_resources": [PublicAPIResources.CALENDAR],
        }
        response = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        data = response.json()
        assert "integration_name" in data

    def test_duplicate_integration_name_returns_400(self, admin_client, organization):
        """A duplicate integration_name yields HTTP 400 (not 500 from IntegrityError).

        The savepoint in create() ensures that the IntegrityError on duplicate
        integration_name is caught and rolled back without poisoning the outer
        request transaction — this is critical under ATOMIC_REQUESTS=True in production.
        """
        payload = {
            "integration_name": "dup_name",
            "available_resources": [PublicAPIResources.CALENDAR],
        }
        # First create succeeds
        response1 = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response1, status.HTTP_201_CREATED)

        # Second with same integration_name should be 400
        response2 = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response2, status.HTTP_400_BAD_REQUEST)
        data = response2.json()
        assert "integration_name" in data

    # ------------------------------------------------------------------
    # Permission / auth failures
    # ------------------------------------------------------------------

    def test_member_user_gets_403(self, member_client, organization):
        """A non-admin member is rejected with HTTP 403."""
        payload = {
            "integration_name": "member_attempt",
            "available_resources": [PublicAPIResources.CALENDAR],
        }
        response = member_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_membership_less_user_gets_403(self, membership_less_client):
        """A user with no membership is rejected with HTTP 403."""
        payload = {
            "integration_name": "gated_attempt",
            "available_resources": [PublicAPIResources.CALENDAR],
        }
        response = membership_less_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_anonymous_user_gets_401(self, anonymous_client):
        """An unauthenticated request is rejected with HTTP 401."""
        payload = {
            "integration_name": "anon_attempt",
            "available_resources": [PublicAPIResources.CALENDAR],
        }
        response = anonymous_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    # ------------------------------------------------------------------
    # Org scoping
    # ------------------------------------------------------------------

    def test_created_system_user_scoped_to_caller_org_not_other(
        self, admin_client, admin_user, organization, other_organization
    ):
        """SystemUser is created for the admin's org, not any other org."""
        payload = {
            "integration_name": "scope_isolation",
            "available_resources": [PublicAPIResources.CALENDAR],
        }
        admin_client.post(self._url(), payload, format="json")
        system_user = SystemUser.objects.get(integration_name="scope_isolation")
        assert system_user.organization_id == organization.id
        assert system_user.organization_id != other_organization.id
