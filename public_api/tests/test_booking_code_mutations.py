"""Integration tests for single-use booking-code mint mutations.

Covers createCalendarBookingCode and createAppointmentTypeBookingCode.
"""

import datetime

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarType
from calendar_integration.models import (
    AppointmentType,
    Calendar,
    CalendarManagementToken,
    CalendarManagementTokenPermission,
    EventManagementPermissions,
)
from organizations.models import Organization
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


CREATE_CALENDAR_BOOKING_CODE_MUTATION = """
mutation CreateCalendarBookingCode($input: CreateBookingCodeInput!) {
    createCalendarBookingCode(input: $input) {
        success
        errorCode
        errorMessage
        code
        id
    }
}
"""

CREATE_APPOINTMENT_TYPE_BOOKING_CODE_MUTATION = """
mutation CreateAppointmentTypeBookingCode($input: CreateAppointmentTypeBookingCodeInput!) {
    createAppointmentTypeBookingCode(input: $input) {
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
def system_user_with_booking_code_resource(organization):
    """Create a SystemUser + token with CALENDAR_BOOKING_CODE resource access."""
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name="booking_code_integration", organization=organization
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
        integration_name="no_booking_code_integration", organization=organization
    )
    # Deliberately grant a different resource but not CALENDAR_BOOKING_CODE
    baker.make(
        ResourceAccess,
        system_user=system_user,
        resource_name=PublicAPIResources.CALENDAR,
    )
    return system_user, token, auth_service


@pytest.fixture
def calendar(organization):
    """Create a personal calendar in the test organization."""
    return baker.make(Calendar, organization=organization, name="Test Calendar")


@pytest.fixture
def bundle_calendar(organization):
    """Create a bundle calendar in the test organization."""
    return baker.make(
        Calendar,
        organization=organization,
        name="Bundle Calendar",
        calendar_type=CalendarType.BUNDLE,
    )


@pytest.fixture
def appointment_type(organization):
    """Create an appointment type in the test organization."""
    return baker.make(AppointmentType, organization=organization, name="Test AppointmentType")


@pytest.mark.django_db
class TestCreateCalendarBookingCode:
    """Tests for createCalendarBookingCode mutation."""

    def setup_method(self):
        self.client = APIClient()

    def _post_mutation(self, system_user, token, auth_service, variables):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={
                    "query": CREATE_CALENDAR_BOOKING_CODE_MUTATION,
                    "variables": variables,
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def test_mints_calendar_booking_code_with_resource(
        self,
        organization,
        calendar,
        system_user_with_booking_code_resource,
    ):
        """Org token WITH CALENDAR_BOOKING_CODE mints a calendar code.

        Asserts:
        - Response has a non-empty ``code`` and non-null ``id``.
        - A CalendarManagementToken row is scoped to the calendar.
        - It has a CREATE permission row.
        - minted_by_system_user is set.
        """
        system_user, token, auth_service = system_user_with_booking_code_resource

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": organization.id, "calendarId": calendar.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarBookingCode"]
        assert result["success"] is True
        assert result["code"] is not None and len(result["code"]) > 0
        assert result["id"] is not None
        assert result["errorCode"] is None
        assert result["errorMessage"] is None

        # Verify the token row in the database (must scope query by org — multi-tenancy contract)
        db_token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=result["id"]
        )
        assert db_token.organization_id == organization.id
        assert db_token.calendar_fk_id == calendar.id
        assert db_token.appointment_type_fk_id is None
        assert db_token.minted_by_system_user_id == system_user.id

        # Verify the CREATE permission row
        permissions = list(
            CalendarManagementTokenPermission.objects.filter_by_organization(organization.id)
            .filter(token_fk_id=db_token.id)
            .values_list("permission", flat=True)
        )
        assert permissions == [EventManagementPermissions.CREATE]

    def test_mints_bundle_calendar_booking_code(
        self,
        organization,
        bundle_calendar,
        system_user_with_booking_code_resource,
    ):
        """Bundle calendars are transparently handled by the calendar mint mutation."""
        system_user, token, auth_service = system_user_with_booking_code_resource

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": organization.id, "calendarId": bundle_calendar.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarBookingCode"]
        assert result["success"] is True
        assert result["code"] is not None and len(result["code"]) > 0

        db_token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=result["id"]
        )
        assert db_token.calendar_fk_id == bundle_calendar.id

    def test_rejected_without_booking_code_resource(
        self,
        organization,
        calendar,
        system_user_without_booking_code_resource,
    ):
        """Org token WITHOUT CALENDAR_BOOKING_CODE is rejected; no token row created."""
        system_user, token, auth_service = system_user_without_booking_code_resource
        tokens_before = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": organization.id, "calendarId": calendar.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

        # No new token row should have been created
        tokens_after = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()
        assert tokens_after == tokens_before

    def test_expires_at_persisted_on_token(
        self,
        organization,
        calendar,
        system_user_with_booking_code_resource,
    ):
        """expiresAt input is persisted on the CalendarManagementToken row."""
        system_user, token, auth_service = system_user_with_booking_code_resource
        expires_at = datetime.datetime(2030, 12, 31, 23, 59, 59, tzinfo=datetime.UTC)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarId": calendar.id,
                    "expiresAt": expires_at.isoformat(),
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarBookingCode"]
        assert result["success"] is True

        db_token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=result["id"]
        )
        # Django stores datetimes as timezone-aware UTC; compare full timestamp.
        assert db_token.expires_at == expires_at

    def test_cross_org_calendar_returns_invalid_code(
        self,
        organization,
        system_user_with_booking_code_resource,
    ):
        """Calendar from another organization returns success=False with INVALID_CODE.

        This prevents cross-org minting without revealing whether the calendar exists.
        """
        system_user, token, auth_service = system_user_with_booking_code_resource
        other_org = baker.make(Organization, name="Other Org")
        other_calendar = baker.make(Calendar, organization=other_org)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": organization.id, "calendarId": other_calendar.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

        # No token row must have been created for the cross-org calendar
        assert not CalendarManagementToken.original_manager.filter(
            calendar_fk_id=other_calendar.id
        ).exists()

    def test_organization_id_mismatch_returns_invalid_code(
        self,
        organization,
        calendar,
        system_user_with_booking_code_resource,
    ):
        """Authenticated org token but organizationId in input set to a different org's id.

        The mutation must reject the request without leaking cross-org existence.
        No CalendarManagementToken row must be created.
        """
        system_user, token, auth_service = system_user_with_booking_code_resource
        other_org = baker.make(Organization, name="Other Org For Mismatch")
        tokens_before = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {"input": {"organizationId": other_org.id, "calendarId": calendar.id}},
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

        tokens_after = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()
        assert tokens_after == tokens_before


@pytest.mark.django_db
class TestCreateAppointmentTypeBookingCode:
    """Tests for createAppointmentTypeBookingCode mutation."""

    def setup_method(self):
        self.client = APIClient()

    def _post_mutation(self, system_user, token, auth_service, variables):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={
                    "query": CREATE_APPOINTMENT_TYPE_BOOKING_CODE_MUTATION,
                    "variables": variables,
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def test_mints_appointment_type_booking_code_with_resource(
        self,
        organization,
        appointment_type,
        system_user_with_booking_code_resource,
    ):
        """Org token WITH CALENDAR_BOOKING_CODE mints an appointment type booking code.

        Asserts:
        - Response has a non-empty ``code`` and non-null ``id``.
        - A CalendarManagementToken row is scoped to the appointment type.
        - It has a CREATE permission row.
        - minted_by_system_user is set.
        """
        system_user, token, auth_service = system_user_with_booking_code_resource

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "appointmentTypeId": appointment_type.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createAppointmentTypeBookingCode"]
        assert result["success"] is True
        assert result["code"] is not None and len(result["code"]) > 0
        assert result["id"] is not None
        assert result["errorCode"] is None
        assert result["errorMessage"] is None

        # Verify the token row in the database (must scope query by org — multi-tenancy contract)
        db_token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=result["id"]
        )
        assert db_token.organization_id == organization.id
        assert db_token.appointment_type_fk_id == appointment_type.id
        assert db_token.calendar_fk_id is None
        assert db_token.minted_by_system_user_id == system_user.id

        # Verify the CREATE permission row
        permissions = list(
            CalendarManagementTokenPermission.objects.filter_by_organization(organization.id)
            .filter(token_fk_id=db_token.id)
            .values_list("permission", flat=True)
        )
        assert permissions == [EventManagementPermissions.CREATE]

    def test_rejected_without_booking_code_resource(
        self,
        organization,
        appointment_type,
        system_user_without_booking_code_resource,
    ):
        """Org token WITHOUT CALENDAR_BOOKING_CODE is rejected; no token row created."""
        system_user, token, auth_service = system_user_without_booking_code_resource
        tokens_before = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "appointmentTypeId": appointment_type.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

        tokens_after = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()
        assert tokens_after == tokens_before

    def test_expires_at_persisted_on_appointment_type_token(
        self,
        organization,
        appointment_type,
        system_user_with_booking_code_resource,
    ):
        """expiresAt input is persisted on the appointment type CalendarManagementToken row."""
        system_user, token, auth_service = system_user_with_booking_code_resource
        expires_at = datetime.datetime(2031, 6, 15, 12, 0, 0, tzinfo=datetime.UTC)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "appointmentTypeId": appointment_type.id,
                    "expiresAt": expires_at.isoformat(),
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createAppointmentTypeBookingCode"]
        assert result["success"] is True

        db_token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=result["id"]
        )
        # Django stores datetimes as timezone-aware UTC; compare full timestamp.
        assert db_token.expires_at == expires_at

    def test_cross_org_appointment_type_returns_invalid_code(
        self,
        organization,
        system_user_with_booking_code_resource,
    ):
        """Appointment type from another organization returns success=False with INVALID_CODE."""
        system_user, token, auth_service = system_user_with_booking_code_resource
        other_org = baker.make(Organization, name="Other Org")
        other_appointment_type = baker.make(AppointmentType, organization=other_org)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "appointmentTypeId": other_appointment_type.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createAppointmentTypeBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

        # No token row must have been created for the cross-org appointment type
        assert not CalendarManagementToken.original_manager.filter(
            appointment_type_fk_id=other_appointment_type.id
        ).exists()

    def test_organization_id_mismatch_returns_invalid_code(
        self,
        organization,
        appointment_type,
        system_user_with_booking_code_resource,
    ):
        """Authenticated org token but organizationId in input set to a different org's id.

        The mutation must reject the request without leaking cross-org existence.
        No CalendarManagementToken row must be created.
        """
        system_user, token, auth_service = system_user_with_booking_code_resource
        other_org = baker.make(Organization, name="Other Org For AppointmentType Mismatch")
        tokens_before = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": other_org.id,
                    "appointmentTypeId": appointment_type.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createAppointmentTypeBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

        tokens_after = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()
        assert tokens_after == tokens_before
