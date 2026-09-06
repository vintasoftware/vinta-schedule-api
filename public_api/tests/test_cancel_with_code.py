"""Integration tests for cancelEventWithCode.

All requests are unauthenticated (no Authorization header).  The cancel code
provides the org scope, the specific event scope, and the CANCEL permission.

Covers BOTH the calendar-bound (non-appointment-type) path and the appointment-type-bound
(appointment-type event) path via the SINGLE ``cancelEventWithCode`` mutation.

Scenario coverage:
1. Calendar cancel happy path — a RESTRICTED calendar, existing event, CANCEL code
   bound to it → event is gone, code consumed.
2. Appointment type cancel happy path — an appointment-type event (primary CalendarEvent +
   CalendarEventAppointmentTypeSelection rows + non-primary BlockedTime) → primary event
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
    AppointmentType,
    AppointmentTypeSlot,
    AppointmentTypeSlotMembership,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarEventAppointmentTypeSelection,
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
    """An existing non-appointment-type event."""
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
        appointment_type=None,
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
# Fixtures — appointment type path
# ---------------------------------------------------------------------------


@pytest.fixture
def primary_calendar(organization):
    """Primary calendar for the appointment type event. RESTRICTED."""
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
def appointment_type(organization, primary_calendar, secondary_calendar):
    """An AppointmentType with two slots: Physicians (primary) and Rooms (secondary)."""
    grp = baker.make(AppointmentType, organization=organization, name="Test AppointmentType")
    slot_a = AppointmentTypeSlot.objects.create(
        organization=organization,
        appointment_type=grp,
        name="Physicians",
        order=0,
        required_count=1,
    )
    slot_b = AppointmentTypeSlot.objects.create(
        organization=organization,
        appointment_type=grp,
        name="Rooms",
        order=1,
        required_count=1,
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization,
        slot=slot_a,
        calendar=primary_calendar,
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization,
        slot=slot_b,
        calendar=secondary_calendar,
    )
    return grp


@pytest.fixture
def appointment_type_event(organization, appointment_type, primary_calendar, secondary_calendar):
    """An appointment-type primary CalendarEvent with a CalendarEventAppointmentTypeSelection and a linked BlockedTime.

    Built directly with baker/model calls (bypassing the appointment type service) so the
    test is independent of the RESTRICTED-calendar guard in can_perform_scheduling.

    Structure:
    - Primary CalendarEvent on primary_calendar, appointment_type_fk = appointment type.
    - CalendarEventAppointmentTypeSelection rows for both calendars.
    - BlockedTime on secondary_calendar with the canonical external_id pattern.
    """
    event = baker.make(
        CalendarEvent,
        organization=organization,
        calendar=primary_calendar,
        appointment_type=appointment_type,
        title="AppointmentType Appointment",
        description="An appointment type appointment.",
        timezone="UTC",
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 10, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 11, 0),
        external_id="",
    )

    # Appointment type selections (would normally be created by create_appointment_type_event).
    slot_a = AppointmentTypeSlot.objects.filter_by_organization(organization.id).get(
        appointment_type=appointment_type, name="Physicians"
    )
    slot_b = AppointmentTypeSlot.objects.filter_by_organization(organization.id).get(
        appointment_type=appointment_type, name="Rooms"
    )
    CalendarEventAppointmentTypeSelection.objects.create(
        organization=organization,
        event=event,
        slot=slot_a,
        calendar=primary_calendar,
    )
    CalendarEventAppointmentTypeSelection.objects.create(
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
        reason=f"AppointmentType booking: {event.title}",
        external_id=f"appointment-type-event-{event.id}-cal-{secondary_calendar.id}",
    )

    return event


@pytest.fixture
def appointment_type_cancel_code(
    permission_service, organization, appointment_type, appointment_type_event
):
    """A valid single-use APPOINTMENT_TYPE CANCEL code bound to ``appointment_type_event``."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CANCEL],
        appointment_type_id=appointment_type.id,
        event_id=appointment_type_event.id,
    )
    return token, code


# ---------------------------------------------------------------------------
# Scenario 1: Calendar cancel happy path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCancelEventWithCodeCalendarHappyPath:
    """Scenario 1: Valid CANCEL code on a non-appointment-type event → success."""

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
        assert not CalendarEvent.original_manager.filter(id=event_id).exists()

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
        assert not CalendarEvent.original_manager.filter(id=token.event_fk_id).exists()
        # Other event is untouched.
        assert CalendarEvent.original_manager.filter(id=other_event.id).exists()


