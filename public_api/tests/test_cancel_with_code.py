"""Integration tests for cancelEventWithCode (Phase 6c).

All requests are unauthenticated (no Authorization header).  The cancel code
provides the org scope, the specific event scope, and the CANCEL permission.

Covers BOTH the calendar-bound (non-grouped) path and the calendar-group-bound
(grouped event) path via the SINGLE ``cancelEventWithCode`` mutation.

Scenario coverage:
1. Calendar cancel happy path — a RESTRICTED calendar, existing event, CANCEL code
   bound to it → event is gone, code consumed.
2. Group cancel happy path — a grouped event (primary CalendarEvent +
   CalendarEventGroupSelection rows + non-primary BlockedTime) → primary event
   gone (cascade removes selections), non-primary BlockedTimes deleted, code
   consumed.
3. Replay — same code again → ALREADY_USED (resolve_code fires before any delete
   attempt, so the mutation doesn't crash on a missing event).
4. Wrong permission — a CREATE/RESCHEDULE-only code → NOT_PERMITTED, event still
   exists.
5. Expired / revoked / invalid → respective error, event still exists.
6. Event-binding: event_id comes from the token, so only the exactly-bound event
   is cancelled.
"""

import datetime
from unittest.mock import patch

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarManagementToken,
    EventManagementPermissions,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from organizations.models import Organization


# ---------------------------------------------------------------------------
# GraphQL mutation string
# ---------------------------------------------------------------------------

CANCEL_WITH_CODE = """
mutation CancelEventWithCode($input: CancelWithCodeInput!) {
    cancelEventWithCode(input: $input) {
        success
        errorCode
        errorMessage
    }
}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def post_graphql(client: APIClient, query: str, variables: dict) -> dict:
    response = client.post(
        "/graphql/",
        data={"query": query, "variables": variables},
        format="json",
    )
    assert response.status_code == 200, response.content.decode()
    return response.json()


def _cancel_input(code: str) -> dict:
    return {"code": code}


# ---------------------------------------------------------------------------
# Fixtures — shared
# ---------------------------------------------------------------------------


@pytest.fixture
def organization():
    return baker.make(Organization, name="Cancel-With-Code Test Org")


@pytest.fixture
def permission_service():
    return CalendarPermissionService()


@pytest.fixture
def anon_client():
    """APIClient with no Authorization header."""
    return APIClient()


# ---------------------------------------------------------------------------
# Fixtures — single-calendar path
# ---------------------------------------------------------------------------


@pytest.fixture
def calendar(organization):
    """A RESTRICTED calendar (accepts_public_scheduling=False).

    The cancel code grants CANCEL permission so that can_perform_update returns
    True even though public scheduling is disabled.
    """
    return baker.make(
        Calendar,
        organization=organization,
        name="Test Calendar",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=False,
        accepts_public_scheduling=False,
    )


@pytest.fixture
def existing_event(organization, calendar):
    """An existing non-grouped event."""
    return baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        title="Appointment",
        description="A scheduled appointment.",
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 11, 0),
        external_id="",
        calendar_group=None,
    )


@pytest.fixture
def cancel_code(permission_service, organization, calendar, existing_event):
    """A valid single-use CANCEL code bound to ``existing_event``."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CANCEL],
        calendar_id=calendar.id,
        event_id=existing_event.id,
    )
    return token, code


@pytest.fixture
def reschedule_code(permission_service, organization, calendar, existing_event):
    """A RESCHEDULE-only code — wrong permission for cancellation."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.RESCHEDULE],
        calendar_id=calendar.id,
        event_id=existing_event.id,
    )
    return token, code


@pytest.fixture
def create_code(permission_service, organization, calendar):
    """A CREATE-only code — wrong permission for cancellation."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=calendar.id,
    )
    return token, code


# ---------------------------------------------------------------------------
# Fixtures — group path
# ---------------------------------------------------------------------------


@pytest.fixture
def primary_calendar(organization):
    """Primary calendar for the group event. RESTRICTED."""
    return baker.make(
        Calendar,
        organization=organization,
        name="Primary Calendar",
        external_id="primary-cal-cancel-test",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=False,
        accepts_public_scheduling=False,
    )


