"""Integration tests for single-use booking-code mint mutations.

Covers createCalendarBookingCode and createCalendarGroupBookingCode.
"""

import datetime

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarType
from calendar_integration.models import (
    Calendar,
    CalendarGroup,
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

CREATE_CALENDAR_GROUP_BOOKING_CODE_MUTATION = """
mutation CreateCalendarGroupBookingCode($input: CreateGroupBookingCodeInput!) {
    createCalendarGroupBookingCode(input: $input) {
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
def calendar_group(organization):
    """Create a calendar group in the test organization."""
    return baker.make(CalendarGroup, organization=organization, name="Test Group")


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
        assert db_token.calendar_group_fk_id is None
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
        tokens_before = CalendarManagementToken.objects.filter(
            organization_id=organization.id
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
        tokens_after = CalendarManagementToken.objects.filter(
            organization_id=organization.id
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
        assert not CalendarManagementToken.objects.filter(calendar_fk_id=other_calendar.id).exists()

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
        tokens_before = CalendarManagementToken.objects.filter(
            organization_id=organization.id
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

        tokens_after = CalendarManagementToken.objects.filter(
            organization_id=organization.id
        ).count()
        assert tokens_after == tokens_before


@pytest.mark.django_db
class TestCreateCalendarGroupBookingCode:
    """Tests for createCalendarGroupBookingCode mutation."""

    def setup_method(self):
        self.client = APIClient()

    def _post_mutation(self, system_user, token, auth_service, variables):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={
                    "query": CREATE_CALENDAR_GROUP_BOOKING_CODE_MUTATION,
                    "variables": variables,
                },
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def test_mints_group_booking_code_with_resource(
        self,
        organization,
        calendar_group,
        system_user_with_booking_code_resource,
    ):
        """Org token WITH CALENDAR_BOOKING_CODE mints a group booking code.

        Asserts:
        - Response has a non-empty ``code`` and non-null ``id``.
        - A CalendarManagementToken row is scoped to the calendar_group.
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
                    "calendarGroupId": calendar_group.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarGroupBookingCode"]
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
        assert db_token.calendar_group_fk_id == calendar_group.id
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
        calendar_group,
        system_user_without_booking_code_resource,
    ):
        """Org token WITHOUT CALENDAR_BOOKING_CODE is rejected; no token row created."""
        system_user, token, auth_service = system_user_without_booking_code_resource
        tokens_before = CalendarManagementToken.objects.filter(
            organization_id=organization.id
        ).count()

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarGroupId": calendar_group.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data
        assert len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

        tokens_after = CalendarManagementToken.objects.filter(
            organization_id=organization.id
        ).count()
        assert tokens_after == tokens_before

    def test_expires_at_persisted_on_group_token(
        self,
        organization,
        calendar_group,
        system_user_with_booking_code_resource,
    ):
        """expiresAt input is persisted on the group CalendarManagementToken row."""
        system_user, token, auth_service = system_user_with_booking_code_resource
        expires_at = datetime.datetime(2031, 6, 15, 12, 0, 0, tzinfo=datetime.UTC)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarGroupId": calendar_group.id,
                    "expiresAt": expires_at.isoformat(),
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarGroupBookingCode"]
        assert result["success"] is True

        db_token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=result["id"]
        )
        # Django stores datetimes as timezone-aware UTC; compare full timestamp.
        assert db_token.expires_at == expires_at

    def test_cross_org_calendar_group_returns_invalid_code(
        self,
        organization,
        system_user_with_booking_code_resource,
    ):
        """Calendar group from another organization returns success=False with INVALID_CODE."""
        system_user, token, auth_service = system_user_with_booking_code_resource
        other_org = baker.make(Organization, name="Other Org")
        other_group = baker.make(CalendarGroup, organization=other_org)

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarGroupId": other_group.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarGroupBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

        # No token row must have been created for the cross-org group
        assert not CalendarManagementToken.objects.filter(
            calendar_group_fk_id=other_group.id
        ).exists()

    def test_organization_id_mismatch_returns_invalid_code(
        self,
        organization,
        calendar_group,
        system_user_with_booking_code_resource,
    ):
        """Authenticated org token but organizationId in input set to a different org's id.

        The mutation must reject the request without leaking cross-org existence.
        No CalendarManagementToken row must be created.
        """
        system_user, token, auth_service = system_user_with_booking_code_resource
        other_org = baker.make(Organization, name="Other Org For Group Mismatch")
        tokens_before = CalendarManagementToken.objects.filter(
            organization_id=organization.id
        ).count()

        response = self._post_mutation(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": other_org.id,
                    "calendarGroupId": calendar_group.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarGroupBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

        tokens_after = CalendarManagementToken.objects.filter(
            organization_id=organization.id
        ).count()
        assert tokens_after == tokens_before
