import json

from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from organizations.models import Organization, OrganizationMembership, OrganizationRole
from public_api.constants import PROVIDER_SCOPED_RESOURCES, PublicAPIResources
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

    def test_create_deduplicates_duplicate_resources(self, admin_client, organization):
        """If the request lists the same resource twice, it is deduplicated.

        Only one ResourceAccess row is created per distinct resource.
        """
        payload = {
            "integration_name": "dedup_test",
            "available_resources": [
                PublicAPIResources.CALENDAR,
                PublicAPIResources.CALENDAR,
                PublicAPIResources.USER,
            ],
        }
        response = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)

        # Verify only one row per distinct resource exists
        system_user = SystemUser.original_manager.get(integration_name="dedup_test")
        persisted_resources = list(
            ResourceAccess.objects.filter(system_user=system_user).values_list(
                "resource_name", flat=True
            )
        )
        # Check exact distinct count: CALENDAR and USER only
        assert len(persisted_resources) == 2
        assert persisted_resources.count(PublicAPIResources.CALENDAR) == 1
        assert persisted_resources.count(PublicAPIResources.USER) == 1

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
        system_user = SystemUser.original_manager.get(integration_name="org_scope_check")
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
        system_user = SystemUser.original_manager.get(integration_name="ra_check")
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
        system_user = SystemUser.original_manager.get(integration_name="scope_isolation")
        assert system_user.organization_id == organization.id
        assert system_user.organization_id != other_organization.id


@pytest.mark.django_db
class TestSystemUserTokenViewSetList:
    """GET /public-api-tokens/ — Phase 13 list endpoint."""

    LIST_URL = "api:PublicAPITokens-list"

    def _url(self):
        return reverse(self.LIST_URL)

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_admin_lists_tokens_returns_200(self, admin_client, organization):
        """Admin can list tokens; 200 returned."""
        # Create a token first
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        response = admin_client.get(self._url())
        assert_response_status_code(response, status.HTTP_200_OK)

    def test_list_returns_all_org_tokens(self, admin_client, organization):
        """List returns all tokens for the caller's org (active and inactive)."""
        # Create multiple tokens in the org
        system_user_1 = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user_1, resource_name=PublicAPIResources.CALENDAR
        )

        system_user_2 = baker.make(SystemUser, organization=organization, is_active=False)
        baker.make(ResourceAccess, system_user=system_user_2, resource_name=PublicAPIResources.USER)

        response = admin_client.get(self._url())
        assert_response_status_code(response, status.HTTP_200_OK)
        data = response.json()
        results = data["results"]
        assert len(results) == 2
        ids = [token["id"] for token in results]
        assert system_user_1.id in ids
        assert system_user_2.id in ids

    def test_list_response_includes_required_fields(self, admin_client, organization):
        """List response includes id, integration_name, is_active, available_resources."""
        system_user = baker.make(
            SystemUser, organization=organization, integration_name="test_int", is_active=True
        )
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        response = admin_client.get(self._url())
        assert_response_status_code(response, status.HTTP_200_OK)
        data = response.json()
        results = data["results"]
        assert len(results) == 1

        token_data = results[0]
        assert "id" in token_data
        assert token_data["id"] == system_user.id
        assert "integration_name" in token_data
        assert token_data["integration_name"] == "test_int"
        assert "is_active" in token_data
        assert token_data["is_active"] is True
        assert "available_resources" in token_data

    def test_list_response_includes_available_resources(self, admin_client, organization):
        """List response includes all available_resources for each token."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        resources = [
            PublicAPIResources.CALENDAR,
            PublicAPIResources.USER,
            PublicAPIResources.CALENDAR_EVENT,
        ]
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)

        response = admin_client.get(self._url())
        assert_response_status_code(response, status.HTTP_200_OK)
        data = response.json()
        results = data["results"]
        assert len(results) == 1

        returned_resources = set(results[0]["available_resources"])
        assert returned_resources == set(resources)

    def test_list_does_not_include_token_or_hash(self, admin_client, organization):
        """List response must never include token or long_lived_token_hash."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        response = admin_client.get(self._url())
        assert_response_status_code(response, status.HTTP_200_OK)
        data = response.json()
        results = data["results"]
        assert len(results) == 1

        token_data = results[0]
        assert "token" not in token_data
        assert "long_lived_token_hash" not in token_data

    def test_list_excludes_cross_org_tokens(self, admin_client, organization, other_organization):
        """List excludes tokens from other organizations."""
        # Create token in the admin's org
        system_user_own = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user_own, resource_name=PublicAPIResources.CALENDAR
        )

        # Create token in another org
        system_user_other = baker.make(SystemUser, organization=other_organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user_other, resource_name=PublicAPIResources.CALENDAR
        )

        response = admin_client.get(self._url())
        assert_response_status_code(response, status.HTTP_200_OK)
        data = response.json()
        results = data["results"]
        assert len(results) == 1
        assert results[0]["id"] == system_user_own.id

    # ------------------------------------------------------------------
    # Permission / auth failures
    # ------------------------------------------------------------------

    def test_member_user_gets_403(self, member_client):
        """A non-admin member is rejected with HTTP 403."""
        response = member_client.get(self._url())
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_membership_less_user_gets_403(self, membership_less_client):
        """A user with no membership is rejected with HTTP 403."""
        response = membership_less_client.get(self._url())
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_anonymous_user_gets_401(self, anonymous_client):
        """An unauthenticated request is rejected with HTTP 401."""
        response = anonymous_client.get(self._url())
        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)