# ---------------------------------------------------------------------------
# Scenario 2: Appointment type cancel happy path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCancelEventWithCodeAppointmentTypeHappyPath:
    """Scenario 2: Valid APPOINTMENT_TYPE CANCEL code → primary event gone, selections cascaded,
    non-primary BlockedTimes deleted, code consumed.
    """

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_happy_path_cancels_appointment_type_event(
        self,
        mock_rate_limiter,
        anon_client,
        appointment_type_cancel_code,
        organization,
        appointment_type,
        primary_calendar,
        secondary_calendar,
        appointment_type_event,
    ):
        mock_rate_limiter.return_value = iter([None])
        token, code = appointment_type_cancel_code
        event_id = appointment_type_event.id

        data = post_graphql(anon_client, CANCEL_WITH_CODE, {"input": _cancel_input(code)})

        assert "errors" not in data or not data.get("errors"), data
        result = data["data"]["cancelEventWithCode"]
        assert result["success"] is True, result
        assert result["errorCode"] is None

        # Primary CalendarEvent must be deleted.
        assert not CalendarEvent.original_manager.filter(id=event_id).exists()

        # CalendarEventAppointmentTypeSelection rows must be gone (FK cascade from event delete).
        assert not CalendarEventAppointmentTypeSelection.original_manager.filter(
            event_fk_id=event_id
        ).exists()

        # Non-primary BlockedTimes with the canonical external_id prefix must be deleted.
        assert not BlockedTime.original_manager.filter(
            external_id__startswith=f"appointment-type-event-{event_id}-cal-"
        ).exists()

        # Token must be gone: event FK on_delete=CASCADE removes it when the event is deleted.
        assert not CalendarManagementToken.original_manager.filter(pk=token.pk).exists()

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_non_primary_blocked_times_deleted_not_orphaned(
        self,
        mock_rate_limiter,
        anon_client,
        appointment_type_cancel_code,
        organization,
        secondary_calendar,
        appointment_type_event,
    ):
        """Explicit assertion: after an appointment type cancel, zero BlockedTimes with the
        appointment-type-event prefix survive."""
        mock_rate_limiter.return_value = iter([None])
        _token, code = appointment_type_cancel_code
        event_id = appointment_type_event.id

        # Confirm the BlockedTime exists before cancel.
        assert BlockedTime.original_manager.filter(
            external_id=f"appointment-type-event-{event_id}-cal-{secondary_calendar.id}"
        ).exists()

        post_graphql(anon_client, CANCEL_WITH_CODE, {"input": _cancel_input(code)})

        # After cancel, no BlockedTime with that prefix should remain.
        assert not BlockedTime.original_manager.filter(
            external_id__startswith=f"appointment-type-event-{event_id}-cal-"
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
    def test_appointment_type_cancel_replay_returns_invalid_code(
        self,
        mock_rate_limiter,
        anon_client,
        appointment_type_cancel_code,
        organization,
        appointment_type_event,
    ):
        """Same replay protection for appointment-type-scoped cancel codes."""
        mock_rate_limiter.return_value = iter([None])
        _token, code = appointment_type_cancel_code
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
        assert CalendarEvent.original_manager.filter(id=existing_event.id).exists()

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
        assert CalendarEvent.original_manager.filter(id=existing_event.id).exists()


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
        assert CalendarEvent.original_manager.filter(id=existing_event.id).exists()

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
        assert CalendarEvent.original_manager.filter(id=existing_event.id).exists()

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
        CalendarManagementToken.original_manager.filter(id=token.id).update(
            used_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
        )

        data = post_graphql(anon_client, CANCEL_WITH_CODE, {"input": _cancel_input(code)})

        result = data["data"]["cancelEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "ALREADY_USED"

        # Event must still exist.
        assert CalendarEvent.original_manager.filter(id=existing_event.id).exists()


# ---------------------------------------------------------------------------
# Scenario 6: Atomicity — consume rolls back when delete fails
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCancelEventWithCodeAtomicity:
    """Scenario 6: consume rolls back on delete failure.

    Proves the single-use-on-success guarantee: if the delete step raises,
    the entire transaction.atomic() block (including consume_code) rolls back
    so the token remains live and available for a genuine future cancel.
    """

    @patch("public_api.extensions.OrganizationRateLimiter.on_execute")
    def test_delete_failure_leaves_code_live_and_event_intact(
        self,
        mock_rate_limiter,
        anon_client,
        cancel_code,
        organization,
        existing_event,
    ):
        """Patching CalendarService.delete_event to raise RuntimeError proves
        that the whole atomic block rolls back: the code is NOT consumed and
        the event still exists after the failed mutation call."""
        mock_rate_limiter.return_value = iter([None])
        token, code = cancel_code

        with patch(
            "calendar_integration.services.calendar_service.CalendarService.delete_event",
            side_effect=RuntimeError("Simulated delete failure"),
        ):
            data = post_graphql(anon_client, CANCEL_WITH_CODE, {"input": _cancel_input(code)})

        # (a) The mutation must NOT succeed: either GraphQL surfaces the uncaught
        # RuntimeError as a top-level error (data is None) or the mutation returns
        # success=False.  Either way the caller does not see a successful cancel.
        cancel_data = (data.get("data") or {}).get("cancelEventWithCode")
        if cancel_data is not None:
            assert cancel_data["success"] is False, cancel_data
        else:
            assert data.get("errors"), "Expected GraphQL errors for unhandled exception"

        # (b) The token must still exist and must NOT be marked as used.
        refreshed = CalendarManagementToken.original_manager.filter(pk=token.pk).first()
        assert refreshed is not None, "Token was unexpectedly deleted (consume was not rolled back)"
        assert refreshed.used_at is None, "Token was consumed despite the delete failure"

        # (c) The event must still exist.
        assert CalendarEvent.original_manager.filter(id=existing_event.id).exists(), (
            "Event was deleted despite the delete failure rolling back"
        )