@pytest.fixture
def secondary_calendar(organization):
    """A non-primary calendar (room) that gets a BlockedTime."""
    return baker.make(
        Calendar,
        organization=organization,
        name="Room Calendar",
        external_id="room-cal-cancel-test",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.RESOURCE,
        manage_available_windows=False,
        accepts_public_scheduling=False,
    )


@pytest.fixture
def group(organization, primary_calendar, secondary_calendar):
    """A CalendarGroup with two slots: Physicians (primary) and Rooms (secondary)."""
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
def grouped_event(organization, group, primary_calendar, secondary_calendar):
    """A grouped primary CalendarEvent with a CalendarEventGroupSelection and a linked BlockedTime.

    Built directly with baker/model calls (bypassing the group service) so the
    test is independent of the RESTRICTED-calendar guard in can_perform_scheduling.

    Structure:
    - Primary CalendarEvent on primary_calendar, calendar_group_fk = group.
    - CalendarEventGroupSelection rows for both calendars.
    - BlockedTime on secondary_calendar with the canonical external_id pattern.
    """
    event = baker.make(
        CalendarEvent,
        organization=organization,
        calendar=primary_calendar,
        calendar_group=group,
        title="Group Appointment",
        description="A grouped appointment.",
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 11, 0),
        external_id="",
    )

    # Group selections (would normally be created by create_grouped_event).
    slot_a = CalendarGroupSlot.objects.filter_by_organization(organization.id).get(
        group=group, name="Physicians"
    )
    slot_b = CalendarGroupSlot.objects.filter_by_organization(organization.id).get(
        group=group, name="Rooms"
    )
    CalendarEventGroupSelection.objects.create(
        organization=organization,
        event=event,
        slot=slot_a,
        calendar=primary_calendar,
    )
    CalendarEventGroupSelection.objects.create(
        organization=organization,
        event=event,
        slot=slot_b,
        calendar=secondary_calendar,
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

    return event


@pytest.fixture
def group_cancel_code(permission_service, organization, group, grouped_event):
    """A valid single-use GROUP CANCEL code bound to ``grouped_event``."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CANCEL],
        calendar_group_id=group.id,
        event_id=grouped_event.id,
    )
    return token, code


# ---------------------------------------------------------------------------
# Scenario 1: Calendar cancel happy path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCancelEventWithCodeCalendarHappyPath:
    """Scenario 1: Valid CANCEL code on a non-grouped event → success."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_happy_path_cancels_event_and_consumes_code(
        self,
        mock_rate_limiter,
        anon_client,
        cancel_code,
        organization,
        existing_event,
    ):
        mock_rate_limiter.return_value = iter([None])
        token, code = cancel_code
        event_id = existing_event.id

        data = post_graphql(anon_client, CANCEL_WITH_CODE, {"input": _cancel_input(code)})

        assert "errors" not in data or not data.get("errors"), data
        result = data["data"]["cancelEventWithCode"]
        assert result["success"] is True, result
        assert result["errorCode"] is None

        # The event must be deleted.
        assert not CalendarEvent.objects.filter(id=event_id).exists()

        # The token must be gone: the event FK has on_delete=CASCADE so deleting
        # the event also removes the token.  Non-existence proves the cancel was
        # atomic (consume succeeded, then event+token were removed together).
        assert not CalendarManagementToken.original_manager.filter(pk=token.pk).exists()

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_cancels_exactly_the_bound_event(
        self,
        mock_rate_limiter,
        anon_client,
        cancel_code,
        organization,
        calendar,
    ):
        """The cancelled event is exactly the event the code was bound to."""
        mock_rate_limiter.return_value = iter([None])
        token, code = cancel_code

        # Create a second event to verify it is NOT touched.
        other_event = baker.make(
            CalendarEvent,
            organization=organization,
            calendar=calendar,
            title="Other Event",
            timezone="UTC",
            start_time_tz_unaware=datetime.datetime(2030, 6, 2, 10, 0),
            end_time_tz_unaware=datetime.datetime(2030, 6, 2, 11, 0),
            external_id="other-event-cancel-001",
        )

        data = post_graphql(anon_client, CANCEL_WITH_CODE, {"input": _cancel_input(code)})

        result = data["data"]["cancelEventWithCode"]
        assert result["success"] is True, result

        # Bound event is gone.
        assert not CalendarEvent.objects.filter(id=token.event_fk_id).exists()
        # Other event is untouched.
        assert CalendarEvent.objects.filter(id=other_event.id).exists()