@pytest.mark.django_db
class TestSystemUserTokenViewSetRetrieve:
    """GET /public-api-tokens/{id}/ — Phase 13 retrieve endpoint."""

    def _url(self, token_id):
        return reverse("api:PublicAPITokens-detail", kwargs={"pk": token_id})

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_admin_retrieves_token_returns_200(self, admin_client, organization):
        """Admin can retrieve a token; 200 returned."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        response = admin_client.get(self._url(system_user.id))
        assert_response_status_code(response, status.HTTP_200_OK)

    def test_retrieve_response_includes_required_fields(self, admin_client, organization):
        """Retrieve response includes id, integration_name, is_active, available_resources."""
        system_user = baker.make(
            SystemUser, organization=organization, integration_name="retrieve_test", is_active=True
        )
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        response = admin_client.get(self._url(system_user.id))
        assert_response_status_code(response, status.HTTP_200_OK)
        data = response.json()

        assert data["id"] == system_user.id
        assert data["integration_name"] == "retrieve_test"
        assert data["is_active"] is True
        assert "available_resources" in data

    def test_retrieve_response_includes_available_resources(self, admin_client, organization):
        """Retrieve response includes all available_resources for the token."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        resources = [PublicAPIResources.CALENDAR, PublicAPIResources.USER]
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)

        response = admin_client.get(self._url(system_user.id))
        assert_response_status_code(response, status.HTTP_200_OK)
        data = response.json()

        returned_resources = set(data["available_resources"])
        assert returned_resources == set(resources)

    def test_retrieve_does_not_include_token_or_hash(self, admin_client, organization):
        """Retrieve response must never include token or long_lived_token_hash."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        response = admin_client.get(self._url(system_user.id))
        assert_response_status_code(response, status.HTTP_200_OK)
        data = response.json()

        assert "token" not in data
        assert "long_lived_token_hash" not in data

    def test_retrieve_cross_org_token_returns_404(
        self, admin_client, organization, other_organization
    ):
        """Attempt to retrieve a token from another org returns 404."""
        system_user = baker.make(SystemUser, organization=other_organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        response = admin_client.get(self._url(system_user.id))
        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    # ------------------------------------------------------------------
    # Permission / auth failures
    # ------------------------------------------------------------------

    def test_member_user_gets_403(self, member_client, organization):
        """A non-admin member is rejected with HTTP 403."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        response = member_client.get(self._url(system_user.id))
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_membership_less_user_gets_403(self, membership_less_client, organization):
        """A user with no membership is rejected with HTTP 403."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        response = membership_less_client.get(self._url(system_user.id))
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_anonymous_user_gets_401(self, anonymous_client, organization):
        """An unauthenticated request is rejected with HTTP 401."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        response = anonymous_client.get(self._url(system_user.id))
        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)


