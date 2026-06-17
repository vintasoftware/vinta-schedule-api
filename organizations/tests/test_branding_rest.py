import json

from django.contrib.auth import get_user_model

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from organizations.models import (
    Organization,
    OrganizationBranding,
    OrganizationMembership,
    OrganizationRole,
)


User = get_user_model()

BRANDING_URL = "/branding/"


def assert_response_status_code(response, expected_status_code):
    """Assert response status code with helpful error message."""
    assert response.status_code == expected_status_code, (
        f"The status error {response.status_code} != {expected_status_code}\n"
        f"Response Payload: {json.dumps(response.json() if hasattr(response, 'json') and callable(response.json) else str(response.content))}"
    )


@pytest.fixture
def client():
    """REST API client."""
    return APIClient()


@pytest.fixture
def user():
    """Create a test user."""
    return baker.make(User)


@pytest.fixture
def reseller_org():
    """Create a reseller organization."""
    return baker.make(Organization, can_invite_organizations=True)


@pytest.fixture
def non_reseller_org():
    """Create a non-reseller organization."""
    return baker.make(Organization, can_invite_organizations=False)


@pytest.fixture
def reseller_org_admin(user, reseller_org):
    """Create an admin membership for the user in the reseller org."""
    return baker.make(
        OrganizationMembership,
        user=user,
        organization=reseller_org,
        role=OrganizationRole.ADMIN,
        is_active=True,
    )


@pytest.fixture
def non_reseller_org_admin(user, non_reseller_org):
    """Create an admin membership for the user in a non-reseller org."""
    return baker.make(
        OrganizationMembership,
        user=user,
        organization=non_reseller_org,
        role=OrganizationRole.ADMIN,
        is_active=True,
    )


@pytest.fixture
def reseller_org_member(reseller_org):
    """Create a non-admin member in the reseller org."""
    member = baker.make(User)
    baker.make(
        OrganizationMembership,
        user=member,
        organization=reseller_org,
        role=OrganizationRole.MEMBER,
        is_active=True,
    )
    return member


