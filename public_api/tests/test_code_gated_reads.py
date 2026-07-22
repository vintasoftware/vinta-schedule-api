"""Integration tests for code-gated (unauthenticated) availability read fields.

Covers these fields:
- availableTimesWithCode
- availabilityWindowsWithCode
- unavailableWindowsWithCode
- calendarGroupBookableSlotsWithCode
- calendarGroupAvailabilityWithCode
- calendarBookableSlotsWithCode

All fields are unauthenticated (no Authorization header required).  The booking
code authorizes access to its bound calendar / calendar group.  Reads never
consume the code (used_at remains NULL after any read).
"""

import datetime
from unittest.mock import Mock, patch

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AvailableTime,
    Calendar,
    CalendarEvent,
    CalendarGroup,
    CalendarManagementToken,
    ChildrenCalendarRelationship,
    EventManagementPermissions,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.dataclasses import (
    AvailableTimeWindow,
    BlockedTimeData,
    BookableSlotProposal,
    CalendarGroupRangeAvailability,
    CalendarGroupSlotAvailability,
    UnavailableTimeWindow,
)
from organizations.models import Organization


# ---------------------------------------------------------------------------
# GraphQL query strings
# ---------------------------------------------------------------------------

AVAILABLE_TIMES_WITH_CODE = """
query AvailableTimesWithCode($code: String!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
    availableTimesWithCode(code: $code, startDatetime: $startDatetime, endDatetime: $endDatetime) {
        id
        startTime
        endTime
    }
}
"""

AVAILABILITY_WINDOWS_WITH_CODE = """
query AvailabilityWindowsWithCode($code: String!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
    availabilityWindowsWithCode(code: $code, startDatetime: $startDatetime, endDatetime: $endDatetime) {
        id
        startTime
        endTime
        canBookPartially
    }
}
"""

UNAVAILABLE_WINDOWS_WITH_CODE = """
query UnavailableWindowsWithCode($code: String!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
    unavailableWindowsWithCode(code: $code, startDatetime: $startDatetime, endDatetime: $endDatetime) {
        id
        startTime
        endTime
        reason
    }
}
"""

CALENDAR_GROUP_BOOKABLE_SLOTS_WITH_CODE = """
query CalendarGroupBookableSlotsWithCode(
    $code: String!,
    $searchWindowStart: DateTime!,
    $searchWindowEnd: DateTime!,
    $durationSeconds: Int!
) {
    calendarGroupBookableSlotsWithCode(
        code: $code,
        searchWindowStart: $searchWindowStart,
        searchWindowEnd: $searchWindowEnd,
        durationSeconds: $durationSeconds
    ) {
        startTime
        endTime
    }
}
"""

CALENDAR_GROUP_AVAILABILITY_WITH_CODE = """
query CalendarGroupAvailabilityWithCode($code: String!, $ranges: [DateTimeRangeInput!]!) {
    calendarGroupAvailabilityWithCode(code: $code, ranges: $ranges) {
        startTime
        endTime
        slots {
            slotId
            availableCalendarIds
            requiredCount
        }
    }
}
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization():
    return baker.make(Organization, name="Test Org for Code-Gated Reads")


@pytest.fixture
def other_organization():
    return baker.make(Organization, name="Other Org")


@pytest.fixture
def calendar(organization):
    return baker.make(Calendar, organization=organization, name="Test Calendar")


@pytest.fixture
def other_calendar(other_organization):
    return baker.make(Calendar, organization=other_organization, name="Other Calendar")


@pytest.fixture
def calendar_group(organization):
    return baker.make(CalendarGroup, organization=organization, name="Test Group")


@pytest.fixture
def calendar_event(organization, calendar):
    return baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        title="Test Event",
        timezone="UTC",
    )


@pytest.fixture
def group_event(organization, calendar, calendar_group):
    return baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        calendar_group=calendar_group,
        title="Group Test Event",
        timezone="UTC",
    )


@pytest.fixture
def permission_service():
    return CalendarPermissionService()


@pytest.fixture
def calendar_booking_code(permission_service, organization, calendar):
    """Create a calendar-scoped booking code. Returns (token, plaintext_code)."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )
    return token, code


@pytest.fixture
def group_booking_code(permission_service, organization, calendar_group):
    """Create a group-scoped booking code. Returns (token, plaintext_code)."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=calendar_group.id,
    )
    return token, code


@pytest.fixture
def event_booking_code(permission_service, organization, calendar, calendar_event):
    """Create an event-scoped booking code (reschedule). Returns (token, plaintext_code)."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_id=calendar.id,
        event_id=calendar_event.id,
    )
    return token, code


@pytest.fixture
def anon_client():
    """An APIClient with no Authorization header."""
    return APIClient()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def post_graphql(client: APIClient, query: str, variables: dict) -> dict:
    """Post a GraphQL request and return the parsed JSON body."""
    response = client.post(
        "/graphql/",
        data={"query": query, "variables": variables},
        format="json",
    )
    assert response.status_code == 200, response.content.decode()
    return response.json()


# ---------------------------------------------------------------------------
# Calendar-code reads
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAvailableTimesWithCode:
    """Tests for availableTimesWithCode — calendar scope."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_returns_available_times_no_auth_header(
        self, mock_rate_limiter, anon_client, calendar_booking_code, organization, calendar
    ):
        """A calendar booking code returns available times with no Authorization header."""
        from calendar_integration.models import AvailableTime
        from di_core.containers import container

        mock_rate_limiter.return_value = iter([None])
        _token, code = calendar_booking_code

        # Seed a real AvailableTime row so the service can return something.
        at = baker.make(
            AvailableTime,
            organization=organization,
            calendar=calendar,
            start_time_tz_unaware=datetime.datetime(2025, 9, 2, 9, 0),
            end_time_tz_unaware=datetime.datetime(2025, 9, 2, 10, 0),
            timezone="UTC",
        )

        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None
        mock_calendar_service.get_available_times_expanded.return_value = [at]

        with container.calendar_service.override(mock_calendar_service):
            data = post_graphql(
                anon_client,
                AVAILABLE_TIMES_WITH_CODE,
                {
                    "code": code,
                    "startDatetime": "2025-09-02T00:00:00Z",
                    "endDatetime": "2025-09-02T23:59:59Z",
                },
            )

        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["availableTimesWithCode"]
        assert len(result) == 1
        assert result[0]["id"] == str(at.id)

        # The service was initialized without a user_or_token (anon).
        mock_calendar_service.initialize_without_provider.assert_called_once()
        mock_calendar_service.get_available_times_expanded.assert_called_once()

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_code_never_consumed_after_read(
        self, mock_rate_limiter, anon_client, calendar_booking_code
    ):
        """Reads must NOT set used_at on the token (codes are reusable for reads)."""
        from di_core.containers import container

        mock_rate_limiter.return_value = iter([None])
        token, code = calendar_booking_code

        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None
        mock_calendar_service.get_available_times_expanded.return_value = []

        with container.calendar_service.override(mock_calendar_service):
            post_graphql(
                anon_client,
                AVAILABLE_TIMES_WITH_CODE,
                {
                    "code": code,
                    "startDatetime": "2025-09-02T00:00:00Z",
                    "endDatetime": "2025-09-02T23:59:59Z",
                },
            )

        token.refresh_from_db()
        assert token.used_at is None, "used_at should remain NULL after a read"

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_invalid_code_returns_graphql_error(self, mock_rate_limiter, anon_client):
        """An invalid / unknown code returns a GraphQL error with the uniform message."""
        mock_rate_limiter.return_value = iter([None])

        data = post_graphql(
            anon_client,
            AVAILABLE_TIMES_WITH_CODE,
            {
                "code": "aW52YWxpZA==",  # "invalid" base64 — no matching token
                "startDatetime": "2025-09-02T00:00:00Z",
                "endDatetime": "2025-09-02T23:59:59Z",
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Invalid or expired code."

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_revoked_code_returns_error(
        self, mock_rate_limiter, anon_client, permission_service, organization, calendar
    ):
        """A revoked code returns the uniform error message."""
        mock_rate_limiter.return_value = iter([None])
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=calendar.id,
        )
        permission_service.revoke_token(organization_id=organization.id, token_id=token.id)

        data = post_graphql(
            anon_client,
            AVAILABLE_TIMES_WITH_CODE,
            {
                "code": code,
                "startDatetime": "2025-09-02T00:00:00Z",
                "endDatetime": "2025-09-02T23:59:59Z",
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Invalid or expired code."

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_expired_code_returns_error(
        self, mock_rate_limiter, anon_client, permission_service, organization, calendar
    ):
        """An expired code returns the uniform error message."""
        mock_rate_limiter.return_value = iter([None])
        past = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
        _token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=calendar.id,
            expires_at=past,
        )

        data = post_graphql(
            anon_client,
            AVAILABLE_TIMES_WITH_CODE,
            {
                "code": code,
                "startDatetime": "2025-09-02T00:00:00Z",
                "endDatetime": "2025-09-02T23:59:59Z",
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Invalid or expired code."

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_used_code_returns_error(
        self, mock_rate_limiter, anon_client, permission_service, organization, calendar
    ):
        """A used (consumed) code returns the uniform error message."""
        mock_rate_limiter.return_value = iter([None])
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=calendar.id,
        )
        CalendarManagementToken.objects.filter(id=token.id).update(
            used_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
        )

        data = post_graphql(
            anon_client,
            AVAILABLE_TIMES_WITH_CODE,
            {
                "code": code,
                "startDatetime": "2025-09-02T00:00:00Z",
                "endDatetime": "2025-09-02T23:59:59Z",
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Invalid or expired code."

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_group_code_on_calendar_field_returns_error(
        self, mock_rate_limiter, anon_client, group_booking_code
    ):
        """A group-bound code passed to a calendar read field returns the uniform error."""
        mock_rate_limiter.return_value = iter([None])
        _token, code = group_booking_code

        data = post_graphql(
            anon_client,
            AVAILABLE_TIMES_WITH_CODE,
            {
                "code": code,
                "startDatetime": "2025-09-02T00:00:00Z",
                "endDatetime": "2025-09-02T23:59:59Z",
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Invalid or expired code."


@pytest.mark.django_db
class TestAvailabilityWindowsWithCode:
    """Tests for availabilityWindowsWithCode — calendar scope."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_returns_windows_no_auth_header(
        self, mock_rate_limiter, anon_client, calendar_booking_code
    ):
        """A calendar booking code returns availability windows with no Authorization header."""
        from di_core.containers import container

        mock_rate_limiter.return_value = iter([None])
        _token, code = calendar_booking_code

        window = AvailableTimeWindow(
            start_time=datetime.datetime(2025, 9, 2, 9, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
            id=1,
            can_book_partially=True,
        )
        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None
        mock_calendar_service.get_availability_windows_in_range.return_value = [window]

        with container.calendar_service.override(mock_calendar_service):
            data = post_graphql(
                anon_client,
                AVAILABILITY_WINDOWS_WITH_CODE,
                {
                    "code": code,
                    "startDatetime": "2025-09-02T00:00:00Z",
                    "endDatetime": "2025-09-02T23:59:59Z",
                },
            )

        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["availabilityWindowsWithCode"]
        assert len(result) == 1
        assert result[0]["id"] == 1
        assert result[0]["canBookPartially"] is True

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_code_not_consumed_after_read(
        self, mock_rate_limiter, anon_client, calendar_booking_code
    ):
        """availabilityWindowsWithCode must not consume the code."""
        from di_core.containers import container

        mock_rate_limiter.return_value = iter([None])
        token, code = calendar_booking_code

        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None
        mock_calendar_service.get_availability_windows_in_range.return_value = []

        with container.calendar_service.override(mock_calendar_service):
            post_graphql(
                anon_client,
                AVAILABILITY_WINDOWS_WITH_CODE,
                {
                    "code": code,
                    "startDatetime": "2025-09-02T00:00:00Z",
                    "endDatetime": "2025-09-02T23:59:59Z",
                },
            )

        token.refresh_from_db()
        assert token.used_at is None

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_invalid_code_returns_error(self, mock_rate_limiter, anon_client):
        """An invalid code returns the uniform error message."""
        mock_rate_limiter.return_value = iter([None])

        data = post_graphql(
            anon_client,
            AVAILABILITY_WINDOWS_WITH_CODE,
            {
                "code": "bm90YXZhbGlkdG9rZW4=",
                "startDatetime": "2025-09-02T00:00:00Z",
                "endDatetime": "2025-09-02T23:59:59Z",
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Invalid or expired code."


@pytest.mark.django_db
class TestUnavailableWindowsWithCode:
    """Tests for unavailableWindowsWithCode — calendar scope."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_returns_unavailable_windows_no_auth_header(
        self, mock_rate_limiter, anon_client, calendar_booking_code
    ):
        """A calendar booking code returns unavailable windows with no Authorization header."""
        from di_core.containers import container

        mock_rate_limiter.return_value = iter([None])
        _token, code = calendar_booking_code

        _bt_data = BlockedTimeData(
            id=42,
            calendar_external_id="ext-cal-id",
            start_time=datetime.datetime(2025, 9, 2, 11, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2025, 9, 2, 12, 0, tzinfo=datetime.UTC),
            timezone="UTC",
            reason="maintenance",
            external_id=None,
            meta={},
        )
        unavail = UnavailableTimeWindow(
            start_time=datetime.datetime(2025, 9, 2, 11, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2025, 9, 2, 12, 0, tzinfo=datetime.UTC),
            id=42,
            reason="blocked_time",
            data=_bt_data,
        )
        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None
        mock_calendar_service.get_unavailable_time_windows_in_range.return_value = [unavail]

        with container.calendar_service.override(mock_calendar_service):
            data = post_graphql(
                anon_client,
                UNAVAILABLE_WINDOWS_WITH_CODE,
                {
                    "code": code,
                    "startDatetime": "2025-09-02T00:00:00Z",
                    "endDatetime": "2025-09-02T23:59:59Z",
                },
            )

        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["unavailableWindowsWithCode"]
        assert len(result) == 1
        assert result[0]["id"] == 42
        assert result[0]["reason"] == "blocked_time"

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_code_not_consumed_after_read(
        self, mock_rate_limiter, anon_client, calendar_booking_code
    ):
        """unavailableWindowsWithCode must not consume the code."""
        from di_core.containers import container

        mock_rate_limiter.return_value = iter([None])
        token, code = calendar_booking_code

        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None
        mock_calendar_service.get_unavailable_time_windows_in_range.return_value = []

        with container.calendar_service.override(mock_calendar_service):
            post_graphql(
                anon_client,
                UNAVAILABLE_WINDOWS_WITH_CODE,
                {
                    "code": code,
                    "startDatetime": "2025-09-02T00:00:00Z",
                    "endDatetime": "2025-09-02T23:59:59Z",
                },
            )

        token.refresh_from_db()
        assert token.used_at is None

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_invalid_code_returns_error(self, mock_rate_limiter, anon_client):
        """An invalid code returns the uniform error message."""
        mock_rate_limiter.return_value = iter([None])

        data = post_graphql(
            anon_client,
            UNAVAILABLE_WINDOWS_WITH_CODE,
            {
                "code": "bm90cmVhbA==",
                "startDatetime": "2025-09-02T00:00:00Z",
                "endDatetime": "2025-09-02T23:59:59Z",
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Invalid or expired code."

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_group_code_on_calendar_read_field_returns_error(
        self, mock_rate_limiter, anon_client, group_booking_code
    ):
        """A group-bound code passed to a calendar read field returns the uniform error (wrong scope)."""
        mock_rate_limiter.return_value = iter([None])
        _token, code = group_booking_code

        data = post_graphql(
            anon_client,
            UNAVAILABLE_WINDOWS_WITH_CODE,
            {
                "code": code,
                "startDatetime": "2025-09-02T00:00:00Z",
                "endDatetime": "2025-09-02T23:59:59Z",
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Invalid or expired code."


# ---------------------------------------------------------------------------
# Event-scoped code reads — scope resolves via event.calendar / event.calendar_group
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestEventBoundCodeCalendarReads:
    """An event-bound (reschedule) code resolves available times via event.calendar."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_event_code_resolves_calendar(
        self,
        mock_rate_limiter,
        anon_client,
        event_booking_code,
        organization,
        calendar,
        calendar_event,
    ):
        """An event-bound reschedule code can be used for calendar reads via event.calendar."""
        from di_core.containers import container

        mock_rate_limiter.return_value = iter([None])
        _token, code = event_booking_code

        window = AvailableTimeWindow(
            start_time=datetime.datetime(2025, 9, 2, 9, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
            id=None,
            can_book_partially=False,
        )
        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None
        mock_calendar_service.get_availability_windows_in_range.return_value = [window]

        with container.calendar_service.override(mock_calendar_service):
            data = post_graphql(
                anon_client,
                AVAILABILITY_WINDOWS_WITH_CODE,
                {
                    "code": code,
                    "startDatetime": "2025-09-02T00:00:00Z",
                    "endDatetime": "2025-09-02T23:59:59Z",
                },
            )

        # The query should succeed — event.calendar is the bound scope.
        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["availabilityWindowsWithCode"]
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Group-code reads
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCalendarGroupBookableSlotsWithCode:
    """Tests for calendarGroupBookableSlotsWithCode — group scope."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_returns_bookable_slots_no_auth_header(
        self, mock_rate_limiter, anon_client, group_booking_code
    ):
        """A group booking code returns bookable slots with no Authorization header."""
        from di_core.containers import container

        mock_rate_limiter.return_value = iter([None])
        _token, code = group_booking_code

        proposal = BookableSlotProposal(
            start_time=datetime.datetime(2025, 9, 2, 9, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
        )
        mock_group_service = Mock()
        mock_group_service.initialize.return_value = None
        mock_group_service.find_bookable_slots.return_value = [proposal]

        with container.calendar_group_service.override(mock_group_service):
            data = post_graphql(
                anon_client,
                CALENDAR_GROUP_BOOKABLE_SLOTS_WITH_CODE,
                {
                    "code": code,
                    "searchWindowStart": "2025-09-02T00:00:00Z",
                    "searchWindowEnd": "2025-09-02T23:59:59Z",
                    "durationSeconds": 3600,
                },
            )

        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["calendarGroupBookableSlotsWithCode"]
        assert len(result) == 1
        assert result[0]["startTime"] == "2025-09-02T09:00:00+00:00"
        assert result[0]["endTime"] == "2025-09-02T10:00:00+00:00"

        mock_group_service.find_bookable_slots.assert_called_once()

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_code_not_consumed_after_read(self, mock_rate_limiter, anon_client, group_booking_code):
        """calendarGroupBookableSlotsWithCode must not consume the code."""
        from di_core.containers import container

        mock_rate_limiter.return_value = iter([None])
        token, code = group_booking_code

        mock_group_service = Mock()
        mock_group_service.initialize.return_value = None
        mock_group_service.find_bookable_slots.return_value = []

        with container.calendar_group_service.override(mock_group_service):
            post_graphql(
                anon_client,
                CALENDAR_GROUP_BOOKABLE_SLOTS_WITH_CODE,
                {
                    "code": code,
                    "searchWindowStart": "2025-09-02T00:00:00Z",
                    "searchWindowEnd": "2025-09-02T23:59:59Z",
                    "durationSeconds": 3600,
                },
            )

        token.refresh_from_db()
        assert token.used_at is None

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_calendar_code_on_group_field_returns_error(
        self, mock_rate_limiter, anon_client, calendar_booking_code
    ):
        """A calendar-bound code passed to a group read field returns the uniform error (wrong scope)."""
        mock_rate_limiter.return_value = iter([None])
        _token, code = calendar_booking_code

        data = post_graphql(
            anon_client,
            CALENDAR_GROUP_BOOKABLE_SLOTS_WITH_CODE,
            {
                "code": code,
                "searchWindowStart": "2025-09-02T00:00:00Z",
                "searchWindowEnd": "2025-09-02T23:59:59Z",
                "durationSeconds": 3600,
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Invalid or expired code."

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_invalid_code_returns_error(self, mock_rate_limiter, anon_client):
        """An invalid code returns the uniform error message."""
        mock_rate_limiter.return_value = iter([None])

        data = post_graphql(
            anon_client,
            CALENDAR_GROUP_BOOKABLE_SLOTS_WITH_CODE,
            {
                "code": "bm90dmFsaWQ=",
                "searchWindowStart": "2025-09-02T00:00:00Z",
                "searchWindowEnd": "2025-09-02T23:59:59Z",
                "durationSeconds": 3600,
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Invalid or expired code."


@pytest.mark.django_db
class TestCalendarGroupAvailabilityWithCode:
    """Tests for calendarGroupAvailabilityWithCode — group scope."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_returns_group_availability_no_auth_header(
        self, mock_rate_limiter, anon_client, group_booking_code, calendar_group
    ):
        """A group booking code returns group availability with no Authorization header."""
        from di_core.containers import container

        mock_rate_limiter.return_value = iter([None])
        _token, code = group_booking_code

        slot_avail = CalendarGroupSlotAvailability(
            slot_id=1, available_calendar_ids=[10, 20], required_count=1
        )
        range_avail = CalendarGroupRangeAvailability(
            start_time=datetime.datetime(2025, 9, 2, 9, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
            slots=[slot_avail],
        )

        mock_group_service = Mock()
        mock_group_service.initialize.return_value = None
        mock_group_service.check_group_availability.return_value = [range_avail]

        with container.calendar_group_service.override(mock_group_service):
            data = post_graphql(
                anon_client,
                CALENDAR_GROUP_AVAILABILITY_WITH_CODE,
                {
                    "code": code,
                    "ranges": [
                        {
                            "startTime": "2025-09-02T09:00:00Z",
                            "endTime": "2025-09-02T10:00:00Z",
                        }
                    ],
                },
            )

        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["calendarGroupAvailabilityWithCode"]
        assert len(result) == 1
        assert result[0]["slots"][0]["slotId"] == 1
        assert result[0]["slots"][0]["requiredCount"] == 1

        mock_group_service.check_group_availability.assert_called_once()

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_code_not_consumed_after_read(self, mock_rate_limiter, anon_client, group_booking_code):
        """calendarGroupAvailabilityWithCode must not consume the code."""
        from di_core.containers import container

        mock_rate_limiter.return_value = iter([None])
        token, code = group_booking_code

        mock_group_service = Mock()
        mock_group_service.initialize.return_value = None
        mock_group_service.check_group_availability.return_value = []

        with container.calendar_group_service.override(mock_group_service):
            post_graphql(
                anon_client,
                CALENDAR_GROUP_AVAILABILITY_WITH_CODE,
                {
                    "code": code,
                    "ranges": [
                        {
                            "startTime": "2025-09-02T09:00:00Z",
                            "endTime": "2025-09-02T10:00:00Z",
                        }
                    ],
                },
            )

        token.refresh_from_db()
        assert token.used_at is None

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_calendar_code_on_group_availability_returns_error(
        self, mock_rate_limiter, anon_client, calendar_booking_code
    ):
        """A calendar-bound code on calendarGroupAvailabilityWithCode returns the uniform error."""
        mock_rate_limiter.return_value = iter([None])
        _token, code = calendar_booking_code

        data = post_graphql(
            anon_client,
            CALENDAR_GROUP_AVAILABILITY_WITH_CODE,
            {
                "code": code,
                "ranges": [
                    {
                        "startTime": "2025-09-02T09:00:00Z",
                        "endTime": "2025-09-02T10:00:00Z",
                    }
                ],
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Invalid or expired code."

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_invalid_code_returns_error(self, mock_rate_limiter, anon_client):
        """An invalid code returns the uniform error message."""
        mock_rate_limiter.return_value = iter([None])

        data = post_graphql(
            anon_client,
            CALENDAR_GROUP_AVAILABILITY_WITH_CODE,
            {
                "code": "bm90YXZhbGlkY29kZQ==",
                "ranges": [
                    {
                        "startTime": "2025-09-02T09:00:00Z",
                        "endTime": "2025-09-02T10:00:00Z",
                    }
                ],
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Invalid or expired code."


# ---------------------------------------------------------------------------
# Cross-org isolation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCrossOrgIsolation:
    """A code can only read data for its own org's calendar / group."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_calendar_code_reads_own_org_only(
        self,
        mock_rate_limiter,
        anon_client,
        organization,
        other_organization,  # noqa: ARG002
        calendar,
        other_calendar,  # noqa: ARG002
        permission_service,
    ):
        """A code bound to calendar A (org1) resolves to org1's calendar only.

        The code carries its own org; the service is initialized with the code's
        org (org1), not the other org's.
        """
        from di_core.containers import container

        mock_rate_limiter.return_value = iter([None])
        # Mint a code for org1's calendar
        _token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=calendar.id,
        )

        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None
        mock_calendar_service.get_availability_windows_in_range.return_value = []

        with container.calendar_service.override(mock_calendar_service):
            data = post_graphql(
                anon_client,
                AVAILABILITY_WINDOWS_WITH_CODE,
                {
                    "code": code,
                    "startDatetime": "2025-09-02T00:00:00Z",
                    "endDatetime": "2025-09-02T23:59:59Z",
                },
            )

        # The query succeeds and the service is initialized with the code's org.
        assert "errors" not in data or len(data.get("errors", [])) == 0
        call_kwargs = mock_calendar_service.initialize_without_provider.call_args[1]
        assert call_kwargs["organization"].id == organization.id


# ---------------------------------------------------------------------------
# Tampered-secret code (valid id, wrong secret)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTamperedSecretCode:
    """The hash-verify gate must reject a code whose secret half is swapped."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_tampered_secret_calendar_field_returns_error(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
        organization,
        calendar,
    ):
        """A code with a valid token_id but a swapped secret is rejected with the uniform error.

        Construction:
        1. Mint a real code via create_booking_token (base64 of "<id>:<raw_secret>").
        2. Decode the base64, split on ':', replace the raw_secret with a different value.
        3. Re-encode base64 → tampered code.
        The id is real (token exists in DB) but the secret does not match the stored hash.
        """
        import base64

        mock_rate_limiter.return_value = iter([None])
        token, real_code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=calendar.id,
        )

        # Construct a tampered code: same id, different secret.
        decoded = base64.b64decode(real_code).decode("utf-8")
        token_id_part, _real_secret = decoded.split(":", 1)
        tampered_code = base64.b64encode(f"{token_id_part}:WRONG_SECRET_VALUE".encode()).decode()

        data = post_graphql(
            anon_client,
            AVAILABLE_TIMES_WITH_CODE,
            {
                "code": tampered_code,
                "startDatetime": "2025-09-02T00:00:00Z",
                "endDatetime": "2025-09-02T23:59:59Z",
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Invalid or expired code."
        # The token must not have been consumed.
        token.refresh_from_db()
        assert token.used_at is None

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_tampered_secret_group_field_returns_error(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
        organization,
        calendar_group,
    ):
        """Same tampered-secret test for the group availability field."""
        import base64

        mock_rate_limiter.return_value = iter([None])
        token, real_code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_group_id=calendar_group.id,
        )

        decoded = base64.b64decode(real_code).decode("utf-8")
        token_id_part, _real_secret = decoded.split(":", 1)
        tampered_code = base64.b64encode(f"{token_id_part}:WRONG_SECRET_VALUE".encode()).decode()

        data = post_graphql(
            anon_client,
            CALENDAR_GROUP_AVAILABILITY_WITH_CODE,
            {
                "code": tampered_code,
                "ranges": [
                    {
                        "startTime": "2025-09-02T09:00:00Z",
                        "endTime": "2025-09-02T10:00:00Z",
                    }
                ],
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Invalid or expired code."
        token.refresh_from_db()
        assert token.used_at is None


# ---------------------------------------------------------------------------
# Real (non-mocked) cross-org isolation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRealCrossOrgIsolation:
    """Real DB isolation: org A's code must not return org B's AvailableTime rows."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_available_times_with_code_scoped_to_own_org(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
    ):
        """With real AvailableTime rows in both orgs, the code only returns its own org's data.

        Setup:
        - Org A: calendar_a + 1 AvailableTime in the window.
        - Org B: calendar_b + 1 AvailableTime in the same window (different org).
        - Mint a code on org A's calendar_a.
        - Call availableTimesWithCode.
        - Assert only org A's AvailableTime id is in the response (not org B's).
        """
        from calendar_integration.models import AvailableTime

        mock_rate_limiter.return_value = iter([None])

        org_a = baker.make(Organization, name="Real Org A")
        org_b = baker.make(Organization, name="Real Org B")

        calendar_a = baker.make(Calendar, organization=org_a, name="Cal A")
        calendar_b = baker.make(Calendar, organization=org_b, name="Cal B")

        # AvailableTime for org A (non-recurring, within query window)
        at_a = baker.make(
            AvailableTime,
            organization=org_a,
            calendar=calendar_a,
            start_time_tz_unaware=datetime.datetime(2025, 10, 1, 9, 0),
            end_time_tz_unaware=datetime.datetime(2025, 10, 1, 10, 0),
            timezone="UTC",
            recurrence_rule=None,
        )
        # AvailableTime for org B in the same window — must NOT appear in org A's results.
        at_b = baker.make(
            AvailableTime,
            organization=org_b,
            calendar=calendar_b,
            start_time_tz_unaware=datetime.datetime(2025, 10, 1, 9, 0),
            end_time_tz_unaware=datetime.datetime(2025, 10, 1, 10, 0),
            timezone="UTC",
            recurrence_rule=None,
        )

        _token, code = permission_service.create_booking_token(
            organization_id=org_a.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=calendar_a.id,
        )

        data = post_graphql(
            anon_client,
            AVAILABLE_TIMES_WITH_CODE,
            {
                "code": code,
                "startDatetime": "2025-10-01T00:00:00Z",
                "endDatetime": "2025-10-01T23:59:59Z",
            },
        )

        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["availableTimesWithCode"]
        returned_ids = [int(r["id"]) for r in result]
        assert at_a.id in returned_ids, "Org A's AvailableTime must be returned"
        assert at_b.id not in returned_ids, "Org B's AvailableTime must NOT be returned"


# ---------------------------------------------------------------------------
# Range clamp check
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCodeGatedRangeClamp:
    """Over-maximum and backwards datetime ranges are rejected before hitting the service."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_over_max_range_calendar_field_rejected(
        self,
        mock_rate_limiter,
        anon_client,
        calendar_booking_code,
    ):
        """A range exceeding MAX_CODE_GATED_RANGE (366 days) is rejected on a calendar field."""
        mock_rate_limiter.return_value = iter([None])
        _token, code = calendar_booking_code

        data = post_graphql(
            anon_client,
            AVAILABLE_TIMES_WITH_CODE,
            {
                "code": code,
                "startDatetime": "2025-01-01T00:00:00Z",
                # 367 days — one day over the 366-day cap
                "endDatetime": "2026-01-03T00:00:00Z",
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Requested time range is too large."

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_backwards_range_calendar_field_rejected(
        self,
        mock_rate_limiter,
        anon_client,
        calendar_booking_code,
    ):
        """A backwards range (end <= start) is rejected on a calendar field."""
        mock_rate_limiter.return_value = iter([None])
        _token, code = calendar_booking_code

        data = post_graphql(
            anon_client,
            AVAILABLE_TIMES_WITH_CODE,
            {
                "code": code,
                "startDatetime": "2025-09-02T23:59:59Z",
                "endDatetime": "2025-09-02T00:00:00Z",
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Invalid time range."

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_over_max_range_group_field_rejected(
        self,
        mock_rate_limiter,
        anon_client,
        group_booking_code,
    ):
        """A range exceeding MAX_CODE_GATED_RANGE (366 days) is rejected on a group field."""
        mock_rate_limiter.return_value = iter([None])
        _token, code = group_booking_code

        data = post_graphql(
            anon_client,
            CALENDAR_GROUP_BOOKABLE_SLOTS_WITH_CODE,
            {
                "code": code,
                "searchWindowStart": "2025-01-01T00:00:00Z",
                # 367 days — one day over the 366-day cap
                "searchWindowEnd": "2026-01-03T00:00:00Z",
                "durationSeconds": 3600,
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Requested time range is too large."


# ---------------------------------------------------------------------------
# Calendar bookable slots with code
# ---------------------------------------------------------------------------

CALENDAR_BOOKABLE_SLOTS_WITH_CODE = """
query CalendarBookableSlotsWithCode(
    $code: String!,
    $searchWindowStart: DateTime!,
    $searchWindowEnd: DateTime!,
    $durationSeconds: Int!
) {
    calendarBookableSlotsWithCode(
        code: $code,
        searchWindowStart: $searchWindowStart,
        searchWindowEnd: $searchWindowEnd,
        durationSeconds: $durationSeconds
    ) {
        startTime
        endTime
    }
}
"""


@pytest.mark.django_db
class TestCalendarBookableSlotsWithCode:
    """Tests for calendarBookableSlotsWithCode — code-gated single/bundle slots."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_returns_slots_for_valid_calendar_code(
        self, mock_rate_limiter, anon_client, calendar_booking_code
    ):
        """A valid calendar-scoped code returns bookable slots with no auth header."""
        from di_core.containers import container

        mock_rate_limiter.return_value = iter([None])
        _token, code = calendar_booking_code

        start = datetime.datetime(2025, 9, 2, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2025, 9, 2, 10, 30, tzinfo=datetime.UTC)
        proposals = [
            BookableSlotProposal(start_time=start, end_time=start + datetime.timedelta(minutes=30)),
            BookableSlotProposal(start_time=start + datetime.timedelta(minutes=30), end_time=end),
        ]

        mock_slots_service = Mock()
        mock_slots_service.initialize.return_value = None
        mock_slots_service.find_bookable_slots_for_calendar.return_value = proposals

        with container.bookable_slots_service.override(mock_slots_service):
            data = post_graphql(
                anon_client,
                CALENDAR_BOOKABLE_SLOTS_WITH_CODE,
                {
                    "code": code,
                    "searchWindowStart": start.isoformat(),
                    "searchWindowEnd": end.isoformat(),
                    "durationSeconds": 30 * 60,
                },
            )

        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["calendarBookableSlotsWithCode"]
        assert len(result) == 2
        assert result[0]["startTime"] == start.isoformat()
        assert result[0]["endTime"] == (start + datetime.timedelta(minutes=30)).isoformat()
        assert result[1]["startTime"] == (start + datetime.timedelta(minutes=30)).isoformat()
        assert result[1]["endTime"] == end.isoformat()

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_code_not_consumed_after_read(
        self, mock_rate_limiter, anon_client, calendar_booking_code
    ):
        """calendarBookableSlotsWithCode must not consume the code."""
        from di_core.containers import container

        mock_rate_limiter.return_value = iter([None])
        token, code = calendar_booking_code

        start = datetime.datetime(2025, 9, 2, 9, 0, tzinfo=datetime.UTC)
        end = datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC)

        mock_slots_service = Mock()
        mock_slots_service.initialize.return_value = None
        mock_slots_service.find_bookable_slots_for_calendar.return_value = []

        with container.bookable_slots_service.override(mock_slots_service):
            post_graphql(
                anon_client,
                CALENDAR_BOOKABLE_SLOTS_WITH_CODE,
                {
                    "code": code,
                    "searchWindowStart": start.isoformat(),
                    "searchWindowEnd": end.isoformat(),
                    "durationSeconds": 30 * 60,
                },
            )

        token.refresh_from_db()
        assert token.used_at is None, "used_at should remain NULL after a read"

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_invalid_code_returns_error(self, mock_rate_limiter, anon_client):
        """An invalid / unknown code returns the uniform error message."""
        mock_rate_limiter.return_value = iter([None])

        data = post_graphql(
            anon_client,
            CALENDAR_BOOKABLE_SLOTS_WITH_CODE,
            {
                "code": "aW52YWxpZA==",  # "invalid" base64 — no matching token
                "searchWindowStart": "2025-09-02T09:00:00Z",
                "searchWindowEnd": "2025-09-02T10:00:00Z",
                "durationSeconds": 30 * 60,
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Invalid or expired code."

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_group_code_rejected(self, mock_rate_limiter, anon_client, group_booking_code):
        """A group-scoped code is rejected (single/bundle calendars only)."""
        mock_rate_limiter.return_value = iter([None])
        _token, code = group_booking_code

        data = post_graphql(
            anon_client,
            CALENDAR_BOOKABLE_SLOTS_WITH_CODE,
            {
                "code": code,
                "searchWindowStart": "2025-09-02T09:00:00Z",
                "searchWindowEnd": "2025-09-02T10:00:00Z",
                "durationSeconds": 30 * 60,
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Invalid or expired code."

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_backwards_range_rejected(self, mock_rate_limiter, anon_client, calendar_booking_code):
        """A backwards range (end <= start) is rejected."""
        mock_rate_limiter.return_value = iter([None])
        _token, code = calendar_booking_code

        data = post_graphql(
            anon_client,
            CALENDAR_BOOKABLE_SLOTS_WITH_CODE,
            {
                "code": code,
                "searchWindowStart": "2025-09-02T10:00:00Z",
                "searchWindowEnd": "2025-09-02T09:00:00Z",
                "durationSeconds": 30 * 60,
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Invalid time range."

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_over_max_range_rejected(self, mock_rate_limiter, anon_client, calendar_booking_code):
        """A range exceeding MAX_CODE_GATED_RANGE (366 days) is rejected."""
        mock_rate_limiter.return_value = iter([None])
        _token, code = calendar_booking_code

        data = post_graphql(
            anon_client,
            CALENDAR_BOOKABLE_SLOTS_WITH_CODE,
            {
                "code": code,
                "searchWindowStart": "2025-01-01T00:00:00Z",
                # 367 days — one day over the 366-day cap
                "searchWindowEnd": "2026-01-03T00:00:00Z",
                "durationSeconds": 30 * 60,
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Requested time range is too large."


# ---------------------------------------------------------------------------
# Strengthened tests: real-service, policy-filtering, equivalence,
# bundle, expired/revoked/used, no-policy-field-disclosure
# ---------------------------------------------------------------------------

# Authenticated analog used for the equivalence test.
_CALENDAR_BOOKABLE_SLOTS_AUTHED = """
query CalendarBookableSlotsAuthed(
    $calendarId: Int!,
    $searchWindowStart: DateTime!,
    $searchWindowEnd: DateTime!,
    $durationSeconds: Int!,
    $slotStepSeconds: Int!
) {
    calendarBookableSlots(
        calendarId: $calendarId,
        searchWindowStart: $searchWindowStart,
        searchWindowEnd: $searchWindowEnd,
        durationSeconds: $durationSeconds,
        slotStepSeconds: $slotStepSeconds
    ) {
        startTime
        endTime
    }
}
"""

# A query that requests a non-existent field — to prove the type is slots-only.
_CALENDAR_BOOKABLE_SLOTS_WITH_CODE_BAD_FIELD = """
query CalendarBookableSlotsWithCodeBadField(
    $code: String!,
    $searchWindowStart: DateTime!,
    $searchWindowEnd: DateTime!,
    $durationSeconds: Int!
) {
    calendarBookableSlotsWithCode(
        code: $code,
        searchWindowStart: $searchWindowStart,
        searchWindowEnd: $searchWindowEnd,
        durationSeconds: $durationSeconds
    ) {
        startTime
        endTime
        leadTimeMinutes
    }
}
"""


_managed_calendar_counter = 0


def _managed_calendar_for_org(
    org: Organization, *, calendar_type=CalendarType.PERSONAL
) -> Calendar:
    """Create a managed internal calendar for the given org with a unique external_id."""
    global _managed_calendar_counter
    _managed_calendar_counter += 1
    return Calendar.objects.create(
        organization=org,
        name="Slots cal",
        external_id=f"slots-cal-{_managed_calendar_counter}",
        provider=CalendarProvider.INTERNAL,
        calendar_type=calendar_type,
        manage_available_windows=True,
        accepts_public_scheduling=True,
    )


def _available_time(
    org: Organization, cal: Calendar, start: datetime.datetime, end: datetime.datetime
) -> AvailableTime:
    return AvailableTime.objects.create(
        organization=org,
        calendar=cal,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
    )


def _calendar_event(
    org: Organization, cal: Calendar, start: datetime.datetime, end: datetime.datetime
) -> CalendarEvent:
    return CalendarEvent.objects.create(
        organization=org,
        calendar_fk=cal,
        title="Busy",
        description="",
        external_id=f"ev-{cal.id}-{start.isoformat()}",
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
    )


def _authed_client_with_bookable_slots(org):
    """Return an APIClient authenticated as a system user with BOOKABLE_SLOTS resource."""
    from public_api.constants import PublicAPIResources
    from public_api.models import ResourceAccess
    from public_api.services import PublicAPIAuthService

    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name="slots_integration",
        organization=org,
    )
    baker.make(
        ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.BOOKABLE_SLOTS
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")
    return client


@pytest.mark.django_db
class TestCalendarBookableSlotsWithCodeStrengthened:
    """Strengthened tests: real service, policy filtering, equivalence,
    bundle, expired/revoked/used, and no policy-field disclosure."""

    # ------------------------------------------------------------------
    # 1. Real-service happy path — no mocks on BookableSlotsService
    # ------------------------------------------------------------------

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_real_service_returns_free_slots_excludes_busy_span(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
    ):
        """Real-service path (no mock): unmanaged calendar with a blocking event.

        An unmanaged calendar is free by default; a CalendarEvent blocks the
        10:00-10:30 window.  With 30-min duration and 30-min step, candidates
        within the 09:00-11:00 search window are: 09:00, 09:30, 10:00, 10:30.
        The event blocks 10:00 → that candidate is absent; 09:00, 09:30, 10:30
        must be present.
        """
        mock_rate_limiter.return_value = iter([None])

        org = baker.make(Organization, name="Real Happy Org", should_sync_rooms=False)
        # Unmanaged calendar: free everywhere unless blocked by an event.
        global _managed_calendar_counter
        _managed_calendar_counter += 1
        cal = Calendar.objects.create(
            organization=org,
            name="Unmanaged cal",
            external_id=f"unmanaged-cal-{_managed_calendar_counter}",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=False,
            accepts_public_scheduling=True,
        )

        window_start = datetime.datetime(2027, 3, 15, 9, 0, tzinfo=datetime.UTC)
        window_end = datetime.datetime(2027, 3, 15, 11, 0, tzinfo=datetime.UTC)
        busy_start = datetime.datetime(2027, 3, 15, 10, 0, tzinfo=datetime.UTC)
        busy_end = datetime.datetime(2027, 3, 15, 10, 30, tzinfo=datetime.UTC)

        # The CalendarEvent blocks 10:00-10:30 on the unmanaged calendar.
        _calendar_event(org, cal, busy_start, busy_end)

        _token, code = permission_service.create_booking_token(
            organization_id=org.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=cal.id,
        )

        data = post_graphql(
            anon_client,
            CALENDAR_BOOKABLE_SLOTS_WITH_CODE,
            {
                "code": code,
                "searchWindowStart": window_start.isoformat(),
                "searchWindowEnd": window_end.isoformat(),
                "durationSeconds": 30 * 60,
            },
        )

        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["calendarBookableSlotsWithCode"]
        returned_starts = [r["startTime"] for r in result]

        # Free slots: 09:00, 09:30, 10:30 (10:00 is blocked).
        assert (
            datetime.datetime(2027, 3, 15, 9, 0, tzinfo=datetime.UTC).isoformat() in returned_starts
        )
        assert (
            datetime.datetime(2027, 3, 15, 9, 30, tzinfo=datetime.UTC).isoformat()
            in returned_starts
        )
        assert (
            datetime.datetime(2027, 3, 15, 10, 30, tzinfo=datetime.UTC).isoformat()
            in returned_starts
        )
        # Busy slot must be absent.
        assert (
            datetime.datetime(2027, 3, 15, 10, 0, tzinfo=datetime.UTC).isoformat()
            not in returned_starts
        )

    # ------------------------------------------------------------------
    # 2. Policy filtering — lead-time excludes near-future slots
    # ------------------------------------------------------------------

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_booking_policy_lead_time_filters_slots_via_code(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
    ):
        """A BookingPolicy with lead_time_seconds on the calendar excludes near-future slots.

        A 4-hour lead time is set on the calendar.  An availability window close to now
        (1 hour from now) must be excluded; one 6 hours from now must be present.
        """
        from django.utils import timezone as tz

        from calendar_integration.factories import create_booking_policy

        mock_rate_limiter.return_value = iter([None])

        org = baker.make(Organization, name="Policy Lead Org", should_sync_rooms=False)
        cal = _managed_calendar_for_org(org)

        # Lead-time of 4 hours: any slot starting within 4 hours of 'now' is cut.
        create_booking_policy(calendar=cal, lead_time_seconds=4 * 3600)

        now = tz.now()
        # Near-future slot: 1 hour from now → blocked by 4-hour lead-time.
        near_start = (now + datetime.timedelta(hours=1)).replace(second=0, microsecond=0)
        near_end = near_start + datetime.timedelta(hours=1)
        # Far-future slot: 6 hours from now → passes lead-time.
        far_start = (now + datetime.timedelta(hours=6)).replace(second=0, microsecond=0)
        far_end = far_start + datetime.timedelta(hours=1)

        _available_time(org, cal, near_start, near_end)
        _available_time(org, cal, far_start, far_end)

        _token, code = permission_service.create_booking_token(
            organization_id=org.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=cal.id,
        )

        # Search over both windows.
        search_start = near_start
        search_end = far_end

        data = post_graphql(
            anon_client,
            CALENDAR_BOOKABLE_SLOTS_WITH_CODE,
            {
                "code": code,
                "searchWindowStart": search_start.isoformat(),
                "searchWindowEnd": search_end.isoformat(),
                "durationSeconds": 60 * 60,
            },
        )

        assert "errors" not in data or len(data.get("errors", [])) == 0
        result = data["data"]["calendarBookableSlotsWithCode"]
        returned_starts = [r["startTime"] for r in result]

        # Near slot must be absent (lead-time blocks it).
        assert near_start.isoformat() not in returned_starts
        # Far slot must be present.
        assert far_start.isoformat() in returned_starts

    # ------------------------------------------------------------------
    # 3. Equivalence: code-gated == authenticated for same calendar + inputs
    # ------------------------------------------------------------------

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_code_gated_equals_authenticated_calendarBookableSlots(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
    ):
        """calendarBookableSlotsWithCode and calendarBookableSlots return identical slot lists."""
        mock_rate_limiter.return_value = iter([None])

        org = baker.make(Organization, name="Equiv Org", should_sync_rooms=False)
        cal = _managed_calendar_for_org(org)

        window_start = datetime.datetime(2028, 5, 10, 9, 0, tzinfo=datetime.UTC)
        window_end = datetime.datetime(2028, 5, 10, 11, 0, tzinfo=datetime.UTC)
        busy_start = datetime.datetime(2028, 5, 10, 9, 30, tzinfo=datetime.UTC)
        busy_end = datetime.datetime(2028, 5, 10, 10, 0, tzinfo=datetime.UTC)

        _available_time(org, cal, window_start, window_end)
        _calendar_event(org, cal, busy_start, busy_end)

        _token, code = permission_service.create_booking_token(
            organization_id=org.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=cal.id,
        )

        # Code-gated (unauthenticated).
        code_data = post_graphql(
            anon_client,
            CALENDAR_BOOKABLE_SLOTS_WITH_CODE,
            {
                "code": code,
                "searchWindowStart": window_start.isoformat(),
                "searchWindowEnd": window_end.isoformat(),
                "durationSeconds": 30 * 60,
            },
        )
        assert "errors" not in code_data or len(code_data.get("errors", [])) == 0
        code_slots = code_data["data"]["calendarBookableSlotsWithCode"]

        # Authenticated analog.
        authed_client = _authed_client_with_bookable_slots(org)
        auth_data = post_graphql(
            authed_client,
            _CALENDAR_BOOKABLE_SLOTS_AUTHED,
            {
                "calendarId": cal.id,
                "searchWindowStart": window_start.isoformat(),
                "searchWindowEnd": window_end.isoformat(),
                "durationSeconds": 30 * 60,
                "slotStepSeconds": 15 * 60,
            },
        )
        assert "errors" not in auth_data or len(auth_data.get("errors", [])) == 0
        auth_slots = auth_data["data"]["calendarBookableSlots"]

        assert code_slots == auth_slots, (
            f"Mismatch between code-gated and authenticated slot lists.\n"
            f"code-gated: {code_slots}\nauthenticated: {auth_slots}"
        )

    # ------------------------------------------------------------------
    # 4. Bundle-scoped code: busy child suppresses slot
    # ------------------------------------------------------------------

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_bundle_code_slot_suppressed_by_busy_child(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
    ):
        """A bundle calendar code: a slot offered only when ALL children are free.

        Two child calendars; only child_a has availability → bundle slot absent.
        Then availability is added to child_b → bundle slot appears.
        """
        mock_rate_limiter.return_value = iter([None])

        org = baker.make(Organization, name="Bundle Code Org", should_sync_rooms=False)

        child_a = _managed_calendar_for_org(org)
        child_b = _managed_calendar_for_org(org)

        # Bundle calendar (not managed itself — children are managed).
        global _managed_calendar_counter
        _managed_calendar_counter += 1
        bundle = Calendar.objects.create(
            organization=org,
            name="Bundle",
            external_id=f"bundle-code-test-{_managed_calendar_counter}",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.BUNDLE,
            manage_available_windows=False,
        )
        ChildrenCalendarRelationship.objects.create(
            organization=org,
            bundle_calendar=bundle,
            child_calendar=child_a,
            is_primary=True,
        )
        ChildrenCalendarRelationship.objects.create(
            organization=org,
            bundle_calendar=bundle,
            child_calendar=child_b,
            is_primary=False,
        )

        window_start = datetime.datetime(2028, 7, 1, 9, 0, tzinfo=datetime.UTC)
        window_end = datetime.datetime(2028, 7, 1, 10, 0, tzinfo=datetime.UTC)

        # Only child_a is available; child_b has none → bundle slot must be absent.
        _available_time(org, child_a, window_start, window_end)

        _token, code = permission_service.create_booking_token(
            organization_id=org.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=bundle.id,
        )

        # --- Only child_a available → no bundle slot ---
        data_a = post_graphql(
            anon_client,
            CALENDAR_BOOKABLE_SLOTS_WITH_CODE,
            {
                "code": code,
                "searchWindowStart": window_start.isoformat(),
                "searchWindowEnd": window_end.isoformat(),
                "durationSeconds": 30 * 60,
            },
        )
        assert "errors" not in data_a or len(data_a.get("errors", [])) == 0
        assert data_a["data"]["calendarBookableSlotsWithCode"] == [], (
            "Bundle slot must not appear when child_b has no availability"
        )

        # --- Both children available → slot appears ---
        _available_time(org, child_b, window_start, window_end)

        data_b = post_graphql(
            anon_client,
            CALENDAR_BOOKABLE_SLOTS_WITH_CODE,
            {
                "code": code,
                "searchWindowStart": window_start.isoformat(),
                "searchWindowEnd": window_end.isoformat(),
                "durationSeconds": 30 * 60,
            },
        )
        assert "errors" not in data_b or len(data_b.get("errors", [])) == 0
        slots_b = data_b["data"]["calendarBookableSlotsWithCode"]
        assert len(slots_b) > 0, "Bundle slot must appear when both children are free"

    # ------------------------------------------------------------------
    # 5a. Revoked code → uniform error
    # ------------------------------------------------------------------

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_revoked_code_returns_uniform_error(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
        organization,
        calendar,
    ):
        """A revoked calendar code returns the uniform 'Invalid or expired code.' error."""
        mock_rate_limiter.return_value = iter([None])

        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=calendar.id,
        )
        permission_service.revoke_token(organization_id=organization.id, token_id=token.id)

        data = post_graphql(
            anon_client,
            CALENDAR_BOOKABLE_SLOTS_WITH_CODE,
            {
                "code": code,
                "searchWindowStart": "2027-09-02T09:00:00Z",
                "searchWindowEnd": "2027-09-02T10:00:00Z",
                "durationSeconds": 30 * 60,
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Invalid or expired code."

    # ------------------------------------------------------------------
    # 5b. Expired code → uniform error
    # ------------------------------------------------------------------

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_expired_code_returns_uniform_error(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
        organization,
        calendar,
    ):
        """An expired calendar code returns the uniform 'Invalid or expired code.' error."""
        mock_rate_limiter.return_value = iter([None])

        past = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
        _token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=calendar.id,
            expires_at=past,
        )

        data = post_graphql(
            anon_client,
            CALENDAR_BOOKABLE_SLOTS_WITH_CODE,
            {
                "code": code,
                "searchWindowStart": "2027-09-02T09:00:00Z",
                "searchWindowEnd": "2027-09-02T10:00:00Z",
                "durationSeconds": 30 * 60,
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Invalid or expired code."

    # ------------------------------------------------------------------
    # 5c. Used (consumed) code → uniform error
    # ------------------------------------------------------------------

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_used_code_returns_uniform_error(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
        organization,
        calendar,
    ):
        """A used (consumed) calendar code returns the uniform 'Invalid or expired code.' error."""
        mock_rate_limiter.return_value = iter([None])

        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=calendar.id,
        )
        CalendarManagementToken.objects.filter(id=token.id).update(
            used_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
        )

        data = post_graphql(
            anon_client,
            CALENDAR_BOOKABLE_SLOTS_WITH_CODE,
            {
                "code": code,
                "searchWindowStart": "2027-09-02T09:00:00Z",
                "searchWindowEnd": "2027-09-02T10:00:00Z",
                "durationSeconds": 30 * 60,
            },
        )

        assert "errors" in data and len(data["errors"]) > 0
        assert data["errors"][0]["message"] == "Invalid or expired code."

    # ------------------------------------------------------------------
    # 6. No policy-field disclosure: leadTimeMinutes must be a GraphQL error
    # ------------------------------------------------------------------

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_no_policy_field_disclosure_lead_time_minutes(
        self,
        mock_rate_limiter,
        anon_client,
        calendar_booking_code,
    ):
        """BookableSlotProposalGraphQLType exposes only startTime/endTime.

        Requesting a non-existent field (leadTimeMinutes) must produce a GraphQL
        validation error — proving that policy rule values are not surfaced.
        """
        mock_rate_limiter.return_value = iter([None])
        _token, code = calendar_booking_code

        data = post_graphql(
            anon_client,
            _CALENDAR_BOOKABLE_SLOTS_WITH_CODE_BAD_FIELD,
            {
                "code": code,
                "searchWindowStart": "2027-09-02T09:00:00Z",
                "searchWindowEnd": "2027-09-02T10:00:00Z",
                "durationSeconds": 30 * 60,
            },
        )

        assert "errors" in data and len(data["errors"]) > 0, (
            "Expected a GraphQL validation error for unknown field 'leadTimeMinutes'"
        )
        # The error must mention the unknown field, not a business-logic failure.
        error_messages = " ".join(e["message"] for e in data["errors"])
        assert "leadTimeMinutes" in error_messages or "Cannot query field" in error_messages
