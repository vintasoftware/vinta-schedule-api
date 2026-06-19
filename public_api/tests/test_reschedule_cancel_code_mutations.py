"""Integration tests for reschedule and cancel booking-code mint mutations.

Covers Phase 2: createCalendarRescheduleBookingCode,
createCalendarGroupRescheduleBookingCode, createCalendarCancellationBookingCode,
createCalendarGroupCancellationBookingCode.
"""

import datetime

from django.utils import timezone

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarGroup,
    CalendarManagementToken,
    CalendarManagementTokenPermission,
    EventManagementPermissions,
)
from organizations.models import Organization
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


CREATE_CALENDAR_RESCHEDULE_CODE_MUTATION = """
mutation CreateCalendarRescheduleBookingCode($input: CreateEventCodeInput!) {
    createCalendarRescheduleBookingCode(input: $input) {
        success
        errorCode
        errorMessage
        code
        id
    }
}
"""

CREATE_CALENDAR_GROUP_RESCHEDULE_CODE_MUTATION = """
mutation CreateCalendarGroupRescheduleBookingCode($input: CreateGroupEventCodeInput!) {
    createCalendarGroupRescheduleBookingCode(input: $input) {
        success
        errorCode
        errorMessage
        code
        id
    }
}
"""

CREATE_CALENDAR_CANCELLATION_CODE_MUTATION = """
mutation CreateCalendarCancellationBookingCode($input: CreateEventCodeInput!) {
    createCalendarCancellationBookingCode(input: $input) {
        success
        errorCode
        errorMessage
        code
        id
    }
}
"""