@pytest.mark.django_db
class TestSystemUserTokenViewSetRevoke:
    """POST /public-api-tokens/{id}/revoke/ — Phase 14 revoke action."""

    def _url(self, token_id):
        return reverse("api:PublicAPITokens-revoke", kwargs={"pk": token_id})

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_admin_revokes_token_returns_200(self, admin_client, organization):
        """Admin can revoke a token; 200 returned."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        response = admin_client.post(self._url(system_user.id))
        assert_response_status_code(response, status.HTTP_200_OK)

    def test_revoke_sets_is_active_false(self, admin_client, organization):
        """Revoking a token sets SystemUser.is_active to False."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        response = admin_client.post(self._url(system_user.id))
        assert_response_status_code(response, status.HTTP_200_OK)

        # Verify the token is now inactive in DB
        system_user.refresh_from_db()
        assert system_user.is_active is False

    def test_revoke_response_includes_required_fields(self, admin_client, organization):
        """Revoke response includes id, integration_name, is_active, available_resources."""
        system_user = baker.make(
            SystemUser,
            organization=organization,
            integration_name="revoke_test",
            is_active=True,
        )
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        response = admin_client.post(self._url(system_user.id))
        assert_response_status_code(response, status.HTTP_200_OK)
        data = response.json()

        assert "id" in data
        assert data["id"] == system_user.id
        assert "integration_name" in data
        assert data["integration_name"] == "revoke_test"
        assert "is_active" in data
        assert data["is_active"] is False
        assert "available_resources" in data

    def test_revoke_response_does_not_include_token(self, admin_client, organization):
        """Revoke response must never include token or long_lived_token_hash."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        response = admin_client.post(self._url(system_user.id))
        assert_response_status_code(response, status.HTTP_200_OK)
        data = response.json()

        assert "token" not in data
        assert "long_lived_token_hash" not in data

    def test_revoke_makes_token_fail_verification(
        self, admin_client, admin_user, organization, di_container
    ):
        """After revoke, check_system_user_token returns (user, False)."""
        # Create a token and capture the plaintext
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        # Simulate token creation flow to get the plaintext
        public_api_auth_service = di_container.public_api_auth_service()
        system_user, plaintext_token = public_api_auth_service.create_system_user(
            integration_name="verify_revoke_test",
            organization=organization,
        )
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        # Verify token works before revoke
        _, is_valid_before = public_api_auth_service.check_system_user_token(
            system_user.id, plaintext_token
        )
        assert is_valid_before is True

        # Revoke the token
        response = admin_client.post(self._url(system_user.id))
        assert_response_status_code(response, status.HTTP_200_OK)

        # Verify token now fails
        _, is_valid_after = public_api_auth_service.check_system_user_token(
            system_user.id, plaintext_token
        )
        assert is_valid_after is False

    def test_revoking_already_revoked_token_is_idempotent(self, admin_client, organization):
        """Revoking an already-revoked token is a 200 no-op."""
        system_user = baker.make(SystemUser, organization=organization, is_active=False)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        # Revoke an already-inactive token
        response = admin_client.post(self._url(system_user.id))
        assert_response_status_code(response, status.HTTP_200_OK)

        # Verify it remains inactive
        system_user.refresh_from_db()
        assert system_user.is_active is False

    def test_revoke_cross_org_token_returns_404(
        self, admin_client, organization, other_organization
    ):
        """Attempt to revoke a token from another org returns 404."""
        system_user = baker.make(SystemUser, organization=other_organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        response = admin_client.post(self._url(system_user.id))
        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    # ------------------------------------------------------------------
    # Permission / auth failures
    # ------------------------------------------------------------------

    def test_member_user_gets_403(self, member_client, organization):
        """A non-admin member is rejected with HTTP 403."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        response = member_client.post(self._url(system_user.id))
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_membership_less_user_gets_403(self, membership_less_client, organization):
        """A user with no membership is rejected with HTTP 403."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        response = membership_less_client.post(self._url(system_user.id))
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_anonymous_user_gets_401(self, anonymous_client, organization):
        """An unauthenticated request is rejected with HTTP 401."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        response = anonymous_client.post(self._url(system_user.id))
        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)


