"""Integration tests for single-use booking-code revoke mutation.

Covers revokeBookingCode.
"""

import datetime

from django.contrib.auth import get_user_model
from django.utils import timezone

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarManagementTokenKind
from calendar_integration.models import (
    Calendar,
    CalendarManagementToken,
    EventManagementPermissions,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from organizations.models import Organization, OrganizationMembership
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


def _make_member(organization: Organization) -> OrganizationMembership:
    """Create a plain org member, independent of the booking-code system user."""
    user = get_user_model().objects.create_user(
        email=f"member-{organization.id}-{OrganizationMembership.objects.count()}@example.com",
        password="pw",  # noqa: S106
    )
    return OrganizationMembership.objects.create(
        user=user, organization=organization, is_active=True
    )


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
            minted_by_system_user=system_user,
            kind=CalendarManagementTokenKind.BOOKING_CODE,
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
        original_revoked_at = timezone.now()
        token_obj = baker.make(
            CalendarManagementToken,
            organization=organization,
            calendar=calendar,
            token_hash="dummy_hash",
            revoked_at=original_revoked_at,
            used_at=None,
            minted_by_system_user=system_user,
            kind=CalendarManagementTokenKind.BOOKING_CODE,
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

    def test_revoke_cannot_touch_calendar_owner_token(
        self,
        organization,
        calendar,
        system_user_with_booking_code_resource,
    ):
        """A calendar-owner token (create_calendar_owner_token) is not a booking
        code and must be indistinguishable from a nonexistent id through this
        mutation -- CalendarPermissionService.revoke_token restricts itself to
        booking codes via the same discriminator the REST surface uses
        (CalendarManagementTokenManager.booking_codes_for_organization).
        """
        system_user, token, auth_service = system_user_with_booking_code_resource
        owner_membership = _make_member(organization)

        service = CalendarPermissionService()
        owner_token = service.create_calendar_owner_token(
            organization_id=organization.id,
            user=owner_membership.user,
            calendar_id=calendar.id,
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": organization.id, "id": owner_token.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["revokeBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"
        assert result["errorMessage"] == "Not found."

        owner_token.refresh_from_db()
        assert owner_token.revoked_at is None

        # The owner can still act on their own calendar afterwards.
        service.initialize_with_user(
            owner_membership.user,
            organization_id=organization.id,
            calendar_id=calendar.id,
        )
        assert service.token is not None
        assert service.token.id == owner_token.id

    def test_revoke_cannot_touch_attendee_token(
        self,
        organization,
        calendar,
        system_user_with_booking_code_resource,
    ):
        """An attendee token (create_attendee_token) is not a booking code and
        must be indistinguishable from a nonexistent id through this mutation.
        """
        from calendar_integration.models import CalendarEvent

        system_user, token, auth_service = system_user_with_booking_code_resource
        attendee_membership = _make_member(organization)

        event = CalendarEvent.objects.create(
            organization=organization,
            calendar_fk=calendar,
            title="Attendee Event",
            description="",
            external_id="attendee-event-1",
            start_time_tz_unaware=timezone.now(),
            end_time_tz_unaware=timezone.now() + datetime.timedelta(hours=1),
            timezone="UTC",
        )

        service = CalendarPermissionService()
        attendee_token = service.create_attendee_token(
            organization_id=organization.id,
            user=attendee_membership.user,
            event_id=event.id,
        )

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": organization.id, "id": attendee_token.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["revokeBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"
        assert result["errorMessage"] == "Not found."

        attendee_token.refresh_from_db()
        assert attendee_token.revoked_at is None

        # The attendee can still act on their own event token afterwards.
        service.initialize_with_user(
            attendee_membership.user,
            organization_id=organization.id,
            event_id=event.id,
        )
        assert service.token is not None
        assert service.token.id == attendee_token.id

    def test_revoke_audit_entry_names_system_user(
        self,
        organization,
        calendar,
        system_user_with_booking_code_resource,
        django_capture_on_commit_callbacks,
    ):
        """The GraphQL revoke's audit entry names the acting SystemUser, not
        the generic system actor."""
        from unittest.mock import patch

        from di_core.containers import container

        system_user, token, auth_service = system_user_with_booking_code_resource

        token_obj = baker.make(
            CalendarManagementToken,
            organization=organization,
            calendar=calendar,
            token_hash="dummy_hash",
            revoked_at=None,
            used_at=None,
            minted_by_system_user=system_user,
            kind=CalendarManagementTokenKind.BOOKING_CODE,
        )

        with patch("vinta_audit_logs.tasks.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                with container.public_api_auth_service.override(auth_service):
                    response = self.client.post(
                        "/graphql/",
                        data={
                            "query": REVOKE_BOOKING_CODE_MUTATION,
                            "variables": {
                                "input": {"organizationId": organization.id, "id": token_obj.id}
                            },
                        },
                        format="json",
                        headers={"authorization": f"Bearer {system_user.id}:{token}"},
                    )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0
        assert data["data"]["revokeBookingCode"]["success"] is True

        payloads = [call.args[0] for call in mock_task.delay.call_args_list]
        token_payloads = [
            p
            for p in payloads
            if p["subject"]["subject_type"] == "calendar_integration.calendarmanagementtoken"
            and p["action_key"] == "update"
        ]
        assert len(token_payloads) == 1
        payload = token_payloads[0]
        assert payload["actor"]["identity_type"] == "system_user"
        assert payload["actor"]["identity_key"] == str(system_user.id)