@pytest.mark.django_db
class TestOrganizationBrandingViewSet:
    """Test suite for OrganizationBrandingViewSet REST endpoints."""

    def test_retrieve_branding_not_configured_returns_404(
        self, client, user, reseller_org, reseller_org_admin
    ):
        """GET /branding/ returns 404 when branding is not yet configured."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        response = client.get(BRANDING_URL)
        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_retrieve_branding_success(self, client, user, reseller_org, reseller_org_admin):
        """GET /branding/ returns branding when configured."""
        _ = baker.make(
            OrganizationBranding,
            organization=reseller_org,
            app_name="MyScheduler",
            logo_url="https://example.com/logo.png",
            primary_color="#FF0000",
            secondary_color="#00FF00",
            support_email="support@example.com",
            return_url_allowlist=["https://example.com", "https://app.example.com"],
        )

        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        response = client.get(BRANDING_URL)
        assert_response_status_code(response, status.HTTP_200_OK)

        data = response.json()
        assert data["app_name"] == "MyScheduler"
        assert data["logo_url"] == "https://example.com/logo.png"
        assert data["primary_color"] == "#FF0000"
        assert data["secondary_color"] == "#00FF00"
        assert data["support_email"] == "support@example.com"
        assert data["return_url_allowlist"] == [
            "https://example.com",
            "https://app.example.com",
        ]

    def test_create_branding_via_put(self, client, user, reseller_org, reseller_org_admin):
        """PUT /branding/ creates branding (upsert)."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "MyScheduler",
            "logo_url": "https://example.com/logo.png",
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "support_email": "support@example.com",
            "return_url_allowlist": ["https://example.com"],
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)

        data = response.json()
        assert data["app_name"] == "MyScheduler"
        assert data["support_email"] == "support@example.com"

        # Verify branding was created in DB
        branding = OrganizationBranding.objects.get(organization_id=reseller_org.id)
        assert branding.app_name == "MyScheduler"

    def test_update_branding_via_put_replaces_all_fields(
        self, client, user, reseller_org, reseller_org_admin
    ):
        """PUT /branding/ replaces entire branding row (upsert)."""
        baker.make(
            OrganizationBranding,
            organization=reseller_org,
            app_name="OldName",
            logo_url="https://old.example.com/logo.png",
            primary_color="#0000FF",
            secondary_color="#FFFF00",
            support_email="old@example.com",
            return_url_allowlist=["https://old.example.com"],
        )

        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "NewName",
            "logo_url": "https://new.example.com/logo.png",
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "support_email": "new@example.com",
            "return_url_allowlist": ["https://new.example.com"],
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

        data = response.json()
        assert data["app_name"] == "NewName"
        assert data["support_email"] == "new@example.com"

    def test_update_branding_via_patch_partial(
        self, client, user, reseller_org, reseller_org_admin
    ):
        """PATCH /branding/ updates partial fields."""
        _ = baker.make(
            OrganizationBranding,
            organization=reseller_org,
            app_name="OriginalName",
            logo_url="https://original.example.com/logo.png",
            primary_color="#FF0000",
            secondary_color="#00FF00",
            support_email="original@example.com",
            return_url_allowlist=["https://original.example.com"],
        )

        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "UpdatedName",
            "support_email": "updated@example.com",
        }

        response = client.patch(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

        data = response.json()
        assert data["app_name"] == "UpdatedName"
        assert data["support_email"] == "updated@example.com"
        # Unchanged fields should remain
        assert data["logo_url"] == "https://original.example.com/logo.png"
        assert data["primary_color"] == "#FF0000"

    def test_patch_branding_when_not_configured_returns_404(
        self, client, user, reseller_org, reseller_org_admin
    ):
        """PATCH /branding/ returns 404 when branding is not configured."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {"app_name": "NewName"}

        response = client.patch(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_invalid_color_format_returns_400(self, client, user, reseller_org, reseller_org_admin):
        """Invalid color format (#RGB instead of #RRGGBB) returns 400."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "MyScheduler",
            "primary_color": "#RGB",  # Invalid
            "secondary_color": "#00FF00",
            "logo_url": "",
            "support_email": "",
            "return_url_allowlist": [],
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_valid_color_with_alpha_accepted(self, client, user, reseller_org, reseller_org_admin):
        """Color format #RRGGBBAA (with alpha) is accepted."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "MyScheduler",
            "primary_color": "#FF0000AA",  # With alpha
            "secondary_color": "#00FF00",
            "logo_url": "",
            "support_email": "",
            "return_url_allowlist": [],
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)

    def test_invalid_url_in_allowlist_returns_400(
        self, client, user, reseller_org, reseller_org_admin
    ):
        """Invalid URL in return_url_allowlist returns 400."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "MyScheduler",
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "logo_url": "",
            "support_email": "",
            "return_url_allowlist": ["not-a-valid-url"],  # Invalid URL
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_valid_urls_in_allowlist_accepted(self, client, user, reseller_org, reseller_org_admin):
        """Valid URLs in return_url_allowlist are accepted."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "MyScheduler",
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "logo_url": "https://example.com/logo.png",
            "support_email": "support@example.com",
            "return_url_allowlist": ["https://example.com", "http://localhost:3000"],
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)

    def test_non_reseller_org_returns_403(self, client, user, non_reseller_org_admin):
        """Non-reseller org admin gets 403."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(non_reseller_org_admin.organization_id))

        payload = {
            "app_name": "MyScheduler",
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "logo_url": "",
            "support_email": "",
            "return_url_allowlist": [],
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_non_admin_member_returns_403(self, client, reseller_org, reseller_org_member):
        """Non-admin member of reseller org gets 403."""
        client.force_authenticate(reseller_org_member)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "MyScheduler",
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "logo_url": "",
            "support_email": "",
            "return_url_allowlist": [],
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_user_returns_401(self, client, reseller_org):
        """Unauthenticated user gets 401."""
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "MyScheduler",
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "logo_url": "",
            "support_email": "",
            "return_url_allowlist": [],
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    def test_serializer_never_exposes_can_invite_organizations(
        self, client, user, reseller_org, reseller_org_admin
    ):
        """can_invite_organizations is never exposed in the serializer."""
        _ = baker.make(
            OrganizationBranding,
            organization=reseller_org,
            app_name="MyScheduler",
            logo_url="",
            primary_color="",
            secondary_color="",
            support_email="",
            return_url_allowlist=[],
        )

        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        response = client.get(BRANDING_URL)
        assert_response_status_code(response, status.HTTP_200_OK)

        data = response.json()
        assert "can_invite_organizations" not in data
        assert "organization" not in data

    def test_only_acting_org_branding_accessible(
        self, client, user, reseller_org, reseller_org_admin
    ):
        """A user can only access their own org's branding, not another org's."""
        other_reseller = baker.make(Organization, can_invite_organizations=True)
        _ = baker.make(
            OrganizationBranding,
            organization=other_reseller,
            app_name="OtherApp",
            logo_url="",
            primary_color="",
            secondary_color="",
            support_email="",
            return_url_allowlist=[],
        )

        baker.make(
            OrganizationBranding,
            organization=reseller_org,
            app_name="MyApp",
            logo_url="",
            primary_color="",
            secondary_color="",
            support_email="",
            return_url_allowlist=[],
        )

        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        response = client.get(BRANDING_URL)
        assert_response_status_code(response, status.HTTP_200_OK)

        data = response.json()
        # Should see my branding, not the other org's
        assert data["app_name"] == "MyApp"

    def test_roundtrip_all_fields(self, client, user, reseller_org, reseller_org_admin):
        """Create and retrieve branding; all fields round-trip."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        original_payload = {
            "app_name": "ClinicScheduler",
            "logo_url": "https://clinic.example.com/logo.png",
            "primary_color": "#0066CC",
            "secondary_color": "#FF6633",
            "support_email": "help@clinic.example.com",
            "return_url_allowlist": [
                "https://clinic.example.com",
                "https://app.clinic.example.com",
            ],
        }

        # Create branding
        response = client.put(BRANDING_URL, data=original_payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)

        # Retrieve and verify all fields match
        response = client.get(BRANDING_URL)
        assert_response_status_code(response, status.HTTP_200_OK)

        data = response.json()
        assert data["app_name"] == original_payload["app_name"]
        assert data["logo_url"] == original_payload["logo_url"]
        assert data["primary_color"] == original_payload["primary_color"]
        assert data["secondary_color"] == original_payload["secondary_color"]
        assert data["support_email"] == original_payload["support_email"]
        assert data["return_url_allowlist"] == original_payload["return_url_allowlist"]

    def test_delete_not_allowed(self, client, user, reseller_org, reseller_org_admin):
        """DELETE /branding/ returns 403."""
        baker.make(
            OrganizationBranding,
            organization=reseller_org,
            app_name="MyScheduler",
            logo_url="",
            primary_color="",
            secondary_color="",
            support_email="",
            return_url_allowlist=[],
        )

        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        response = client.delete(BRANDING_URL)
        # DELETE should not be in allowed methods (405)
        assert response.status_code in (
            status.HTTP_403_FORBIDDEN,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )
