"""Integration tests for code-gated (unauthenticated) availability read fields.

Covers Phase 4:
- availableTimesWithCode
- availabilityWindowsWithCode
- unavailableWindowsWithCode
- calendarGroupBookableSlotsWithCode
- calendarGroupAvailabilityWithCode

All fields are unauthenticated (no Authorization header required).  The booking
code authorizes access to its bound calendar / calendar group.  Reads never
consume the code (used_at remains NULL after any read).
"""

import datetime
from unittest.mock import Mock, patch

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarGroup,
    CalendarManagementToken,
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
