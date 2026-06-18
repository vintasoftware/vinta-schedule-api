"""Integration tests for single-use booking-code revoke mutation.

Covers Phase 3: revokeBookingCode.
"""

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.models import (
    Calendar,
    CalendarManagementToken,
    EventManagementPermissions,
)
from organizations.models import Organization
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


REVOKE_BOOKING_CODE_MUTATION = """
mutation RevokeBookingCode($input: RevokeBookingCodeInput!) {
    revokeBookingCode(input: $input) {
        success
        errorCode
        errorMessage
        code
        id
    }
}
"""


@pytest.fixture
def organization():
    """Create a test organization."""
    return baker.make(Organization, name="Test Organization")


@pytest.fixture
def another_organization():
    """Create another test organization for cross-org tests."""
    return baker.make(Organization, name="Another Organization")


@pytest.fixture
def system_user_with_booking_code_resource(organization):
    """Create a SystemUser + token with CALENDAR_BOOKING_CODE resource access."""
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name="revoke_booking_code_integration", organization=organization
    )
    baker.make(
        ResourceAccess,
        system_user=system_user,
        resource_name=PublicAPIResources.CALENDAR_BOOKING_CODE,
    )
    return system_user, token, auth_service


@pytest.fixture
def system_user_without_booking_code_resource(organization):
    """Create a SystemUser + token WITHOUT CALENDAR_BOOKING_CODE resource access."""
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name="no_revoke_booking_code_integration", organization=organization
    )
    # Deliberately grant a different resource but not CALENDAR_BOOKING_CODE
    baker.make(
        ResourceAccess,
        system_user=system_user,
        resource_name=PublicAPIResources.CALENDAR_EVENT,
    )
    return system_user, token, auth_service


@pytest.fixture
def calendar(organization):
    """Create a test calendar in the organization."""
    return baker.make(Calendar, organization=organization, name="Test Calendar")


@pytest.fixture
def another_org_calendar(another_organization):
    """Create a test calendar in another organization."""
    return baker.make(Calendar, organization=another_organization, name="Other Org Calendar")


@pytest.mark.django_db
class TestRevokeBookingCode:
    """Tests for revokeBookingCode mutation."""

    def setup_method(self):
        self.client = APIClient()

    def _post_mutation(self, system_user, token, auth_service, variables):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={
                    "query": REVOKE_BOOKING_CODE_MUTATION,
                    "variables": variables,
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def test_revoke_unrevoked_code(
        self,
        organization,
        calendar,
        system_user_with_booking_code_resource,
    ):
        """Revoking an active code sets revoked_at and returns success.

        Asserts:
        - Response is success=True.
        - The token row's revoked_at is now set.
        """
        system_user, token, auth_service = system_user_with_booking_code_resource

        # Create a booking code token
        token_obj = baker.make(
            CalendarManagementToken,
            organization=organization,
            calendar=calendar,
            token_hash="dummy_hash",
            revoked_at=None,
            used_at=None,
        )
        baker.make(
            CalendarManagementToken.permissions.field.model,
            token=token_obj,
            permission=EventManagementPermissions.CREATE,
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": organization.id, "id": token_obj.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["revokeBookingCode"]
        assert result["success"] is True
        assert result["errorCode"] is None
        assert result["errorMessage"] is None
        # Revoke should NOT return code or id
        assert result["code"] is None
        assert result["id"] is None

        # Verify the token was revoked in the database
        db_token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=token_obj.id
        )
        assert db_token.revoked_at is not None

    def test_revoke_is_idempotent(
        self,
        organization,
        calendar,
        system_user_with_booking_code_resource,
    ):
        """Revoking an already-revoked code returns success; revoked_at unchanged.

        Asserts:
        - First revoke sets revoked_at.
        - Second revoke returns success.
        - revoked_at timestamp did not change.
        """
        system_user, token, auth_service = system_user_with_booking_code_resource

        # Create a booking code token
        from django.utils import timezone

        original_revoked_at = timezone.now()
        token_obj = baker.make(
            CalendarManagementToken,
            organization=organization,
            calendar=calendar,
            token_hash="dummy_hash",
            revoked_at=original_revoked_at,
            used_at=None,
        )

        # Revoke it again (it's already revoked)
        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": organization.id, "id": token_obj.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["revokeBookingCode"]
        assert result["success"] is True

        # Verify the revoked_at timestamp did not change
        db_token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=token_obj.id
        )
        assert db_token.revoked_at is not None
        # Allow up to 1 second of drift (NTP, system clock, etc.)
        time_diff = abs((db_token.revoked_at - original_revoked_at).total_seconds())
        assert time_diff < 1, "revoked_at timestamp should not have changed"

    def test_revoke_cross_org_token_fails(
        self,
        organization,
        another_organization,
        another_org_calendar,
        system_user_with_booking_code_resource,
    ):
        """Revoking a token from another org returns INVALID_CODE.

        Asserts:
        - Response is success=False, errorCode=INVALID_CODE.
        - The other org's token is NOT revoked.
        """
        system_user, token, auth_service = system_user_with_booking_code_resource

        # Create a booking code token in a different organization
        other_token = baker.make(
            CalendarManagementToken,
            organization=another_organization,
            calendar=another_org_calendar,
            token_hash="other_hash",
            revoked_at=None,
            used_at=None,
        )

        # Try to revoke it from our organization's token
        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": organization.id, "id": other_token.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["revokeBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"
        assert result["errorMessage"] == "Not found."

        # Verify the other org's token was NOT revoked
        db_token = CalendarManagementToken.objects.filter_by_organization(
            another_organization.id
        ).get(id=other_token.id)
        assert db_token.revoked_at is None

    def test_revoke_unknown_code_fails(
        self,
        organization,
        system_user_with_booking_code_resource,
    ):
        """Revoking an unknown code id returns INVALID_CODE."""
        system_user, token, auth_service = system_user_with_booking_code_resource

        # Use a non-existent token id
        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": organization.id, "id": 999999}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["revokeBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"
        assert result["errorMessage"] == "Not found."

    def test_revoke_without_resource_rejected(
        self,
        organization,
        calendar,
        system_user_without_booking_code_resource,
    ):
        """Org token WITHOUT CALENDAR_BOOKING_CODE is rejected."""
        system_user, token, auth_service = system_user_without_booking_code_resource

        # Create a booking code token
        token_obj = baker.make(
            CalendarManagementToken,
            organization=organization,
            calendar=calendar,
            token_hash="dummy_hash",
            revoked_at=None,
            used_at=None,
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": organization.id, "id": token_obj.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

        # Verify the token was NOT revoked
        db_token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=token_obj.id
        )
        assert db_token.revoked_at is None

    def test_revoke_organization_id_mismatch(
        self,
        organization,
        another_organization,
        calendar,
        system_user_with_booking_code_resource,
    ):
        """organizationId mismatch in input returns INVALID_CODE."""
        system_user, token, auth_service = system_user_with_booking_code_resource

        # Create a booking code token in the correct organization
        token_obj = baker.make(
            CalendarManagementToken,
            organization=organization,
            calendar=calendar,
            token_hash="dummy_hash",
            revoked_at=None,
            used_at=None,
        )

        # Try to revoke with a different organization id
        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": another_organization.id, "id": token_obj.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["revokeBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"
        assert result["errorMessage"] == "Not found."

        # Verify the token was NOT revoked
        db_token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=token_obj.id
        )
        assert db_token.revoked_at is None
