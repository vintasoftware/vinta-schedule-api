"""Integration tests for rescheduleCalendarGroupEventWithCode (Phase 6b).

All requests are unauthenticated (no Authorization header).  The group
reschedule code provides the org scope, the calendar-group scope, the
specific event scope, and the RESCHEDULE permission.

Scenario coverage:
1. Happy path — valid group-reschedule code + in-window new slot → success on a
   RESTRICTED primary calendar (proves the code authorizes), event times updated
   on the primary CalendarEvent AND on the linked non-primary BlockedTimes, event
   id UNCHANGED, details preserved, code consumed.
2. Replay → ALREADY_USED, no further change.
3. Out-of-window new slot → SLOT_UNAVAILABLE, code NOT consumed, retryable.
4. Calendar-scoped reschedule code (token.calendar_group is None) → NOT_PERMITTED
   (routes to rescheduleCalendarEventWithCode).
5. Missing RESCHEDULE permission (CREATE/CANCEL code) → NOT_PERMITTED.
6. Expired / revoked / invalid codes → respective error codes.
7. The rescheduled event is exactly `token.event` and still has `calendar_group_fk`
   set (still a grouped event after reschedule).
"""

import datetime
from unittest.mock import patch

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarManagementToken,
    EventManagementPermissions,
)
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    CalendarGroupEventInputData,
    CalendarGroupSlotSelectionInputData,
)
from organizations.models import Organization


# ---------------------------------------------------------------------------
# GraphQL mutation string
# ---------------------------------------------------------------------------

RESCHEDULE_GROUP_WITH_CODE = """
mutation RescheduleCalendarGroupEventWithCode($input: RescheduleGroupWithCodeInput!) {
    rescheduleCalendarGroupEventWithCode(input: $input) {
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
    return baker.make(Organization, name="Reschedule-Group-With-Code Test Org")


@pytest.fixture
def primary_calendar(organization):
    """The primary calendar for the group.  RESTRICTED: accepts_public_scheduling=False.

    Using a restricted calendar ensures the reschedule code alone authorizes the
    operation — public scheduling is disabled, the code must be the gate.
    """
    return baker.make(
        Calendar,
        organization=organization,
        name="Primary Calendar",
        external_id="primary-cal-reschedulegrp-test",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=False,
    )


@pytest.fixture
def secondary_calendar(organization):
    """A second calendar belonging to a non-primary slot."""
    return baker.make(
        Calendar,
        organization=organization,
        name="Room Calendar",
        external_id="room-cal-reschedulegrp-test",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.RESOURCE,
        manage_available_windows=True,
        accepts_public_scheduling=False,
    )


@pytest.fixture
def group(organization, primary_calendar, secondary_calendar):
    """A CalendarGroup with two slots: slot_a (primary_calendar) and slot_b (secondary_calendar)."""
    grp = baker.make(CalendarGroup, organization=organization, name="Test Group")
    slot_a = CalendarGroupSlot.objects.create(
        organization=organization,
        group=grp,
        name="Physicians",
        order=0,
        required_count=1,
    )
    slot_b = CalendarGroupSlot.objects.create(
        organization=organization,
        group=grp,
        name="Rooms",
        order=1,
        required_count=1,
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization,
        slot=slot_a,
        calendar=primary_calendar,
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization,
        slot=slot_b,
        calendar=secondary_calendar,
    )
    return grp


@pytest.fixture
def availability_windows(organization, primary_calendar, secondary_calendar):
    """Availability windows for both calendars covering both the original and the new slots.

    Original slot: 10:00-11:00 UTC
    New slot:      14:00-15:00 UTC
    Both fall inside 09:00-17:00 UTC.
    """
    windows = []
    for cal in (primary_calendar, secondary_calendar):
        windows.append(
            AvailableTime.objects.create(
                organization=organization,
                calendar=cal,
                start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
                end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
                timezone="UTC",
            )
        )
    return windows


@pytest.fixture
def grouped_event(organization, group, primary_calendar, secondary_calendar):
    """A grouped primary CalendarEvent with a linked non-primary BlockedTime.

    Constructed directly with baker/model calls (bypassing the group service
    create_event path) so that the test is independent of the RESTRICTED-calendar
    guard in can_perform_scheduling.  This mirrors how Phase 6a builds its test
    event — the reschedule mutation is what we are testing, not event creation.

    Structure created:
    - Primary CalendarEvent on primary_calendar, calendar_group_fk = group.
    - BlockedTime on secondary_calendar with the canonical external_id pattern.
    - One EventExternalAttendance (patient@example.com) on the primary event.
    """
    from calendar_integration.models import EventExternalAttendance, ExternalAttendee

    event = baker.make(
        CalendarEvent,
        organization=organization,
        calendar=primary_calendar,
        calendar_group=group,
        title="Original Group Title",
        description="Original description.",
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 11, 0),
        external_id="",
    )

    # Non-primary BlockedTime with the canonical external_id pattern.
    BlockedTime.objects.create(
        organization=organization,
        calendar=secondary_calendar,
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 11, 0),
        timezone="UTC",
        reason=f"Group booking: {event.title}",
        external_id=f"group-event-{event.id}-cal-{secondary_calendar.id}",
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
def group_reschedule_code(permission_service, organization, group, grouped_event):
    """A valid single-use GROUP RESCHEDULE code bound to ``grouped_event``."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_group_id=group.id,
        event_id=grouped_event.id,
    )
    return token, code


