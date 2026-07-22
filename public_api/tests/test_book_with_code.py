"""Integration tests for createCalendarEventWithCode.

All requests are unauthenticated (no Authorization header).  The booking
code provides the org scope, calendar scope, and CREATE permission.

Scenario coverage:
1. Happy path — valid code + available slot → event created, code consumed.
2. Replay — same code again → ALREADY_USED, no second event.
3. Failed write does not consume — SLOT_UNAVAILABLE, code remains active.
4. Wrong permission — code without CREATE → NOT_PERMITTED, no event.
5. Wrong scope — group code used on single-calendar mutation → NOT_PERMITTED.
6. Lifecycle rejections — expired / revoked / invalid → EXPIRED / REVOKED / INVALID_CODE.
7. Cross-org: event is created in the code's org.
"""

import datetime
from unittest.mock import patch

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
    EventManagementPermissions,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from organizations.models import Organization


# ---------------------------------------------------------------------------
# GraphQL mutation string
# ---------------------------------------------------------------------------

CREATE_EVENT_WITH_CODE = """
mutation CreateCalendarEventWithCode($input: CreateEventWithCodeInput!) {
    createCalendarEventWithCode(input: $input) {
        success
        errorCode
        errorMessage
        event {
            id
            title
            externalAttendances {
                externalAttendee {
                    email
                    name
                }
            }
        }
    }
}
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization():
    return baker.make(Organization, name="Book-With-Code Test Org")


@pytest.fixture
def other_organization():
    return baker.make(Organization, name="Other Org")


@pytest.fixture
def calendar(organization):
    """A RESTRICTED calendar (accepts_public_scheduling=False) with managed availability windows.

    Tests must seed AvailableTime rows to make a slot bookable; the code-as-token provides
    the CREATE permission so that can_perform_scheduling returns True via the token path.
    """
    return baker.make(
        Calendar,
        organization=organization,
        name="Test Calendar",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=False,
    )


@pytest.fixture
def calendar_group(organization):
    return baker.make(CalendarGroup, organization=organization, name="Test Group")


@pytest.fixture
def available_window(organization, calendar):
    """A future availability window that covers the test booking slot."""
    return baker.make(
        AvailableTime,
        organization=organization,
        calendar=calendar,
        # Start at 09:00 UTC, end at 17:00 UTC -- covers the test slot (10:00-11:00).
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
        timezone="UTC",
    )


@pytest.fixture
def permission_service():
    return CalendarPermissionService()


@pytest.fixture
def booking_code(permission_service, organization, calendar):
    """A valid single-use CREATE code scoped to `calendar`."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )
    return token, code


@pytest.fixture
def reschedule_code(permission_service, organization, calendar):
    """A RESCHEDULE-only code (no CREATE) — wrong permission for booking."""
    event = baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        title="Existing Event",
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 11, 0),
        external_id="existing-event-external-id",
    )
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_id=calendar.id,
        event_id=event.id,
    )
    return token, code


@pytest.fixture
def group_booking_code(permission_service, organization, calendar_group):
    """A CREATE code scoped to a calendar GROUP (wrong scope for single-calendar mutation)."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=calendar_group.id,
    )
    return token, code


@pytest.fixture
def anon_client():
    """APIClient with no Authorization header."""
    return APIClient()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BOOKING_START = datetime.datetime(2030, 6, 1, 10, 0, tzinfo=datetime.UTC)
BOOKING_END = datetime.datetime(2030, 6, 1, 11, 0, tzinfo=datetime.UTC)


def _booking_input(code: str, **overrides) -> dict:
    """Build the default happy-path mutation input dict."""
    base = {
        "code": code,
        "title": "My Appointment",
        "description": "A test booking",
        "startTime": BOOKING_START.isoformat(),
        "endTime": BOOKING_END.isoformat(),
        "timezone": "UTC",
        "externalAttendee": {
            "email": "patient@example.com",
            "name": "Pat Patient",
        },
    }
    base.update(overrides)
    return base


def post_graphql(client: APIClient, query: str, variables: dict) -> dict:
    response = client.post(
        "/graphql/",
        data={"query": query, "variables": variables},
        format="json",
    )
    assert response.status_code == 200, response.content.decode()
    return response.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateCalendarEventWithCodeHappyPath:
    """Scenario 1: Valid booking code + available slot → event created, code consumed."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_happy_path_creates_event_and_consumes_code(
        self,
        mock_rate_limiter,
        anon_client,
        booking_code,
        organization,
        calendar,
        available_window,  # noqa: ARG002 — seeds DB rows consumed by create_event
    ):
        mock_rate_limiter.return_value = iter([None])
        token, code = booking_code

        data = post_graphql(
            anon_client,
            CREATE_EVENT_WITH_CODE,
            {"input": _booking_input(code)},
        )

        assert "errors" not in data or not data.get("errors"), data
        result = data["data"]["createCalendarEventWithCode"]
        assert result["success"] is True
        assert result["errorCode"] is None
        assert result["event"] is not None
        assert result["event"]["title"] == "My Appointment"

        # Code must be consumed.
        token.refresh_from_db()
        assert token.used_at is not None
        assert token.consumed_source_ip is not None

        # The event must exist in the DB, on the right calendar/org.
        event_id = int(result["event"]["id"])
        event = CalendarEvent.objects.filter_by_organization(organization.id).get(id=event_id)
        assert event.calendar_fk_id == calendar.id
        assert event.organization_id == organization.id

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_event_has_external_attendee(
        self,
        mock_rate_limiter,
        anon_client,
        booking_code,
        organization,
        calendar,
        available_window,  # noqa: ARG002
    ):
        """The created event carries the external attendee supplied in the input."""
        mock_rate_limiter.return_value = iter([None])
        _token, code = booking_code

        data = post_graphql(
            anon_client,
            CREATE_EVENT_WITH_CODE,
            {"input": _booking_input(code)},
        )

        result = data["data"]["createCalendarEventWithCode"]
        assert result["success"] is True
        event_id = int(result["event"]["id"])
        event = CalendarEvent.objects.filter_by_organization(organization.id).get(id=event_id)
        external_attendances = list(event.external_attendances.select_related("external_attendee"))
        assert len(external_attendances) == 1
        assert external_attendances[0].external_attendee.email == "patient@example.com"
        assert external_attendances[0].external_attendee.name == "Pat Patient"