CREATE_CALENDAR_GROUP_CANCELLATION_CODE_MUTATION = """
mutation CreateCalendarGroupCancellationBookingCode($input: CreateGroupEventCodeInput!) {
    createCalendarGroupCancellationBookingCode(input: $input) {
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
        integration_name="reschedule_cancel_integration", organization=organization
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
    baker.make(
        ResourceAccess,
        system_user=system_user,
        resource_name=PublicAPIResources.CALENDAR,
    )
    return system_user, token, auth_service


@pytest.fixture
def calendar(organization):
    """Create a calendar in the test organization."""
    return baker.make(Calendar, organization=organization, name="Test Calendar")


@pytest.fixture
def calendar_group(organization):
    """Create a calendar group in the test organization."""
    return baker.make(CalendarGroup, organization=organization, name="Test Group")


@pytest.fixture
def event(organization, calendar):
    """Create a CalendarEvent on the test calendar."""
    now = timezone.now()
    return CalendarEvent.objects.create(
        organization=organization,
        calendar_fk=calendar,
        title="Test Event",
        start_time_tz_unaware=now,
        end_time_tz_unaware=now + datetime.timedelta(hours=1),
        timezone="UTC",
    )


@pytest.fixture
def group_event(organization, calendar, calendar_group):
    """Create a CalendarEvent associated with the test calendar group."""
    now = timezone.now()
    return CalendarEvent.objects.create(
        organization=organization,
        calendar_fk=calendar,
        calendar_group_fk=calendar_group,
        title="Group Event",
        start_time_tz_unaware=now,
        end_time_tz_unaware=now + datetime.timedelta(hours=1),
        timezone="UTC",
    )


def _post_graphql(client, system_user, token, auth_service, query, variables):
    """Post a GraphQL mutation with bearer auth."""
    from di_core.containers import container

    with container.public_api_auth_service.override(auth_service):
        return client.post(
            "/graphql/",
            data={"query": query, "variables": variables},
            format="json",
            headers={"authorization": f"Bearer {system_user.id}:{token}"},
        )


# ---------------------------------------------------------------------------
# createCalendarRescheduleBookingCode
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateCalendarRescheduleBookingCode:
    """Tests for createCalendarRescheduleBookingCode mutation."""

    def setup_method(self):
        self.client = APIClient()

    def _post(self, system_user, token, auth_service, variables):
        return _post_graphql(
            self.client,
            system_user,
            token,
            auth_service,
            CREATE_CALENDAR_RESCHEDULE_CODE_MUTATION,
            variables,
        )

    def test_happy_path_mints_reschedule_code(
        self,
        organization,
        calendar,
        event,
        system_user_with_booking_code_resource,
    ):
        """Org token with CALENDAR_BOOKING_CODE mints an event-scoped reschedule code.

        Asserts:
        - Response has non-empty code and non-null id.
        - CalendarManagementToken row is scoped to calendar + event.
        - It has exactly one RESCHEDULE permission row.
        - minted_by_system_user is set.
        """
        system_user, token, auth_service = system_user_with_booking_code_resource

        response = self._post(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarId": calendar.id,
                    "eventId": event.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarRescheduleBookingCode"]
        assert result["success"] is True
        assert result["code"] is not None and len(result["code"]) > 0
        assert result["id"] is not None
        assert result["errorCode"] is None
        assert result["errorMessage"] is None

        # Verify the token row (org-scoped — multi-tenancy contract)
        db_token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=result["id"]
        )
        assert db_token.organization_id == organization.id
        assert db_token.calendar_fk_id == calendar.id
        assert db_token.event_fk_id == event.id
        assert db_token.calendar_group_fk_id is None
        assert db_token.minted_by_system_user_id == system_user.id

        # Must have exactly RESCHEDULE — not CREATE, not CANCEL
        permissions = list(
            CalendarManagementTokenPermission.objects.filter_by_organization(organization.id)
            .filter(token_fk_id=db_token.id)
            .values_list("permission", flat=True)
        )
        assert permissions == [EventManagementPermissions.RESCHEDULE]

    def test_event_belonging_to_different_calendar_is_rejected(
        self,
        organization,
        calendar,
        system_user_with_booking_code_resource,
    ):
        """An event from a different calendar returns INVALID_CODE; no token created."""
        system_user, token, auth_service = system_user_with_booking_code_resource
        # Unique external_id avoids the (external_id, provider, org) unique-together constraint.
        other_calendar = baker.make(
            Calendar,
            organization=organization,
            name="Other Calendar",
            external_id="other-cal-reschedule",
        )
        now = timezone.now()
        other_event = CalendarEvent.objects.create(
            organization=organization,
            calendar_fk=other_calendar,
            title="Other Event",
            start_time_tz_unaware=now,
            end_time_tz_unaware=now + datetime.timedelta(hours=1),
            timezone="UTC",
        )
        tokens_before = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()

        response = self._post(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarId": calendar.id,
                    "eventId": other_event.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["createCalendarRescheduleBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

        tokens_after = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()
        assert tokens_after == tokens_before

    def test_cross_org_calendar_and_event_returns_invalid_code(
        self,
        organization,
        system_user_with_booking_code_resource,
    ):
        """Calendar/event from another org returns INVALID_CODE; no token created."""
        system_user, token, auth_service = system_user_with_booking_code_resource
        other_org = baker.make(Organization, name="Other Org")
        other_calendar = baker.make(Calendar, organization=other_org)
        now = timezone.now()
        other_event = CalendarEvent.objects.create(
            organization=other_org,
            calendar_fk=other_calendar,
            title="Cross-Org Event",
            start_time_tz_unaware=now,
            end_time_tz_unaware=now + datetime.timedelta(hours=1),
            timezone="UTC",
        )

        response = self._post(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarId": other_calendar.id,
                    "eventId": other_event.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["createCalendarRescheduleBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

        assert not CalendarManagementToken.objects.filter(event_fk_id=other_event.id).exists()

    def test_rejected_without_booking_code_resource(
        self,
        organization,
        calendar,
        event,
        system_user_without_booking_code_resource,
    ):
        """Org token WITHOUT CALENDAR_BOOKING_CODE is rejected; no token row created."""
        system_user, token, auth_service = system_user_without_booking_code_resource
        tokens_before = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()

        response = self._post(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarId": calendar.id,
                    "eventId": event.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

        tokens_after = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()
        assert tokens_after == tokens_before

    def test_organization_id_mismatch_returns_invalid_code(
        self,
        organization,
        calendar,
        event,
        system_user_with_booking_code_resource,
    ):
        """input.organizationId != authenticated org id returns INVALID_CODE."""
        system_user, token, auth_service = system_user_with_booking_code_resource
        other_org = baker.make(Organization, name="Mismatch Org")

        response = self._post(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": other_org.id,
                    "calendarId": calendar.id,
                    "eventId": event.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["createCalendarRescheduleBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

    def test_grouped_event_is_rejected(
        self,
        organization,
        calendar,
        calendar_group,
        group_event,
        system_user_with_booking_code_resource,
    ):
        """Calendar reschedule code for a grouped event returns INVALID_CODE; no token created.

        A grouped event has calendar_group_fk set.  The calendar variant must not
        bind to it even though the event's calendar_fk points to a valid calendar.
        """
        system_user, token, auth_service = system_user_with_booking_code_resource
        tokens_before = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()

        response = self._post(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarId": group_event.calendar_fk_id,
                    "eventId": group_event.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["createCalendarRescheduleBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

        tokens_after = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()
        assert tokens_after == tokens_before


# ---------------------------------------------------------------------------
# createCalendarGroupRescheduleBookingCode
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateCalendarGroupRescheduleBookingCode:
    """Tests for createCalendarGroupRescheduleBookingCode mutation."""

    def setup_method(self):
        self.client = APIClient()

    def _post(self, system_user, token, auth_service, variables):
        return _post_graphql(
            self.client,
            system_user,
            token,
            auth_service,
            CREATE_CALENDAR_GROUP_RESCHEDULE_CODE_MUTATION,
            variables,
        )

    def test_happy_path_mints_group_reschedule_code(
        self,
        organization,
        calendar,
        calendar_group,
        group_event,
        system_user_with_booking_code_resource,
    ):
        """Org token mints a group+event-scoped reschedule code with RESCHEDULE permission."""
        system_user, token, auth_service = system_user_with_booking_code_resource

        response = self._post(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarGroupId": calendar_group.id,
                    "eventId": group_event.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarGroupRescheduleBookingCode"]
        assert result["success"] is True
        assert result["code"] is not None and len(result["code"]) > 0
        assert result["id"] is not None

        db_token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=result["id"]
        )
        assert db_token.calendar_group_fk_id == calendar_group.id
        assert db_token.event_fk_id == group_event.id
        assert db_token.calendar_fk_id is None
        assert db_token.minted_by_system_user_id == system_user.id

        permissions = list(
            CalendarManagementTokenPermission.objects.filter_by_organization(organization.id)
            .filter(token_fk_id=db_token.id)
            .values_list("permission", flat=True)
        )
        assert permissions == [EventManagementPermissions.RESCHEDULE]

    def test_event_not_in_group_is_rejected(
        self,
        organization,
        calendar,
        calendar_group,
        event,
        system_user_with_booking_code_resource,
    ):
        """An event not linked to the named group returns INVALID_CODE; no token created."""
        system_user, token, auth_service = system_user_with_booking_code_resource
        # `event` fixture has no calendar_group set
        tokens_before = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()

        response = self._post(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarGroupId": calendar_group.id,
                    "eventId": event.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["createCalendarGroupRescheduleBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

        tokens_after = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()
        assert tokens_after == tokens_before

    def test_cross_org_group_and_event_returns_invalid_code(
        self,
        organization,
        system_user_with_booking_code_resource,
    ):
        """Group/event from another org returns INVALID_CODE."""
        system_user, token, auth_service = system_user_with_booking_code_resource
        other_org = baker.make(Organization, name="Other Org Group")
        other_calendar = baker.make(Calendar, organization=other_org)
        other_group = baker.make(CalendarGroup, organization=other_org)
        now = timezone.now()
        other_event = CalendarEvent.objects.create(
            organization=other_org,
            calendar_fk=other_calendar,
            calendar_group_fk=other_group,
            title="Cross-Org Group Event",
            start_time_tz_unaware=now,
            end_time_tz_unaware=now + datetime.timedelta(hours=1),
            timezone="UTC",
        )

        response = self._post(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarGroupId": other_group.id,
                    "eventId": other_event.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["createCalendarGroupRescheduleBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

    def test_rejected_without_booking_code_resource(
        self,
        organization,
        calendar_group,
        group_event,
        system_user_without_booking_code_resource,
    ):
        """Org token WITHOUT CALENDAR_BOOKING_CODE is rejected."""
        system_user, token, auth_service = system_user_without_booking_code_resource

        response = self._post(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarGroupId": calendar_group.id,
                    "eventId": group_event.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()


# ---------------------------------------------------------------------------
# createCalendarCancellationBookingCode
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateCalendarCancellationBookingCode:
    """Tests for createCalendarCancellationBookingCode mutation."""

    def setup_method(self):
        self.client = APIClient()

    def _post(self, system_user, token, auth_service, variables):
        return _post_graphql(
            self.client,
            system_user,
            token,
            auth_service,
            CREATE_CALENDAR_CANCELLATION_CODE_MUTATION,
            variables,
        )

    def test_happy_path_mints_cancellation_code(
        self,
        organization,
        calendar,
        event,
        system_user_with_booking_code_resource,
    ):
        """Org token with CALENDAR_BOOKING_CODE mints an event-scoped cancellation code.

        Asserts:
        - Response has non-empty code and non-null id.
        - CalendarManagementToken row is scoped to calendar + event.
        - It has exactly one CANCEL permission row — NOT RESCHEDULE.
        - minted_by_system_user is set.
        """
        system_user, token, auth_service = system_user_with_booking_code_resource

        response = self._post(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarId": calendar.id,
                    "eventId": event.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarCancellationBookingCode"]
        assert result["success"] is True
        assert result["code"] is not None and len(result["code"]) > 0
        assert result["id"] is not None
        assert result["errorCode"] is None
        assert result["errorMessage"] is None

        db_token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=result["id"]
        )
        assert db_token.organization_id == organization.id
        assert db_token.calendar_fk_id == calendar.id
        assert db_token.event_fk_id == event.id
        assert db_token.calendar_group_fk_id is None
        assert db_token.minted_by_system_user_id == system_user.id

        # Must have exactly CANCEL — not RESCHEDULE
        permissions = list(
            CalendarManagementTokenPermission.objects.filter_by_organization(organization.id)
            .filter(token_fk_id=db_token.id)
            .values_list("permission", flat=True)
        )
        assert permissions == [EventManagementPermissions.CANCEL]

    def test_event_belonging_to_different_calendar_is_rejected(
        self,
        organization,
        calendar,
        system_user_with_booking_code_resource,
    ):
        """An event from a different calendar returns INVALID_CODE; no token created."""
        system_user, token, auth_service = system_user_with_booking_code_resource
        # Unique external_id avoids the (external_id, provider, org) unique-together constraint.
        other_calendar = baker.make(
            Calendar,
            organization=organization,
            name="Other Calendar",
            external_id="other-cal-cancel",
        )
        now = timezone.now()
        other_event = CalendarEvent.objects.create(
            organization=organization,
            calendar_fk=other_calendar,
            title="Other Event",
            start_time_tz_unaware=now,
            end_time_tz_unaware=now + datetime.timedelta(hours=1),
            timezone="UTC",
        )
        tokens_before = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()

        response = self._post(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarId": calendar.id,
                    "eventId": other_event.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["createCalendarCancellationBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

        tokens_after = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()
        assert tokens_after == tokens_before

    def test_cross_org_returns_invalid_code(
        self,
        organization,
        system_user_with_booking_code_resource,
    ):
        """Calendar/event from another org returns INVALID_CODE."""
        system_user, token, auth_service = system_user_with_booking_code_resource
        other_org = baker.make(Organization, name="Other Org Cancel")
        other_calendar = baker.make(Calendar, organization=other_org)
        now = timezone.now()
        other_event = CalendarEvent.objects.create(
            organization=other_org,
            calendar_fk=other_calendar,
            title="Cross-Org Event",
            start_time_tz_unaware=now,
            end_time_tz_unaware=now + datetime.timedelta(hours=1),
            timezone="UTC",
        )

        response = self._post(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarId": other_calendar.id,
                    "eventId": other_event.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["createCalendarCancellationBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

    def test_rejected_without_booking_code_resource(
        self,
        organization,
        calendar,
        event,
        system_user_without_booking_code_resource,
    ):
        """Org token WITHOUT CALENDAR_BOOKING_CODE is rejected."""
        system_user, token, auth_service = system_user_without_booking_code_resource

        response = self._post(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarId": calendar.id,
                    "eventId": event.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_organization_id_mismatch_returns_invalid_code(
        self,
        organization,
        calendar,
        event,
        system_user_with_booking_code_resource,
    ):
        """input.organizationId != authenticated org id returns INVALID_CODE."""
        system_user, token, auth_service = system_user_with_booking_code_resource
        other_org = baker.make(Organization, name="Mismatch Org Cancel")

        response = self._post(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": other_org.id,
                    "calendarId": calendar.id,
                    "eventId": event.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["createCalendarCancellationBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

    def test_grouped_event_is_rejected(
        self,
        organization,
        calendar,
        calendar_group,
        group_event,
        system_user_with_booking_code_resource,
    ):
        """Calendar cancellation code for a grouped event returns INVALID_CODE; no token created.

        A grouped event has calendar_group_fk set.  The calendar variant must not
        bind to it even though the event's calendar_fk points to a valid calendar.
        """
        system_user, token, auth_service = system_user_with_booking_code_resource
        tokens_before = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()

        response = self._post(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarId": group_event.calendar_fk_id,
                    "eventId": group_event.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["createCalendarCancellationBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

        tokens_after = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()
        assert tokens_after == tokens_before


# ---------------------------------------------------------------------------
# createCalendarGroupCancellationBookingCode
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateCalendarGroupCancellationBookingCode:
    """Tests for createCalendarGroupCancellationBookingCode mutation."""

    def setup_method(self):
        self.client = APIClient()

    def _post(self, system_user, token, auth_service, variables):
        return _post_graphql(
            self.client,
            system_user,
            token,
            auth_service,
            CREATE_CALENDAR_GROUP_CANCELLATION_CODE_MUTATION,
            variables,
        )

    def test_happy_path_mints_group_cancellation_code(
        self,
        organization,
        calendar,
        calendar_group,
        group_event,
        system_user_with_booking_code_resource,
    ):
        """Org token mints a group+event-scoped cancellation code with CANCEL permission."""
        system_user, token, auth_service = system_user_with_booking_code_resource

        response = self._post(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarGroupId": calendar_group.id,
                    "eventId": group_event.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or len(data.get("errors", [])) == 0

        result = data["data"]["createCalendarGroupCancellationBookingCode"]
        assert result["success"] is True
        assert result["code"] is not None and len(result["code"]) > 0
        assert result["id"] is not None

        db_token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=result["id"]
        )
        assert db_token.calendar_group_fk_id == calendar_group.id
        assert db_token.event_fk_id == group_event.id
        assert db_token.calendar_fk_id is None
        assert db_token.minted_by_system_user_id == system_user.id

        # Must have exactly CANCEL — not RESCHEDULE
        permissions = list(
            CalendarManagementTokenPermission.objects.filter_by_organization(organization.id)
            .filter(token_fk_id=db_token.id)
            .values_list("permission", flat=True)
        )
        assert permissions == [EventManagementPermissions.CANCEL]

    def test_event_not_in_group_is_rejected(
        self,
        organization,
        calendar,
        calendar_group,
        event,
        system_user_with_booking_code_resource,
    ):
        """An event not linked to the named group returns INVALID_CODE; no token created."""
        system_user, token, auth_service = system_user_with_booking_code_resource
        tokens_before = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()

        response = self._post(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarGroupId": calendar_group.id,
                    "eventId": event.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["createCalendarGroupCancellationBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

        tokens_after = CalendarManagementToken.objects.filter_by_organization(
            organization.id
        ).count()
        assert tokens_after == tokens_before

    def test_cross_org_group_and_event_returns_invalid_code(
        self,
        organization,
        system_user_with_booking_code_resource,
    ):
        """Group/event from another org returns INVALID_CODE."""
        system_user, token, auth_service = system_user_with_booking_code_resource
        other_org = baker.make(Organization, name="Other Org Group Cancel")
        other_calendar = baker.make(Calendar, organization=other_org)
        other_group = baker.make(CalendarGroup, organization=other_org)
        now = timezone.now()
        other_event = CalendarEvent.objects.create(
            organization=other_org,
            calendar_fk=other_calendar,
            calendar_group_fk=other_group,
            title="Cross-Org Group Event Cancel",
            start_time_tz_unaware=now,
            end_time_tz_unaware=now + datetime.timedelta(hours=1),
            timezone="UTC",
        )

        response = self._post(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarGroupId": other_group.id,
                    "eventId": other_event.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["createCalendarGroupCancellationBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

    def test_rejected_without_booking_code_resource(
        self,
        organization,
        calendar_group,
        group_event,
        system_user_without_booking_code_resource,
    ):
        """Org token WITHOUT CALENDAR_BOOKING_CODE is rejected."""
        system_user, token, auth_service = system_user_without_booking_code_resource

        response = self._post(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarGroupId": calendar_group.id,
                    "eventId": group_event.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_organization_id_mismatch_returns_invalid_code(
        self,
        organization,
        calendar_group,
        group_event,
        system_user_with_booking_code_resource,
    ):
        """input.organizationId != authenticated org id returns INVALID_CODE."""
        system_user, token, auth_service = system_user_with_booking_code_resource
        other_org = baker.make(Organization, name="Mismatch Org Group Cancel")

        response = self._post(
            system_user,
            token,
            auth_service,
            {
                "input": {
                    "organizationId": other_org.id,
                    "calendarGroupId": calendar_group.id,
                    "eventId": group_event.id,
                }
            },
        )

        assert response.status_code == 200
        data = response.json()
        result = data["data"]["createCalendarGroupCancellationBookingCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"


# ---------------------------------------------------------------------------
# Permission-swap safety checks: reschedule != cancel
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPermissionNotSwapped:
    """Assert that reschedule mutations use RESCHEDULE and cancel mutations use CANCEL.

    This prevents accidental permission swaps between the two families.
    """

    def setup_method(self):
        self.client = APIClient()

    def test_reschedule_code_has_reschedule_not_cancel(
        self,
        organization,
        calendar,
        event,
        system_user_with_booking_code_resource,
    ):
        """createCalendarRescheduleBookingCode must create RESCHEDULE, never CANCEL."""
        system_user, token, auth_service = system_user_with_booking_code_resource
        response = _post_graphql(
            self.client,
            system_user,
            token,
            auth_service,
            CREATE_CALENDAR_RESCHEDULE_CODE_MUTATION,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarId": calendar.id,
                    "eventId": event.id,
                }
            },
        )
        result = response.json()["data"]["createCalendarRescheduleBookingCode"]
        assert result["success"] is True

        db_token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=result["id"]
        )
        perms = list(
            CalendarManagementTokenPermission.objects.filter_by_organization(organization.id)
            .filter(token_fk_id=db_token.id)
            .values_list("permission", flat=True)
        )
        assert EventManagementPermissions.RESCHEDULE in perms
        assert EventManagementPermissions.CANCEL not in perms

    def test_cancellation_code_has_cancel_not_reschedule(
        self,
        organization,
        calendar,
        event,
        system_user_with_booking_code_resource,
    ):
        """createCalendarCancellationBookingCode must create CANCEL, never RESCHEDULE."""
        system_user, token, auth_service = system_user_with_booking_code_resource
        response = _post_graphql(
            self.client,
            system_user,
            token,
            auth_service,
            CREATE_CALENDAR_CANCELLATION_CODE_MUTATION,
            {
                "input": {
                    "organizationId": organization.id,
                    "calendarId": calendar.id,
                    "eventId": event.id,
                }
            },
        )
        result = response.json()["data"]["createCalendarCancellationBookingCode"]
        assert result["success"] is True

        db_token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
            id=result["id"]
        )
        perms = list(
            CalendarManagementTokenPermission.objects.filter_by_organization(organization.id)
            .filter(token_fk_id=db_token.id)
            .values_list("permission", flat=True)
        )
        assert EventManagementPermissions.CANCEL in perms
        assert EventManagementPermissions.RESCHEDULE not in perms