# ---------------------------------------------------------------------------
# Scenario 2: Group cancel happy path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCancelEventWithCodeGroupHappyPath:
    """Scenario 2: Valid GROUP CANCEL code → primary event gone, selections cascaded,
    non-primary BlockedTimes deleted, code consumed.
    """

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_happy_path_cancels_grouped_event(
        self,
        mock_rate_limiter,
        anon_client,
        group_cancel_code,
        organization,
        group,
        primary_calendar,
        secondary_calendar,
        grouped_event,
    ):
        mock_rate_limiter.return_value = iter([None])
        token, code = group_cancel_code
        event_id = grouped_event.id

        data = post_graphql(anon_client, CANCEL_WITH_CODE, {"input": _cancel_input(code)})

        assert "errors" not in data or not data.get("errors"), data
        result = data["data"]["cancelEventWithCode"]
        assert result["success"] is True, result
        assert result["errorCode"] is None

        # Primary CalendarEvent must be deleted.
        assert not CalendarEvent.objects.filter(id=event_id).exists()

        # CalendarEventGroupSelection rows must be gone (FK cascade from event delete).
        assert not CalendarEventGroupSelection.objects.filter(event_fk_id=event_id).exists()

        # Non-primary BlockedTimes with the canonical external_id prefix must be deleted.
        assert not BlockedTime.objects.filter(
            external_id__startswith=f"group-event-{event_id}-cal-"
        ).exists()

        # Token must be gone: event FK on_delete=CASCADE removes it when the event is deleted.
        assert not CalendarManagementToken.original_manager.filter(pk=token.pk).exists()

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_non_primary_blocked_times_deleted_not_orphaned(
        self,
        mock_rate_limiter,
        anon_client,
        group_cancel_code,
        organization,
        secondary_calendar,
        grouped_event,
    ):
        """Explicit assertion: after a group cancel, zero BlockedTimes with the
        group-event prefix survive."""
        mock_rate_limiter.return_value = iter([None])
        _token, code = group_cancel_code
        event_id = grouped_event.id

        # Confirm the BlockedTime exists before cancel.
        assert BlockedTime.objects.filter(
            external_id=f"group-event-{event_id}-cal-{secondary_calendar.id}"
        ).exists()

        post_graphql(anon_client, CANCEL_WITH_CODE, {"input": _cancel_input(code)})

        # After cancel, no BlockedTime with that prefix should remain.
        assert not BlockedTime.objects.filter(
            external_id__startswith=f"group-event-{event_id}-cal-"
        ).exists()