@pytest.mark.django_db
class TestCreateCalendarEventWithCodeReplay:
    """Scenario 2: Replay with same code → ALREADY_USED, no second event."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_replay_returns_already_used(
        self,
        mock_rate_limiter,
        anon_client,
        booking_code,
        organization,
        available_window,  # noqa: ARG002
    ):
        mock_rate_limiter.return_value = iter([None])
        _token, code = booking_code
        input_data = _booking_input(code)

        # First call — must succeed.
        first = post_graphql(anon_client, CREATE_EVENT_WITH_CODE, {"input": input_data})
        assert first["data"]["createCalendarEventWithCode"]["success"] is True

        # Second call — same code must return ALREADY_USED.
        second = post_graphql(anon_client, CREATE_EVENT_WITH_CODE, {"input": input_data})
        result = second["data"]["createCalendarEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "ALREADY_USED"

        # Only one event should have been created.
        event_count = CalendarEvent.objects.filter_by_organization(organization.id).count()
        assert event_count == 1


@pytest.mark.django_db
class TestCreateCalendarEventWithCodeFailedWriteDoesNotConsume:
    """Scenario 3: Failed write (SLOT_UNAVAILABLE) leaves the code active for retry.

    Uses the REAL calendar service against a RESTRICTED calendar with an AvailableTime
    window.  The out-of-window request triggers a genuine NoAvailableTimeWindowsError
    without any mocking, then a follow-up in-window request succeeds.
    """

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_slot_unavailable_does_not_consume_code(
        self,
        mock_rate_limiter,
        anon_client,
        booking_code,
        organization,
        available_window,  # noqa: ARG002 - seeds DB rows; window is 09:00-17:00 UTC
        calendar,
    ):
        """Booking a slot OUTSIDE the availability window returns SLOT_UNAVAILABLE; code stays active."""
        mock_rate_limiter.return_value = iter([None])
        token, code = booking_code

        # Request a slot at 22:00-23:00 UTC - outside the 09:00-17:00 window.
        out_of_window_input = _booking_input(
            code,
            startTime=datetime.datetime(2030, 6, 1, 22, 0, tzinfo=datetime.UTC).isoformat(),
            endTime=datetime.datetime(2030, 6, 1, 23, 0, tzinfo=datetime.UTC).isoformat(),
        )

        data = post_graphql(
            anon_client,
            CREATE_EVENT_WITH_CODE,
            {"input": out_of_window_input},
        )

        result = data["data"]["createCalendarEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "SLOT_UNAVAILABLE"
        assert result["errorMessage"] == "The requested time slot is not available."

        # The code must still be unused so a subsequent call can succeed.
        token.refresh_from_db()
        assert token.used_at is None

        # No event must have been created.
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_after_failed_write_code_can_still_be_used(
        self,
        mock_rate_limiter,
        anon_client,
        booking_code,
        organization,
        available_window,  # noqa: ARG002 - window is 09:00-17:00 UTC
        calendar,
    ):
        """After a SLOT_UNAVAILABLE failure on restricted calendar, the same code succeeds on a valid slot."""
        mock_rate_limiter.return_value = iter([None])
        token, code = booking_code

        # First call - slot outside the availability window: SLOT_UNAVAILABLE.
        out_of_window_input = _booking_input(
            code,
            startTime=datetime.datetime(2030, 6, 1, 22, 0, tzinfo=datetime.UTC).isoformat(),
            endTime=datetime.datetime(2030, 6, 1, 23, 0, tzinfo=datetime.UTC).isoformat(),
        )
        fail_result = post_graphql(
            anon_client,
            CREATE_EVENT_WITH_CODE,
            {"input": out_of_window_input},
        )
        assert fail_result["data"]["createCalendarEventWithCode"]["success"] is False
        assert fail_result["data"]["createCalendarEventWithCode"]["errorCode"] == "SLOT_UNAVAILABLE"

        token.refresh_from_db()
        assert token.used_at is None, "Code must remain active after failed write"

        # Second call - in-window slot (10:00-11:00 UTC); must succeed.
        data = post_graphql(
            anon_client,
            CREATE_EVENT_WITH_CODE,
            {"input": _booking_input(code)},
        )

        result = data["data"]["createCalendarEventWithCode"]
        assert result["success"] is True, result
        token.refresh_from_db()
        assert token.used_at is not None
        assert CalendarEvent.objects.filter_by_organization(organization.id).count() == 1


@pytest.mark.django_db
class TestCreateCalendarEventWithCodeWrongPermission:
    """Scenario 4: Code without CREATE permission → NOT_PERMITTED."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_reschedule_only_code_returns_not_permitted(
        self,
        mock_rate_limiter,
        anon_client,
        reschedule_code,
        organization,
    ):
        mock_rate_limiter.return_value = iter([None])
        _token, code = reschedule_code

        data = post_graphql(
            anon_client,
            CREATE_EVENT_WITH_CODE,
            {"input": _booking_input(code)},
        )

        result = data["data"]["createCalendarEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "NOT_PERMITTED"

        # No event should have been created.
        assert CalendarEvent.objects.filter_by_organization(organization.id).count() == 1


@pytest.mark.django_db
class TestCreateCalendarEventWithCodeWrongScope:
    """Scenario 5: Group-scoped code on single-calendar mutation → NOT_PERMITTED."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_group_code_returns_not_permitted(
        self,
        mock_rate_limiter,
        anon_client,
        group_booking_code,
        organization,
    ):
        mock_rate_limiter.return_value = iter([None])
        _token, code = group_booking_code

        data = post_graphql(
            anon_client,
            CREATE_EVENT_WITH_CODE,
            {"input": _booking_input(code)},
        )

        result = data["data"]["createCalendarEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "NOT_PERMITTED"

        # No event should have been created.
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()


@pytest.mark.django_db
class TestCreateCalendarEventWithCodeLifecycleRejections:
    """Scenario 6: Expired / revoked / invalid codes are rejected with the correct error codes."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_expired_code_returns_expired(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
        organization,
        calendar,
    ):
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
            CREATE_EVENT_WITH_CODE,
            {"input": _booking_input(code)},
        )

        result = data["data"]["createCalendarEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "EXPIRED"

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_revoked_code_returns_revoked(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
        organization,
        calendar,
    ):
        mock_rate_limiter.return_value = iter([None])
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=calendar.id,
        )
        permission_service.revoke_token(organization_id=organization.id, token_id=token.id)

        data = post_graphql(
            anon_client,
            CREATE_EVENT_WITH_CODE,
            {"input": _booking_input(code)},
        )

        result = data["data"]["createCalendarEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "REVOKED"

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_invalid_code_returns_invalid_code(
        self,
        mock_rate_limiter,
        anon_client,
    ):
        mock_rate_limiter.return_value = iter([None])

        data = post_graphql(
            anon_client,
            CREATE_EVENT_WITH_CODE,
            {"input": _booking_input("aW52YWxpZGJvb2tpbmdjb2Rl")},  # "invalidbookingcode" base64
        )

        result = data["data"]["createCalendarEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_used_code_returns_already_used(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
        organization,
        calendar,
    ):
        """A code that was already marked as used before this call → ALREADY_USED."""
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
            CREATE_EVENT_WITH_CODE,
            {"input": _booking_input(code)},
        )

        result = data["data"]["createCalendarEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "ALREADY_USED"


@pytest.mark.django_db
class TestCreateCalendarEventWithCodeCrossOrg:
    """Scenario 7: Event is created in the code's org."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_event_created_in_code_org(
        self,
        mock_rate_limiter,
        anon_client,
        booking_code,
        organization,
        available_window,  # noqa: ARG002
    ):
        mock_rate_limiter.return_value = iter([None])
        token, code = booking_code

        data = post_graphql(
            anon_client,
            CREATE_EVENT_WITH_CODE,
            {"input": _booking_input(code)},
        )

        result = data["data"]["createCalendarEventWithCode"]
        assert result["success"] is True

        event_id = int(result["event"]["id"])
        event = CalendarEvent.objects.filter_by_organization(organization.id).get(id=event_id)
        assert event.organization_id == token.organization_id
