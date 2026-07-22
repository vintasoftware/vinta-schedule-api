"""Integration tests for rescheduleCalendarEventWithCode.

All requests are unauthenticated (no Authorization header).  The reschedule
code provides the org scope, the specific event scope, and the RESCHEDULE
permission.

Scenario coverage:
1. Happy path — valid code + in-window new slot → event times updated, title/description/
   attendees PRESERVED, code consumed.  Works on a RESTRICTED calendar (proves the
   code authorizes via RESCHEDULE; also proves details-preservation so only RESCHEDULE
   is required — if details were accidentally altered the test would fail with
   PermissionDenied).
2. Replay — same code again → ALREADY_USED, event not changed again.
3. Failed reschedule (new slot outside availability) → SLOT_UNAVAILABLE, code NOT
   consumed (``used_at`` remains NULL), retryable.
4. Wrong permission — a CREATE/CANCEL-only code → NOT_PERMITTED.
5. Wrong scope — a group reschedule code (token.calendar_group set) → NOT_PERMITTED.
6. Code is event-bound: calendar_id and event_id come from the token, so the code
   always targets exactly ``token.event``.
7. Expired / revoked / invalid → respective error.
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

RESCHEDULE_WITH_CODE = """
mutation RescheduleCalendarEventWithCode($input: RescheduleWithCodeInput!) {
    rescheduleCalendarEventWithCode(input: $input) {
        success
        errorCode
        errorMessage
        event {
            id
            title
            startTime
            endTime
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
    return baker.make(Organization, name="Reschedule-With-Code Test Org")


@pytest.fixture
def other_organization():
    return baker.make(Organization, name="Other Org")


@pytest.fixture
def calendar(organization):
    """A RESTRICTED calendar (accepts_public_scheduling=False) with managed availability.

    The reschedule code grants RESCHEDULE permission so that can_perform_update
    returns True even though public scheduling is disabled.
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
    """Availability window covering both the original and the new slots.

    Original slot: 10:00-11:00 UTC
    New slot:      14:00-15:00 UTC
    Both fall inside 09:00-17:00 UTC.
    """
    return baker.make(
        AvailableTime,
        organization=organization,
        calendar=calendar,
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
        timezone="UTC",
    )


@pytest.fixture
def existing_event(organization, calendar):
    """An existing event with a title, description, and one external attendee."""
    from calendar_integration.models import EventExternalAttendance, ExternalAttendee

    event = baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        title="Original Title",
        description="Original description.",
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 11, 0),
        external_id="",
    )
    external_attendee = baker.make(
        ExternalAttendee,
        organization=organization,
        email="patient@example.com",
        name="Pat Patient",
    )
    baker.make(
        EventExternalAttendance,
        organization=organization,
        event=event,
        external_attendee=external_attendee,
    )
    return event


@pytest.fixture
def permission_service():
    return CalendarPermissionService()


@pytest.fixture
def reschedule_code(permission_service, organization, calendar, existing_event):
    """A valid single-use RESCHEDULE code bound to ``existing_event``."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_id=calendar.id,
        event_id=existing_event.id,
    )
    return token, code


@pytest.fixture
def cancel_code(permission_service, organization, calendar, existing_event):
    """A CANCEL-only code — wrong permission for rescheduling."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CANCEL],
        calendar_id=calendar.id,
        event_id=existing_event.id,
    )
    return token, code


@pytest.fixture
def create_code(permission_service, organization, calendar):
    """A CREATE-only code — wrong permission for rescheduling."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )
    return token, code


@pytest.fixture
def group_reschedule_code(permission_service, organization, calendar_group, existing_event):
    """A group-scoped RESCHEDULE code — wrong scope for the single-calendar mutation."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_group_id=calendar_group.id,
        event_id=existing_event.id,
    )
    return token, code


@pytest.fixture
def anon_client():
    """APIClient with no Authorization header."""
    return APIClient()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Original slot: 10:00-11:00 UTC (set on existing_event fixture)
ORIGINAL_START = datetime.datetime(2030, 6, 1, 10, 0, tzinfo=datetime.UTC)
ORIGINAL_END = datetime.datetime(2030, 6, 1, 11, 0, tzinfo=datetime.UTC)

# New slot for the happy-path reschedule: 14:00-15:00 UTC (inside the availability window)
NEW_START = datetime.datetime(2030, 6, 1, 14, 0, tzinfo=datetime.UTC)
NEW_END = datetime.datetime(2030, 6, 1, 15, 0, tzinfo=datetime.UTC)

# Out-of-window slot: 22:00-23:00 UTC (outside the 09:00-17:00 window)
OOW_START = datetime.datetime(2030, 6, 1, 22, 0, tzinfo=datetime.UTC)
OOW_END = datetime.datetime(2030, 6, 1, 23, 0, tzinfo=datetime.UTC)


def _reschedule_input(code: str, **overrides) -> dict:
    """Build a default happy-path mutation input dict."""
    base = {
        "code": code,
        "startTime": NEW_START.isoformat(),
        "endTime": NEW_END.isoformat(),
        "timezone": "UTC",
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
class TestRescheduleCalendarEventWithCodeHappyPath:
    """Scenario 1: Valid reschedule code + in-window new slot → success, code consumed.

    Also verifies that title, description, and attendees are PRESERVED so that only
    RESCHEDULE permission is required (not UPDATE_DETAILS / UPDATE_ATTENDEES).
    Uses a RESTRICTED calendar (accepts_public_scheduling=False) to prove the code
    alone authorizes the operation.
    """

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_happy_path_reschedules_event_and_consumes_code(
        self,
        mock_rate_limiter,
        anon_client,
        reschedule_code,
        organization,
        calendar,
        existing_event,
        available_window,  # noqa: ARG002 — seeds DB rows
    ):
        mock_rate_limiter.return_value = iter([None])
        token, code = reschedule_code

        data = post_graphql(
            anon_client,
            RESCHEDULE_WITH_CODE,
            {"input": _reschedule_input(code)},
        )

        assert "errors" not in data or not data.get("errors"), data
        result = data["data"]["rescheduleCalendarEventWithCode"]
        assert result["success"] is True, result
        assert result["errorCode"] is None
        assert result["event"] is not None

        # The event returned is the bound event.
        event_id = int(result["event"]["id"])
        assert event_id == existing_event.id

        # The code must be consumed.
        token.refresh_from_db()
        assert token.used_at is not None
        assert token.consumed_source_ip is not None

        # Times must have been updated.  ``start_time_tz_unaware`` is returned with
        # UTC tzinfo when USE_TZ=True, so compare by stripping tzinfo from both sides.
        existing_event.refresh_from_db()
        assert existing_event.start_time_tz_unaware.replace(tzinfo=None) == NEW_START.replace(
            tzinfo=None
        )
        assert existing_event.end_time_tz_unaware.replace(tzinfo=None) == NEW_END.replace(
            tzinfo=None
        )

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_title_description_and_attendees_preserved(
        self,
        mock_rate_limiter,
        anon_client,
        reschedule_code,
        organization,
        calendar,
        existing_event,
        available_window,  # noqa: ARG002
    ):
        """Title, description, and external attendee are unchanged after reschedule."""
        mock_rate_limiter.return_value = iter([None])
        _token, code = reschedule_code

        data = post_graphql(
            anon_client,
            RESCHEDULE_WITH_CODE,
            {"input": _reschedule_input(code)},
        )

        result = data["data"]["rescheduleCalendarEventWithCode"]
        assert result["success"] is True, result

        # Title preserved.
        assert result["event"]["title"] == "Original Title"

        # External attendee preserved (via GraphQL response).
        external_attendances = result["event"]["externalAttendances"]
        assert len(external_attendances) == 1
        assert external_attendances[0]["externalAttendee"]["email"] == "patient@example.com"
        assert external_attendances[0]["externalAttendee"]["name"] == "Pat Patient"

        # DB: title and description unchanged.
        existing_event.refresh_from_db()
        assert existing_event.title == "Original Title"
        assert existing_event.description == "Original description."

        # DB: external attendee still exists.
        from calendar_integration.models import EventExternalAttendance

        ext_attendances = list(
            EventExternalAttendance.objects.filter_by_organization(organization.id)
            .select_related("external_attendee")
            .filter(event=existing_event)
        )
        assert len(ext_attendances) == 1
        assert ext_attendances[0].external_attendee.email == "patient@example.com"

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_event_belongs_to_bound_event(
        self,
        mock_rate_limiter,
        anon_client,
        reschedule_code,
        organization,
        calendar,
        existing_event,
        available_window,  # noqa: ARG002
    ):
        """The rescheduled event is exactly the event the code was bound to."""
        mock_rate_limiter.return_value = iter([None])
        token, code = reschedule_code

        data = post_graphql(
            anon_client,
            RESCHEDULE_WITH_CODE,
            {"input": _reschedule_input(code)},
        )

        result = data["data"]["rescheduleCalendarEventWithCode"]
        assert result["success"] is True, result

        returned_event_id = int(result["event"]["id"])
        assert returned_event_id == existing_event.id
        assert returned_event_id == token.event_fk_id


@pytest.mark.django_db
class TestRescheduleCalendarEventWithCodeReplay:
    """Scenario 2: Replay with same code → ALREADY_USED, event not changed again."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_replay_returns_already_used(
        self,
        mock_rate_limiter,
        anon_client,
        reschedule_code,
        organization,
        existing_event,
        available_window,  # noqa: ARG002
    ):
        mock_rate_limiter.return_value = iter([None])
        _token, code = reschedule_code
        input_data = _reschedule_input(code)

        # First call — must succeed.
        first = post_graphql(anon_client, RESCHEDULE_WITH_CODE, {"input": input_data})
        assert first["data"]["rescheduleCalendarEventWithCode"]["success"] is True

        # Second call — same code must return ALREADY_USED.
        second = post_graphql(anon_client, RESCHEDULE_WITH_CODE, {"input": input_data})
        result = second["data"]["rescheduleCalendarEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "ALREADY_USED"

        # Event times must remain as set by the first reschedule, not reset again.
        existing_event.refresh_from_db()
        assert existing_event.start_time_tz_unaware.replace(tzinfo=None) == NEW_START.replace(
            tzinfo=None
        )


@pytest.mark.django_db
class TestRescheduleCalendarEventWithCodeSlotUnavailable:
    """Scenario 3: New slot outside availability → SLOT_UNAVAILABLE, code NOT consumed."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_slot_outside_window_does_not_consume(
        self,
        mock_rate_limiter,
        anon_client,
        reschedule_code,
        organization,
        existing_event,
        available_window,  # noqa: ARG002 — window is 09:00-17:00 UTC
    ):
        mock_rate_limiter.return_value = iter([None])
        token, code = reschedule_code

        # Request a slot at 22:00-23:00 UTC — outside the 09:00-17:00 window.
        data = post_graphql(
            anon_client,
            RESCHEDULE_WITH_CODE,
            {
                "input": _reschedule_input(
                    code,
                    startTime=OOW_START.isoformat(),
                    endTime=OOW_END.isoformat(),
                )
            },
        )

        result = data["data"]["rescheduleCalendarEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "SLOT_UNAVAILABLE"
        assert result["errorMessage"] == "The requested time slot is not available."

        # Code must still be active (not consumed).
        token.refresh_from_db()
        assert token.used_at is None

        # Event times must be unchanged.
        existing_event.refresh_from_db()
        assert existing_event.start_time_tz_unaware.replace(tzinfo=None) == ORIGINAL_START.replace(
            tzinfo=None
        )

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_after_failed_reschedule_code_can_still_be_used(
        self,
        mock_rate_limiter,
        anon_client,
        reschedule_code,
        organization,
        existing_event,
        available_window,  # noqa: ARG002
    ):
        """After a SLOT_UNAVAILABLE failure the same code can succeed on a valid slot."""
        mock_rate_limiter.return_value = iter([None])
        token, code = reschedule_code

        # First: out-of-window → failure.
        fail = post_graphql(
            anon_client,
            RESCHEDULE_WITH_CODE,
            {
                "input": _reschedule_input(
                    code,
                    startTime=OOW_START.isoformat(),
                    endTime=OOW_END.isoformat(),
                )
            },
        )
        assert fail["data"]["rescheduleCalendarEventWithCode"]["errorCode"] == "SLOT_UNAVAILABLE"
        token.refresh_from_db()
        assert token.used_at is None

        # Second: in-window → success.
        data = post_graphql(
            anon_client,
            RESCHEDULE_WITH_CODE,
            {"input": _reschedule_input(code)},
        )
        result = data["data"]["rescheduleCalendarEventWithCode"]
        assert result["success"] is True, result
        token.refresh_from_db()
        assert token.used_at is not None


@pytest.mark.django_db
class TestRescheduleCalendarEventWithCodeWrongPermission:
    """Scenario 4: Code without RESCHEDULE permission → NOT_PERMITTED."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_cancel_only_code_returns_not_permitted(
        self,
        mock_rate_limiter,
        anon_client,
        cancel_code,
        organization,
        existing_event,
    ):
        mock_rate_limiter.return_value = iter([None])
        _token, code = cancel_code

        data = post_graphql(
            anon_client,
            RESCHEDULE_WITH_CODE,
            {"input": _reschedule_input(code)},
        )

        result = data["data"]["rescheduleCalendarEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "NOT_PERMITTED"

        # Event times must be unchanged.
        existing_event.refresh_from_db()
        assert existing_event.start_time_tz_unaware.replace(tzinfo=None) == ORIGINAL_START.replace(
            tzinfo=None
        )

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_create_only_code_returns_not_permitted(
        self,
        mock_rate_limiter,
        anon_client,
        create_code,
        organization,
        existing_event,
    ):
        mock_rate_limiter.return_value = iter([None])
        _token, code = create_code

        data = post_graphql(
            anon_client,
            RESCHEDULE_WITH_CODE,
            {"input": _reschedule_input(code)},
        )

        result = data["data"]["rescheduleCalendarEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "NOT_PERMITTED"


@pytest.mark.django_db
class TestRescheduleCalendarEventWithCodeWrongScope:
    """Scenario 5: Group-scoped reschedule code → NOT_PERMITTED (routes to the group reschedule path)."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_group_code_returns_not_permitted(
        self,
        mock_rate_limiter,
        anon_client,
        group_reschedule_code,
        organization,
        existing_event,
    ):
        mock_rate_limiter.return_value = iter([None])
        _token, code = group_reschedule_code

        data = post_graphql(
            anon_client,
            RESCHEDULE_WITH_CODE,
            {"input": _reschedule_input(code)},
        )

        result = data["data"]["rescheduleCalendarEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "NOT_PERMITTED"

        # Event times must be unchanged.
        existing_event.refresh_from_db()
        assert existing_event.start_time_tz_unaware.replace(tzinfo=None) == ORIGINAL_START.replace(
            tzinfo=None
        )


@pytest.mark.django_db
class TestRescheduleCalendarEventWithCodeLifecycleRejections:
    """Scenario 7: Expired / revoked / invalid codes are rejected with the correct error codes."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_expired_code_returns_expired(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
        organization,
        calendar,
        existing_event,
    ):
        mock_rate_limiter.return_value = iter([None])
        past = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
        _token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.RESCHEDULE],
            calendar_id=calendar.id,
            event_id=existing_event.id,
            expires_at=past,
        )

        data = post_graphql(
            anon_client,
            RESCHEDULE_WITH_CODE,
            {"input": _reschedule_input(code)},
        )

        result = data["data"]["rescheduleCalendarEventWithCode"]
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
        existing_event,
    ):
        mock_rate_limiter.return_value = iter([None])
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.RESCHEDULE],
            calendar_id=calendar.id,
            event_id=existing_event.id,
        )
        permission_service.revoke_token(organization_id=organization.id, token_id=token.id)

        data = post_graphql(
            anon_client,
            RESCHEDULE_WITH_CODE,
            {"input": _reschedule_input(code)},
        )

        result = data["data"]["rescheduleCalendarEventWithCode"]
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
            RESCHEDULE_WITH_CODE,
            {"input": _reschedule_input("aW52YWxpZHJlc2NoZWR1bGVjb2Rl")},  # base64 junk
        )

        result = data["data"]["rescheduleCalendarEventWithCode"]
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
        existing_event,
    ):
        """A code already marked as used → ALREADY_USED."""
        mock_rate_limiter.return_value = iter([None])
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.RESCHEDULE],
            calendar_id=calendar.id,
            event_id=existing_event.id,
        )
        CalendarManagementToken.objects.filter(id=token.id).update(
            used_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
        )

        data = post_graphql(
            anon_client,
            RESCHEDULE_WITH_CODE,
            {"input": _reschedule_input(code)},
        )

        result = data["data"]["rescheduleCalendarEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "ALREADY_USED"