@pytest.mark.django_db
class TestSystemUserTokenViewSetUpdate:
    """PATCH /public-api-tokens/{id}/ — Phase 15 update action."""

    def _url(self, token_id):
        return reverse("api:PublicAPITokens-detail", kwargs={"pk": token_id})

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_admin_patches_token_returns_200(self, admin_client, organization):
        """Admin can PATCH a token to update resource grants; 200 returned."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        payload = {"available_resources": [PublicAPIResources.USER]}
        response = admin_client.patch(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

    def test_patch_replaces_resource_grants_exactly(self, admin_client, organization):
        """PATCH replaces the token's ResourceAccess rows; old grants removed, new ones added."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        initial_resources = [
            PublicAPIResources.CALENDAR,
            PublicAPIResources.CALENDAR_EVENT,
        ]
        for res in initial_resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=res)

        # Patch to replace with different resources
        new_resources = [PublicAPIResources.USER, PublicAPIResources.ORGANIZATION]
        payload = {"available_resources": new_resources}
        response = admin_client.patch(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

        # Verify DB has EXACTLY the new set (old removed, new added)
        persisted = set(
            ResourceAccess.objects.filter(system_user=system_user).values_list(
                "resource_name", flat=True
            )
        )
        assert persisted == set(new_resources)

    def test_patch_adds_resources_when_empty_initially(self, admin_client, organization):
        """PATCH adds resources to a token that initially has none."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        # Create with no ResourceAccess rows initially
        assert ResourceAccess.objects.filter(system_user=system_user).count() == 0

        # Patch to grant resources
        new_resources = [PublicAPIResources.CALENDAR, PublicAPIResources.USER]
        payload = {"available_resources": new_resources}
        response = admin_client.patch(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

        # Verify resources were added
        persisted = set(
            ResourceAccess.objects.filter(system_user=system_user).values_list(
                "resource_name", flat=True
            )
        )
        assert persisted == set(new_resources)

    def test_patch_removes_all_resources_when_none_desired(self, admin_client, organization):
        """PATCH fails when trying to set empty available_resources (validation error)."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        # Attempt to PATCH with empty list
        payload = {"available_resources": []}
        response = admin_client.patch(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_patch_response_includes_updated_resources(self, admin_client, organization):
        """PATCH response includes the new available_resources list."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        new_resources = [PublicAPIResources.USER, PublicAPIResources.ORGANIZATION]
        payload = {"available_resources": new_resources}
        response = admin_client.patch(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

        returned_resources = set(response.json()["available_resources"])
        assert returned_resources == set(new_resources)

    def test_patch_does_not_change_token_hash(self, admin_client, organization):
        """PATCH does NOT change the SystemUser.long_lived_token_hash."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        original_hash = system_user.long_lived_token_hash
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        payload = {"available_resources": [PublicAPIResources.USER]}
        response = admin_client.patch(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

        # Verify the hash unchanged in DB
        system_user.refresh_from_db()
        assert system_user.long_lived_token_hash == original_hash

    def test_patch_does_not_return_token_in_response(self, admin_client, organization):
        """PATCH response must never include the token plaintext or hash."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        payload = {"available_resources": [PublicAPIResources.USER]}
        response = admin_client.patch(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

        data = response.json()
        assert "token" not in data
        assert "long_lived_token_hash" not in data

    def test_patch_ignores_integration_name_in_body(self, admin_client, organization):
        """PATCH ignores integration_name if sent in the request body."""
        system_user = baker.make(
            SystemUser,
            organization=organization,
            integration_name="original_name",
            is_active=True,
        )
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        # Attempt to change integration_name via PATCH
        payload = {
            "available_resources": [PublicAPIResources.USER],
            "integration_name": "different_name",
        }
        response = admin_client.patch(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

        # Verify integration_name unchanged in DB
        system_user.refresh_from_db()
        assert system_user.integration_name == "original_name"

    def test_patch_deduplicates_duplicate_desired_resources(self, admin_client, organization):
        """If the request lists the same resource twice, de-duplication works."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        # Send duplicates in the list
        payload = {
            "available_resources": [
                PublicAPIResources.USER,
                PublicAPIResources.USER,
                PublicAPIResources.ORGANIZATION,
            ]
        }
        response = admin_client.patch(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

        # Verify only one row per resource exists (no duplicates)
        persisted = list(
            ResourceAccess.objects.filter(system_user=system_user).values_list(
                "resource_name", flat=True
            )
        )
        assert persisted.count(PublicAPIResources.USER) == 1
        assert len(persisted) == 2  # Only USER and ORGANIZATION

    def test_patch_response_includes_required_fields(self, admin_client, organization):
        """PATCH response includes id, integration_name, is_active, available_resources."""
        system_user = baker.make(
            SystemUser,
            organization=organization,
            integration_name="patch_test",
            is_active=True,
        )
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        payload = {"available_resources": [PublicAPIResources.USER]}
        response = admin_client.patch(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)
        data = response.json()

        assert "id" in data
        assert data["id"] == system_user.id
        assert "integration_name" in data
        assert data["integration_name"] == "patch_test"
        assert "is_active" in data
        assert data["is_active"] is True
        assert "available_resources" in data

    def test_patch_does_not_change_is_active_status(self, admin_client, organization):
        """PATCH does NOT change the SystemUser.is_active field."""
        system_user = baker.make(SystemUser, organization=organization, is_active=False)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        payload = {"available_resources": [PublicAPIResources.USER]}
        response = admin_client.patch(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

        # Verify is_active unchanged (still False)
        system_user.refresh_from_db()
        assert system_user.is_active is False

    # ------------------------------------------------------------------
    # Validation errors
    # ------------------------------------------------------------------

    def test_patch_invalid_resource_value_returns_400(self, admin_client, organization):
        """An unrecognised resource value in PATCH yields HTTP 400."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        payload = {"available_resources": ["not_a_real_resource"]}
        response = admin_client.patch(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_patch_missing_available_resources_returns_400(self, admin_client, organization):
        """Missing available_resources field in PATCH yields HTTP 400."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        payload = {}
        response = admin_client.patch(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    # ------------------------------------------------------------------
    # Permission / auth failures
    # ------------------------------------------------------------------

    def test_patch_member_user_gets_403(self, member_client, organization):
        """A non-admin member is rejected with HTTP 403 on PATCH."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        payload = {"available_resources": [PublicAPIResources.USER]}
        response = member_client.patch(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_patch_membership_less_user_gets_403(self, membership_less_client, organization):
        """A user with no membership is rejected with HTTP 403 on PATCH."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        payload = {"available_resources": [PublicAPIResources.USER]}
        response = membership_less_client.patch(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_patch_anonymous_user_gets_401(self, anonymous_client, organization):
        """An unauthenticated request is rejected with HTTP 401 on PATCH."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        payload = {"available_resources": [PublicAPIResources.USER]}
        response = anonymous_client.patch(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    # ------------------------------------------------------------------
    # Org scoping
    # ------------------------------------------------------------------

    def test_patch_cross_org_token_returns_404(
        self, admin_client, organization, other_organization
    ):
        """Attempt to PATCH a token from another org returns 404."""
        system_user = baker.make(SystemUser, organization=other_organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        payload = {"available_resources": [PublicAPIResources.USER]}
        response = admin_client.patch(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)


@pytest.mark.django_db
class TestSystemUserTokenViewSetPartialUpdate:
    """PUT /public-api-tokens/{id}/ — Phase 15 full update action."""

    def _url(self, token_id):
        return reverse("api:PublicAPITokens-detail", kwargs={"pk": token_id})

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_admin_puts_token_returns_200(self, admin_client, organization):
        """Admin can PUT a token to update resource grants; 200 returned."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        payload = {"available_resources": [PublicAPIResources.USER]}
        response = admin_client.put(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

    def test_put_replaces_resource_grants_exactly(self, admin_client, organization):
        """PUT replaces the token's ResourceAccess rows; old grants removed, new ones added."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        initial_resources = [
            PublicAPIResources.CALENDAR,
            PublicAPIResources.CALENDAR_EVENT,
        ]
        for res in initial_resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=res)

        # PUT to replace with different resources
        new_resources = [PublicAPIResources.USER, PublicAPIResources.ORGANIZATION]
        payload = {"available_resources": new_resources}
        response = admin_client.put(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

        # Verify DB has EXACTLY the new set
        persisted = set(
            ResourceAccess.objects.filter(system_user=system_user).values_list(
                "resource_name", flat=True
            )
        )
        assert persisted == set(new_resources)

    def test_put_response_includes_required_fields(self, admin_client, organization):
        """PUT response includes id, integration_name, is_active, available_resources."""
        system_user = baker.make(
            SystemUser,
            organization=organization,
            integration_name="put_test",
            is_active=True,
        )
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        payload = {"available_resources": [PublicAPIResources.USER]}
        response = admin_client.put(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)
        data = response.json()

        assert "id" in data
        assert data["id"] == system_user.id
        assert "integration_name" in data
        assert data["integration_name"] == "put_test"
        assert "is_active" in data
        assert "available_resources" in data

    # ------------------------------------------------------------------
    # Permission / auth failures
    # ------------------------------------------------------------------

    def test_put_member_user_gets_403(self, member_client, organization):
        """A non-admin member is rejected with HTTP 403 on PUT."""
        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        payload = {"available_resources": [PublicAPIResources.USER]}
        response = member_client.put(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_put_cross_org_token_returns_404(self, admin_client, organization, other_organization):
        """Attempt to PUT a token from another org returns 404."""
        system_user = baker.make(SystemUser, organization=other_organization, is_active=True)
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        payload = {"available_resources": [PublicAPIResources.USER]}
        response = admin_client.put(self._url(system_user.id), payload, format="json")
        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)


@pytest.mark.django_db
class TestSystemUserTokenViewSetScopedCreate:
    """POST /public-api-tokens/ — Phase 3 owner-scoped token creation."""

    CREATE_URL = "api:PublicAPITokens-list"

    def _url(self):
        return reverse(self.CREATE_URL)

    def _detail_url(self, token_id):
        return reverse("api:PublicAPITokens-detail", kwargs={"pk": token_id})

    @pytest.fixture
    def provider_user(self, organization):
        """A User that is an active member of the test organization (the owner to scope to)."""
        user = baker.make(User, email="provider@example.com")
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
    def outside_user(self, other_organization):
        """A User that belongs to a different organization (cross-org isolation test)."""
        user = baker.make(User, email="outside@example.com")
        baker.make(Profile, user=user)
        baker.make(
            OrganizationMembership,
            user=user,
            organization=other_organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )
        return user

    # ------------------------------------------------------------------
    # Happy path — scoped token creation
    # ------------------------------------------------------------------

    def test_admin_creates_scoped_token_returns_201(
        self, admin_client, organization, provider_user
    ):
        """Admin can create a scoped token; 201 returned with scoped_to_user set."""
        # Use two resources from PROVIDER_SCOPED_RESOURCES
        provider_resources = [PublicAPIResources.CALENDAR, PublicAPIResources.AVAILABLE_TIME]
        assert all(r in PROVIDER_SCOPED_RESOURCES for r in provider_resources)

        payload = {
            "integration_name": "scoped_token_test",
            "available_resources": provider_resources,
            "scoped_to_user": provider_user.id,
        }
        response = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)

    def test_scoped_token_response_includes_owner_id(
        self, admin_client, organization, provider_user
    ):
        """Response scoped_to_user matches the supplied owner id."""
        payload = {
            "integration_name": "scoped_owner_check",
            "available_resources": [PublicAPIResources.CALENDAR],
            "scoped_to_user": provider_user.id,
        }
        response = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)
        assert response.json()["scoped_to_user"] == provider_user.id

    def test_scoped_token_db_has_owner_and_token(self, admin_client, organization, provider_user):
        """DB SystemUser.scoped_to_user_id matches the owner; token is non-empty in response."""
        payload = {
            "integration_name": "scoped_db_check",
            "available_resources": [PublicAPIResources.CALENDAR],
            "scoped_to_user": provider_user.id,
        }
        response = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)
        data = response.json()
        # DB row must reference the owner
        system_user = SystemUser.objects.get(integration_name="scoped_db_check")
        assert system_user.scoped_to_user_id == provider_user.id
        # Plaintext token must be present in response exactly once
        assert "token" in data
        assert data["token"]  # non-empty string

    # ------------------------------------------------------------------
    # Backward-compat: no-owner path unchanged
    # ------------------------------------------------------------------

    def test_no_owner_create_returns_201_with_null_scoped_to_user(self, admin_client, organization):
        """Create without scoped_to_user returns 201; response scoped_to_user is null."""
        payload = {
            "integration_name": "unscoped_token",
            "available_resources": [PublicAPIResources.CALENDAR],
        }
        response = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)
        assert response.json()["scoped_to_user"] is None

    def test_explicit_null_owner_create_returns_201(self, admin_client, organization):
        """POSTing scoped_to_user: null explicitly is identical to omitting it — 201, null back."""
        payload = {
            "integration_name": "explicit_null_owner",
            "available_resources": [PublicAPIResources.CALENDAR],
            "scoped_to_user": None,
        }
        response = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)
        assert response.json()["scoped_to_user"] is None

    def test_no_owner_create_accepts_non_provider_resource(self, admin_client, organization):
        """Create without owner accepts resources outside PROVIDER_SCOPED_RESOURCES (e.g. user).

        This proves the provider allow-list did NOT leak onto the no-owner path.
        """
        non_provider_resource = PublicAPIResources.USER
        assert non_provider_resource not in PROVIDER_SCOPED_RESOURCES

        payload = {
            "integration_name": "unscoped_user_resource",
            "available_resources": [non_provider_resource],
        }
        response = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)
        returned_resources = response.json()["available_resources"]
        assert non_provider_resource in returned_resources

    # ------------------------------------------------------------------
    # Validation errors — scoped path
    # ------------------------------------------------------------------

    def test_over_grant_with_owner_returns_400(self, admin_client, organization, provider_user):
        """Supplying a resource outside PROVIDER_SCOPED_RESOURCES with an owner yields 400."""
        non_provider_resource = PublicAPIResources.USER
        assert non_provider_resource not in PROVIDER_SCOPED_RESOURCES

        payload = {
            "integration_name": "over_grant_test",
            "available_resources": [non_provider_resource],
            "scoped_to_user": provider_user.id,
        }
        response = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_over_grant_with_owner_does_not_create_system_user(
        self, admin_client, organization, provider_user
    ):
        """A rejected over-grant must not leave a SystemUser row in the DB."""
        payload = {
            "integration_name": "over_grant_no_create",
            "available_resources": [PublicAPIResources.USER],
            "scoped_to_user": provider_user.id,
        }
        admin_client.post(self._url(), payload, format="json")
        assert not SystemUser.objects.filter(integration_name="over_grant_no_create").exists()

    def test_owner_outside_org_returns_400(self, admin_client, organization, outside_user):
        """Owner from a different org yields 400 (cross-org mint rejected)."""
        payload = {
            "integration_name": "cross_org_owner_test",
            "available_resources": [PublicAPIResources.CALENDAR],
            "scoped_to_user": outside_user.id,
        }
        response = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_owner_outside_org_does_not_create_system_user(
        self, admin_client, organization, outside_user
    ):
        """A cross-org owner rejection must not leave a SystemUser row in the DB."""
        payload = {
            "integration_name": "cross_org_no_create",
            "available_resources": [PublicAPIResources.CALENDAR],
            "scoped_to_user": outside_user.id,
        }
        admin_client.post(self._url(), payload, format="json")
        assert not SystemUser.objects.filter(integration_name="cross_org_no_create").exists()

    def test_nonexistent_owner_id_returns_400(self, admin_client, organization):
        """A scoped_to_user id that does not exist in the DB yields 400."""
        payload = {
            "integration_name": "nonexistent_owner_test",
            "available_resources": [PublicAPIResources.CALENDAR],
            "scoped_to_user": 999999999,
        }
        response = admin_client.post(self._url(), payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_nonexistent_owner_does_not_create_system_user(self, admin_client, organization):
        """A nonexistent owner rejection must not leave a SystemUser row in the DB."""
        payload = {
            "integration_name": "nonexistent_owner_no_create",
            "available_resources": [PublicAPIResources.CALENDAR],
            "scoped_to_user": 999999999,
        }
        admin_client.post(self._url(), payload, format="json")
        assert not SystemUser.objects.filter(
            integration_name="nonexistent_owner_no_create"
        ).exists()

    # ------------------------------------------------------------------
    # Owner immutability on update (PUT + PATCH)
    # ------------------------------------------------------------------

    def test_put_cannot_change_owner(self, admin_client, organization, provider_user):
        """PUT with a different scoped_to_user in the body must not change the stored owner."""
        # Create another org member to use as the "attempted replacement" owner
        replacement_user = baker.make(User, email="replacement@example.com")
        baker.make(Profile, user=replacement_user)
        baker.make(
            OrganizationMembership,
            user=replacement_user,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        # Create a scoped token
        create_response = admin_client.post(
            self._url(),
            {
                "integration_name": "immutable_owner_put",
                "available_resources": [PublicAPIResources.CALENDAR],
                "scoped_to_user": provider_user.id,
            },
            format="json",
        )
        assert_response_status_code(create_response, status.HTTP_201_CREATED)
        token_id = create_response.json()["id"]

        # Attempt PUT with a different scoped_to_user
        put_response = admin_client.put(
            self._detail_url(token_id),
            {
                "available_resources": [PublicAPIResources.AVAILABLE_TIME],
                "scoped_to_user": replacement_user.id,
            },
            format="json",
        )
        assert_response_status_code(put_response, status.HTTP_200_OK)

        # Owner must remain the original provider_user
        system_user = SystemUser.objects.get(pk=token_id)
        assert system_user.scoped_to_user_id == provider_user.id

    def test_patch_cannot_change_owner(self, admin_client, organization, provider_user):
        """PATCH with a different scoped_to_user in the body must not change the stored owner."""
        replacement_user = baker.make(User, email="patch_replacement@example.com")
        baker.make(Profile, user=replacement_user)
        baker.make(
            OrganizationMembership,
            user=replacement_user,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        # Create a scoped token
        create_response = admin_client.post(
            self._url(),
            {
                "integration_name": "immutable_owner_patch",
                "available_resources": [PublicAPIResources.CALENDAR],
                "scoped_to_user": provider_user.id,
            },
            format="json",
        )
        assert_response_status_code(create_response, status.HTTP_201_CREATED)
        token_id = create_response.json()["id"]

        # Attempt PATCH with a different scoped_to_user
        patch_response = admin_client.patch(
            self._detail_url(token_id),
            {
                "available_resources": [PublicAPIResources.AVAILABLE_TIME],
                "scoped_to_user": replacement_user.id,
            },
            format="json",
        )
        assert_response_status_code(patch_response, status.HTTP_200_OK)

        # Owner must remain the original provider_user
        system_user = SystemUser.objects.get(pk=token_id)
        assert system_user.scoped_to_user_id == provider_user.id


@pytest.mark.django_db
class TestSystemUserTokenUpdateEscalationGuard:
    """Update-path allow-list guard: scoped tokens cannot gain non-provider resources."""

    def _list_url(self):
        return reverse("api:PublicAPITokens-list")

    def _detail_url(self, token_id):
        return reverse("api:PublicAPITokens-detail", kwargs={"pk": token_id})

    @pytest.fixture
    def provider_user(self, organization):
        """A User that is an active member of the test organization."""
        user = baker.make(User, email="guard_provider@example.com")
        baker.make(Profile, user=user)
        baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )
        return user

    def test_update_cannot_add_non_provider_resource_to_scoped_token(
        self, admin_client, organization, provider_user
    ):
        """PUT/PATCH a scoped token with a resource outside PROVIDER_SCOPED_RESOURCES returns 400
        AND the persisted grants are unchanged."""
        non_provider_resource = PublicAPIResources.USER
        assert non_provider_resource not in PROVIDER_SCOPED_RESOURCES

        # Create a scoped token with a safe provider resource.
        create_response = admin_client.post(
            self._list_url(),
            {
                "integration_name": "guard_scoped_token",
                "available_resources": [PublicAPIResources.CALENDAR],
                "scoped_to_user": provider_user.id,
            },
            format="json",
        )
        assert_response_status_code(create_response, status.HTTP_201_CREATED)
        token_id = create_response.json()["id"]

        # Capture grants before the attempted escalation.
        grants_before = set(
            ResourceAccess.objects.filter(system_user_id=token_id).values_list(
                "resource_name", flat=True
            )
        )

        # Attempt to PATCH in a non-provider resource.
        patch_response = admin_client.patch(
            self._detail_url(token_id),
            {"available_resources": [PublicAPIResources.CALENDAR, non_provider_resource]},
            format="json",
        )
        assert_response_status_code(patch_response, status.HTTP_400_BAD_REQUEST)

        # Grants must be unchanged.
        grants_after = set(
            ResourceAccess.objects.filter(system_user_id=token_id).values_list(
                "resource_name", flat=True
            )
        )
        assert grants_after == grants_before

        # Also verify PUT is blocked.
        put_response = admin_client.put(
            self._detail_url(token_id),
            {"available_resources": [non_provider_resource]},
            format="json",
        )
        assert_response_status_code(put_response, status.HTTP_400_BAD_REQUEST)

        # Grants still unchanged after PUT attempt.
        grants_final = set(
            ResourceAccess.objects.filter(system_user_id=token_id).values_list(
                "resource_name", flat=True
            )
        )
        assert grants_final == grants_before

    def test_update_org_wide_token_still_accepts_any_resource(self, admin_client, organization):
        """An org-wide token (no owner) can be updated to include a non-provider resource.
        This proves the guard is scoped-only and did not break org-wide editing."""
        non_provider_resource = PublicAPIResources.USER
        assert non_provider_resource not in PROVIDER_SCOPED_RESOURCES

        # Create an org-wide token.
        system_user = baker.make(
            SystemUser, organization=organization, is_active=True, scoped_to_user=None
        )
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        # PATCH in a non-provider resource — must succeed.
        patch_response = admin_client.patch(
            self._detail_url(system_user.id),
            {"available_resources": [non_provider_resource]},
            format="json",
        )
        assert_response_status_code(patch_response, status.HTTP_200_OK)

        # Verify the non-provider resource is now persisted.
        persisted = set(
            ResourceAccess.objects.filter(system_user=system_user).values_list(
                "resource_name", flat=True
            )
        )
        assert non_provider_resource in persisted