@pytest.fixture
def calendar_scoped_reschedule_code(
    permission_service, organization, primary_calendar, grouped_event
):
    """A RESCHEDULE code scoped to a single calendar only (no calendar_group)."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_id=primary_calendar.id,
        event_id=grouped_event.id,
    )
    return token, code


@pytest.fixture
def create_only_code(permission_service, organization, group):
    """A CREATE-only code — wrong permission for rescheduling."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=group.id,
    )
    return token, code


@pytest.fixture
def cancel_only_code(permission_service, organization, group, grouped_event):
    """A CANCEL-only code bound to the grouped event — wrong permission for rescheduling."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CANCEL],
        calendar_group_id=group.id,
        event_id=grouped_event.id,
    )
    return token, code


@pytest.fixture
def anon_client():
    """APIClient with no Authorization header."""
    return APIClient()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Original slot set by the ``grouped_event`` fixture (10:00-11:00 UTC on 2030-06-01).
ORIGINAL_START = datetime.datetime(2030, 6, 1, 10, 0, tzinfo=datetime.UTC)
ORIGINAL_END = datetime.datetime(2030, 6, 1, 11, 0, tzinfo=datetime.UTC)

# New slot for happy-path reschedule: 14:00-15:00 UTC (inside 09:00-17:00 window).
NEW_START = datetime.datetime(2030, 6, 1, 14, 0, tzinfo=datetime.UTC)
NEW_END = datetime.datetime(2030, 6, 1, 15, 0, tzinfo=datetime.UTC)

# Out-of-window slot: 22:00-23:00 UTC (outside the 09:00-17:00 window).
OOW_START = datetime.datetime(2030, 6, 1, 22, 0, tzinfo=datetime.UTC)
OOW_END = datetime.datetime(2030, 6, 1, 23, 0, tzinfo=datetime.UTC)


def _reschedule_group_input(code: str, **overrides) -> dict:
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


def _get_blocked_time(organization: Organization, event_id: int, calendar_id: int) -> BlockedTime:
    """Load the BlockedTime row linked to a grouped event for a specific calendar."""
    return BlockedTime.objects.filter_by_organization(organization.id).get(
        external_id=f"group-event-{event_id}-cal-{calendar_id}"
    )


# ---------------------------------------------------------------------------
# Scenario 1: Happy path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRescheduleGroupWithCodeHappyPath:
    """Scenario 1: Valid group-reschedule code + in-window slot → success.

    Verifies:
    - Primary CalendarEvent times are updated.
    - Non-primary BlockedTime times are updated.
    - Event id is preserved (same pk as the original grouped_event).
    - Title, description, external attendee are preserved.
    - Code is consumed.
    - Works on a RESTRICTED primary calendar.
    - The returned event still has calendar_group_fk set.
    """

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_happy_path_updates_primary_event_and_blocked_times(
        self,
        mock_rate_limiter,
        anon_client,
        group_reschedule_code,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
        grouped_event,
        availability_windows,  # noqa: ARG002 — seeds DB rows
    ):
        mock_rate_limiter.return_value = iter([None])
        token, code = group_reschedule_code

        data = post_graphql(
            anon_client,
            RESCHEDULE_GROUP_WITH_CODE,
            {"input": _reschedule_group_input(code)},
        )

        assert "errors" not in data or not data.get("errors"), data
        result = data["data"]["rescheduleCalendarGroupEventWithCode"]
        assert result["success"] is True, result
        assert result["errorCode"] is None
        assert result["event"] is not None

        # Event id is preserved (same pk, not a new event).
        returned_event_id = int(result["event"]["id"])
        assert returned_event_id == grouped_event.id

        # Code must be consumed.
        token.refresh_from_db()
        assert token.used_at is not None
        assert token.consumed_source_ip is not None

        # Primary event times must be updated.
        grouped_event.refresh_from_db()
        assert grouped_event.start_time_tz_unaware.replace(tzinfo=None) == NEW_START.replace(
            tzinfo=None
        )
        assert grouped_event.end_time_tz_unaware.replace(tzinfo=None) == NEW_END.replace(
            tzinfo=None
        )

        # Non-primary BlockedTime must also be updated.
        bt = _get_blocked_time(organization, grouped_event.id, secondary_calendar.id)
        assert bt.start_time_tz_unaware.replace(tzinfo=None) == NEW_START.replace(tzinfo=None)
        assert bt.end_time_tz_unaware.replace(tzinfo=None) == NEW_END.replace(tzinfo=None)

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_event_is_still_grouped_after_reschedule(
        self,
        mock_rate_limiter,
        anon_client,
        group_reschedule_code,
        organization,
        group,
        grouped_event,
        availability_windows,  # noqa: ARG002
    ):
        """The rescheduled event still has calendar_group_fk set."""
        mock_rate_limiter.return_value = iter([None])
        _token, code = group_reschedule_code

        data = post_graphql(
            anon_client,
            RESCHEDULE_GROUP_WITH_CODE,
            {"input": _reschedule_group_input(code)},
        )

        result = data["data"]["rescheduleCalendarGroupEventWithCode"]
        assert result["success"] is True, result

        grouped_event.refresh_from_db()
        assert grouped_event.calendar_group_fk_id == group.id

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_title_description_and_attendees_preserved(
        self,
        mock_rate_limiter,
        anon_client,
        group_reschedule_code,
        organization,
        group,
        grouped_event,
        availability_windows,  # noqa: ARG002
    ):
        """Title, description, and external attendee are preserved after reschedule."""
        mock_rate_limiter.return_value = iter([None])
        _token, code = group_reschedule_code

        data = post_graphql(
            anon_client,
            RESCHEDULE_GROUP_WITH_CODE,
            {"input": _reschedule_group_input(code)},
        )

        result = data["data"]["rescheduleCalendarGroupEventWithCode"]
        assert result["success"] is True, result

        # Title preserved in GraphQL response.
        assert result["event"]["title"] == "Original Group Title"

        # External attendee preserved in GraphQL response.
        external_attendances = result["event"]["externalAttendances"]
        assert len(external_attendances) == 1
        assert external_attendances[0]["externalAttendee"]["email"] == "patient@example.com"
        assert external_attendances[0]["externalAttendee"]["name"] == "Pat Patient"

        # DB: title and description unchanged.
        grouped_event.refresh_from_db()
        assert grouped_event.title == "Original Group Title"
        assert grouped_event.description == "Original description."

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_returned_event_is_the_bound_event(
        self,
        mock_rate_limiter,
        anon_client,
        group_reschedule_code,
        organization,
        group,
        grouped_event,
        availability_windows,  # noqa: ARG002
    ):
        """The rescheduled event is exactly the event the code was bound to."""
        mock_rate_limiter.return_value = iter([None])
        token, code = group_reschedule_code

        data = post_graphql(
            anon_client,
            RESCHEDULE_GROUP_WITH_CODE,
            {"input": _reschedule_group_input(code)},
        )

        result = data["data"]["rescheduleCalendarGroupEventWithCode"]
        assert result["success"] is True, result

        returned_event_id = int(result["event"]["id"])
        assert returned_event_id == grouped_event.id
        assert returned_event_id == token.event_fk_id


# ---------------------------------------------------------------------------
# Scenario 2: Replay
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRescheduleGroupWithCodeReplay:
    """Scenario 2: Replay with same code → ALREADY_USED, event not changed again."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_replay_returns_already_used(
        self,
        mock_rate_limiter,
        anon_client,
        group_reschedule_code,
        organization,
        group,
        grouped_event,
        availability_windows,  # noqa: ARG002
    ):
        mock_rate_limiter.return_value = iter([None])
        _token, code = group_reschedule_code
        input_data = _reschedule_group_input(code)

        # First call — must succeed.
        first = post_graphql(anon_client, RESCHEDULE_GROUP_WITH_CODE, {"input": input_data})
        assert first["data"]["rescheduleCalendarGroupEventWithCode"]["success"] is True

        # Second call — same code must return ALREADY_USED.
        second = post_graphql(anon_client, RESCHEDULE_GROUP_WITH_CODE, {"input": input_data})
        result = second["data"]["rescheduleCalendarGroupEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "ALREADY_USED"

        # Event times must remain as set by the first reschedule.
        grouped_event.refresh_from_db()
        assert grouped_event.start_time_tz_unaware.replace(tzinfo=None) == NEW_START.replace(
            tzinfo=None
        )


# ---------------------------------------------------------------------------
# Scenario 3: Out-of-window slot → SLOT_UNAVAILABLE, code NOT consumed
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRescheduleGroupWithCodeSlotUnavailable:
    """Scenario 3: New slot outside availability → SLOT_UNAVAILABLE, code NOT consumed."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_out_of_window_slot_does_not_consume_code(
        self,
        mock_rate_limiter,
        anon_client,
        group_reschedule_code,
        organization,
        group,
        grouped_event,
        availability_windows,  # noqa: ARG002 — window is 09:00-17:00 UTC
    ):
        mock_rate_limiter.return_value = iter([None])
        token, code = group_reschedule_code

        data = post_graphql(
            anon_client,
            RESCHEDULE_GROUP_WITH_CODE,
            {
                "input": _reschedule_group_input(
                    code,
                    startTime=OOW_START.isoformat(),
                    endTime=OOW_END.isoformat(),
                )
            },
        )

        result = data["data"]["rescheduleCalendarGroupEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "SLOT_UNAVAILABLE"
        assert result["errorMessage"] == "The requested time slot is not available."

        # Code must still be active.
        token.refresh_from_db()
        assert token.used_at is None

        # Event times must be unchanged.
        grouped_event.refresh_from_db()
        assert grouped_event.start_time_tz_unaware.replace(tzinfo=None) == ORIGINAL_START.replace(
            tzinfo=None
        )

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_after_failed_reschedule_code_can_succeed(
        self,
        mock_rate_limiter,
        anon_client,
        group_reschedule_code,
        organization,
        group,
        grouped_event,
        availability_windows,  # noqa: ARG002
    ):
        """After a SLOT_UNAVAILABLE failure the same code can succeed on a valid slot."""
        mock_rate_limiter.return_value = iter([None])
        token, code = group_reschedule_code

        # First: out-of-window → failure.
        fail = post_graphql(
            anon_client,
            RESCHEDULE_GROUP_WITH_CODE,
            {
                "input": _reschedule_group_input(
                    code,
                    startTime=OOW_START.isoformat(),
                    endTime=OOW_END.isoformat(),
                )
            },
        )
        assert (
            fail["data"]["rescheduleCalendarGroupEventWithCode"]["errorCode"] == "SLOT_UNAVAILABLE"
        )
        token.refresh_from_db()
        assert token.used_at is None

        # Second: in-window → success.
        data = post_graphql(
            anon_client,
            RESCHEDULE_GROUP_WITH_CODE,
            {"input": _reschedule_group_input(code)},
        )
        result = data["data"]["rescheduleCalendarGroupEventWithCode"]
        assert result["success"] is True, result
        token.refresh_from_db()
        assert token.used_at is not None


# ---------------------------------------------------------------------------
# Scenario 4: Calendar-scoped code → NOT_PERMITTED
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRescheduleGroupWithCodeCalendarScopedCode:
    """Scenario 4: Calendar-scoped reschedule code (no calendar_group) → NOT_PERMITTED."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_calendar_only_code_returns_not_permitted(
        self,
        mock_rate_limiter,
        anon_client,
        calendar_scoped_reschedule_code,
        organization,
        grouped_event,
    ):
        mock_rate_limiter.return_value = iter([None])
        _token, code = calendar_scoped_reschedule_code

        data = post_graphql(
            anon_client,
            RESCHEDULE_GROUP_WITH_CODE,
            {"input": _reschedule_group_input(code)},
        )

        result = data["data"]["rescheduleCalendarGroupEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "NOT_PERMITTED"

        # Event times must be unchanged.
        grouped_event.refresh_from_db()
        assert grouped_event.start_time_tz_unaware.replace(tzinfo=None) == ORIGINAL_START.replace(
            tzinfo=None
        )


# ---------------------------------------------------------------------------
# Scenario 5: Missing RESCHEDULE permission
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRescheduleGroupWithCodeWrongPermission:
    """Scenario 5: Code without RESCHEDULE permission → NOT_PERMITTED."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_create_code_returns_not_permitted(
        self,
        mock_rate_limiter,
        anon_client,
        create_only_code,
        organization,
        grouped_event,
    ):
        mock_rate_limiter.return_value = iter([None])
        _token, code = create_only_code

        data = post_graphql(
            anon_client,
            RESCHEDULE_GROUP_WITH_CODE,
            {"input": _reschedule_group_input(code)},
        )

        result = data["data"]["rescheduleCalendarGroupEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "NOT_PERMITTED"

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_cancel_code_returns_not_permitted(
        self,
        mock_rate_limiter,
        anon_client,
        cancel_only_code,
        organization,
        grouped_event,
    ):
        mock_rate_limiter.return_value = iter([None])
        _token, code = cancel_only_code

        data = post_graphql(
            anon_client,
            RESCHEDULE_GROUP_WITH_CODE,
            {"input": _reschedule_group_input(code)},
        )

        result = data["data"]["rescheduleCalendarGroupEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "NOT_PERMITTED"


# ---------------------------------------------------------------------------
# Scenario 6: Lifecycle rejections
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRescheduleGroupWithCodeLifecycleRejections:
    """Scenario 6: Expired / revoked / invalid codes → respective error codes."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_expired_code_returns_expired(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
        organization,
        group,
        grouped_event,
    ):
        mock_rate_limiter.return_value = iter([None])
        past = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
        _token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.RESCHEDULE],
            calendar_group_id=group.id,
            event_id=grouped_event.id,
            expires_at=past,
        )

        data = post_graphql(
            anon_client,
            RESCHEDULE_GROUP_WITH_CODE,
            {"input": _reschedule_group_input(code)},
        )

        result = data["data"]["rescheduleCalendarGroupEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "EXPIRED"

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_revoked_code_returns_revoked(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
        organization,
        group,
        grouped_event,
    ):
        mock_rate_limiter.return_value = iter([None])
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.RESCHEDULE],
            calendar_group_id=group.id,
            event_id=grouped_event.id,
        )
        permission_service.revoke_token(organization_id=organization.id, token_id=token.id)

        data = post_graphql(
            anon_client,
            RESCHEDULE_GROUP_WITH_CODE,
            {"input": _reschedule_group_input(code)},
        )

        result = data["data"]["rescheduleCalendarGroupEventWithCode"]
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
            RESCHEDULE_GROUP_WITH_CODE,
            {"input": _reschedule_group_input("aW52YWxpZGdyb3VwcmVzY2hlZHVsZQ==")},
        )

        result = data["data"]["rescheduleCalendarGroupEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_already_used_code_returns_already_used(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
        organization,
        group,
        grouped_event,
    ):
        mock_rate_limiter.return_value = iter([None])
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.RESCHEDULE],
            calendar_group_id=group.id,
            event_id=grouped_event.id,
        )
        CalendarManagementToken.objects.filter(id=token.id).update(
            used_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
        )

        data = post_graphql(
            anon_client,
            RESCHEDULE_GROUP_WITH_CODE,
            {"input": _reschedule_group_input(code)},
        )

        result = data["data"]["rescheduleCalendarGroupEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "ALREADY_USED"


# ---------------------------------------------------------------------------
# Non-UTC regression: BLOCKER fix — create and reschedule agree on timezone
# ---------------------------------------------------------------------------

# America/Recife is UTC-3 (no DST).
# Original slot: 12:00-13:00 UTC on 2030-06-01 → wall-clock 09:00-10:00 Recife.
# New slot:      15:00-16:00 UTC on 2030-06-01 → wall-clock 12:00-13:00 Recife.
RECIFE_ORIG_UTC_START = datetime.datetime(2030, 6, 1, 12, 0, tzinfo=datetime.UTC)
RECIFE_ORIG_UTC_END = datetime.datetime(2030, 6, 1, 13, 0, tzinfo=datetime.UTC)
RECIFE_NEW_UTC_START = datetime.datetime(2030, 6, 1, 15, 0, tzinfo=datetime.UTC)
RECIFE_NEW_UTC_END = datetime.datetime(2030, 6, 1, 16, 0, tzinfo=datetime.UTC)
RECIFE_TZ = "America/Recife"

# The full window that covers both slots (in Recife wall-clock / stored as naive local).
RECIFE_WINDOW_START = datetime.datetime(2030, 6, 1, 8, 0)
RECIFE_WINDOW_END = datetime.datetime(2030, 6, 1, 18, 0)


@pytest.mark.django_db
class TestRescheduleGroupWithCodeNonUTCTimezone:
    """Non-UTC regression: primary event and linked BlockedTimes derive the same
    UTC instant after a reschedule in a non-UTC zone.

    The test builds the grouped event via the real CalendarGroupService
    (``create_grouped_event``) so that the BlockedTime rows are written by the
    same fixed ``_create_non_primary_blocked_times`` path.  If the BLOCKER fix
    is reverted, the BlockedTime's ``start_time``/``end_time`` GeneratedField
    instants will disagree with the primary event's instants by 3 hours (the
    America/Recife offset).
    """

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_non_utc_reschedule_aligns_primary_and_blocked_times(
        self,
        mock_rate_limiter,
        anon_client,
    ):
        mock_rate_limiter.return_value = iter([None])

        # --- Build org + calendars ------------------------------------------------
        org = baker.make(Organization, name="Recife Org")

        # Primary calendar must have accepts_public_scheduling=True so that
        # CalendarService.create_event doesn't reject it without a user token.
        primary_cal = Calendar.objects.create(
            organization=org,
            name="Recife Primary",
            external_id="recife-primary",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=True,
            accepts_public_scheduling=True,
        )
        secondary_cal = Calendar.objects.create(
            organization=org,
            name="Recife Room",
            external_id="recife-room",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.RESOURCE,
            manage_available_windows=True,
            accepts_public_scheduling=False,
        )

        # --- Build group + availability -------------------------------------------
        grp = baker.make(CalendarGroup, organization=org, name="Recife Group", accepts_public_scheduling=True)
        slot_a = CalendarGroupSlot.objects.create(
            organization=org, group=grp, name="Physicians", order=0, required_count=1
        )
        slot_b = CalendarGroupSlot.objects.create(
            organization=org, group=grp, name="Rooms", order=1, required_count=1
        )
        CalendarGroupSlotMembership.objects.create(
            organization=org, slot=slot_a, calendar=primary_cal
        )
        CalendarGroupSlotMembership.objects.create(
            organization=org, slot=slot_b, calendar=secondary_cal
        )

        # Availability windows in Recife wall-clock covering both slots.
        for cal in (primary_cal, secondary_cal):
            AvailableTime.objects.create(
                organization=org,
                calendar=cal,
                start_time_tz_unaware=RECIFE_WINDOW_START,
                end_time_tz_unaware=RECIFE_WINDOW_END,
                timezone=RECIFE_TZ,
            )

        # --- Create the grouped event via the real service -------------------------
        cs = CalendarService()
        cs.initialize_without_provider(organization=org)
        group_svc = CalendarGroupService(calendar_service=cs)
        group_svc.initialize(organization=org)

        event = group_svc.create_grouped_event(
            CalendarGroupEventInputData(
                title="Recife Appointment",
                description="",
                start_time=RECIFE_ORIG_UTC_START,
                end_time=RECIFE_ORIG_UTC_END,
                timezone=RECIFE_TZ,
                group_id=grp.id,
                slot_selections=[
                    CalendarGroupSlotSelectionInputData(
                        slot_id=slot_a.id, calendar_ids=[primary_cal.id]
                    ),
                    CalendarGroupSlotSelectionInputData(
                        slot_id=slot_b.id, calendar_ids=[secondary_cal.id]
                    ),
                ],
            )
        )

        # --- Create RESCHEDULE code -----------------------------------------------
        perm_svc = CalendarPermissionService()
        token, code = perm_svc.create_booking_token(
            organization_id=org.id,
            permissions=[EventManagementPermissions.RESCHEDULE],
            calendar_group_id=grp.id,
            event_id=event.id,
        )

        # --- Reschedule via GraphQL mutation --------------------------------------
        data = post_graphql(
            anon_client,
            RESCHEDULE_GROUP_WITH_CODE,
            {
                "input": {
                    "code": code,
                    "startTime": RECIFE_NEW_UTC_START.isoformat(),
                    "endTime": RECIFE_NEW_UTC_END.isoformat(),
                    "timezone": RECIFE_TZ,
                }
            },
        )

        assert "errors" not in data or not data.get("errors"), data
        result = data["data"]["rescheduleCalendarGroupEventWithCode"]
        assert result["success"] is True, result

        # --- Assert instants agree ------------------------------------------------
        # Reload from DB to get the GeneratedField values.
        event.refresh_from_db()
        bt = BlockedTime.objects.filter_by_organization(org.id).get(
            external_id=f"group-event-{event.id}-cal-{secondary_cal.id}"
        )
        bt.refresh_from_db()

        # The GeneratedField ``start_time`` / ``end_time`` must be the same UTC
        # instant for both the primary CalendarEvent and the linked BlockedTime.
        assert event.start_time == bt.start_time, (
            f"Primary start_time {event.start_time} != BlockedTime start_time {bt.start_time}"
        )
        assert event.end_time == bt.end_time, (
            f"Primary end_time {event.end_time} != BlockedTime end_time {bt.end_time}"
        )

        # And those instants must equal the requested new UTC times.
        assert event.start_time == RECIFE_NEW_UTC_START
        assert event.end_time == RECIFE_NEW_UTC_END

        # Code must be consumed.
        token.refresh_from_db()
        assert token.used_at is not None