# ---------------------------------------------------------------------------
# Scenario 3: Replay → INVALID_CODE (token is cascade-deleted with the event)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCancelEventWithCodeReplay:
    """Scenario 3: Replay with same code → INVALID_CODE.

    The CalendarManagementToken.event FK has on_delete=CASCADE.  When the
    primary event is deleted the token row is also deleted.  A second attempt
    with the same code therefore cannot find any token and resolve_code raises
    InvalidTokenError, which the mutation surfaces as INVALID_CODE.

    The single-use guarantee is still enforced: the code CANNOT be replayed
    (the token is permanently gone).
    """

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_replay_returns_invalid_code_after_cancel(
        self,
        mock_rate_limiter,
        anon_client,
        cancel_code,
        organization,
        existing_event,
    ):
        mock_rate_limiter.return_value = iter([None])
        _token, code = cancel_code
        input_data = _cancel_input(code)

        # First call — must succeed (event is deleted, token is cascade-deleted).
        first = post_graphql(anon_client, CANCEL_WITH_CODE, {"input": input_data})
        assert first["data"]["cancelEventWithCode"]["success"] is True

        # Second call — token row is gone (cascade-deleted with the event).
        # resolve_code raises InvalidTokenError → INVALID_CODE.
        second = post_graphql(anon_client, CANCEL_WITH_CODE, {"input": input_data})
        result = second["data"]["cancelEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_group_cancel_replay_returns_invalid_code(
        self,
        mock_rate_limiter,
        anon_client,
        group_cancel_code,
        organization,
        grouped_event,
    ):
        """Same replay protection for group-scoped cancel codes."""
        mock_rate_limiter.return_value = iter([None])
        _token, code = group_cancel_code
        input_data = _cancel_input(code)

        # First call — must succeed.
        first = post_graphql(anon_client, CANCEL_WITH_CODE, {"input": input_data})
        assert first["data"]["cancelEventWithCode"]["success"] is True

        # Second call — token cascade-deleted with the event; must return INVALID_CODE.
        second = post_graphql(anon_client, CANCEL_WITH_CODE, {"input": input_data})
        result = second["data"]["cancelEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"


# ---------------------------------------------------------------------------
# Scenario 4: Wrong permission → NOT_PERMITTED, event still exists
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCancelEventWithCodeWrongPermission:
    """Scenario 4: Code without CANCEL permission → NOT_PERMITTED."""

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_reschedule_only_code_returns_not_permitted(
        self,
        mock_rate_limiter,
        anon_client,
        reschedule_code,
        existing_event,
    ):
        mock_rate_limiter.return_value = iter([None])
        _token, code = reschedule_code

        data = post_graphql(anon_client, CANCEL_WITH_CODE, {"input": _cancel_input(code)})

        result = data["data"]["cancelEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "NOT_PERMITTED"

        # Event must still exist.
        assert CalendarEvent.objects.filter(id=existing_event.id).exists()

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_create_only_code_returns_not_permitted(
        self,
        mock_rate_limiter,
        anon_client,
        create_code,
        existing_event,
    ):
        mock_rate_limiter.return_value = iter([None])
        _token, code = create_code

        data = post_graphql(anon_client, CANCEL_WITH_CODE, {"input": _cancel_input(code)})

        result = data["data"]["cancelEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "NOT_PERMITTED"

        # Event must still exist.
        assert CalendarEvent.objects.filter(id=existing_event.id).exists()


# ---------------------------------------------------------------------------
# Scenario 5: Lifecycle rejections — expired / revoked / invalid / already-used
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCancelEventWithCodeLifecycleRejections:
    """Scenario 5: Expired / revoked / invalid codes are rejected with the correct error."""

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
            permissions=[EventManagementPermissions.CANCEL],
            calendar_id=calendar.id,
            event_id=existing_event.id,
            expires_at=past,
        )

        data = post_graphql(anon_client, CANCEL_WITH_CODE, {"input": _cancel_input(code)})

        result = data["data"]["cancelEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "EXPIRED"

        # Event must still exist.
        assert CalendarEvent.objects.filter(id=existing_event.id).exists()

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
            permissions=[EventManagementPermissions.CANCEL],
            calendar_id=calendar.id,
            event_id=existing_event.id,
        )
        permission_service.revoke_token(organization_id=organization.id, token_id=token.id)

        data = post_graphql(anon_client, CANCEL_WITH_CODE, {"input": _cancel_input(code)})

        result = data["data"]["cancelEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "REVOKED"

        # Event must still exist.
        assert CalendarEvent.objects.filter(id=existing_event.id).exists()

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_invalid_code_returns_invalid_code(
        self,
        mock_rate_limiter,
        anon_client,
    ):
        mock_rate_limiter.return_value = iter([None])

        data = post_graphql(
            anon_client,
            CANCEL_WITH_CODE,
            {"input": _cancel_input("aW52YWxpZGNhbmNlbGNvZGU=")},  # base64 junk
        )

        result = data["data"]["cancelEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "INVALID_CODE"

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_already_used_code_returns_already_used(
        self,
        mock_rate_limiter,
        anon_client,
        permission_service,
        organization,
        calendar,
        existing_event,
    ):
        """A code already marked as used → ALREADY_USED (event not touched)."""
        mock_rate_limiter.return_value = iter([None])
        token, code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CANCEL],
            calendar_id=calendar.id,
            event_id=existing_event.id,
        )
        CalendarManagementToken.objects.filter(id=token.id).update(
            used_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
        )

        data = post_graphql(anon_client, CANCEL_WITH_CODE, {"input": _cancel_input(code)})

        result = data["data"]["cancelEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "ALREADY_USED"

        # Event must still exist.
        assert CalendarEvent.objects.filter(id=existing_event.id).exists()
