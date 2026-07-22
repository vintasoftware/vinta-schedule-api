"""Integration tests for booking-time policy enforcement at the GraphQL layer.

Coverage:
- ``scheduleEvent`` (authenticated single-calendar):
  - Lead-time / horizon / buffer violations → GraphQL error with explanatory message.
  - Compliant booking → succeeds (no regression).
  - No policy → write unchanged (off-state).

- ``createCalendarEventWithCode`` (code-gated single-calendar):
  - Policy violation → SLOT_UNAVAILABLE with an explanatory message and code NOT consumed.
  - Compliant booking → succeeds.
  - No policy → write unchanged (off-state).
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import patch

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.factories import create_booking_policy
from calendar_integration.models import (
    AvailableTime,
    Calendar,
    CalendarEvent,
    CalendarManagementToken,
    CalendarOwnership,
    EventManagementPermissions,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from organizations.models import Organization, OrganizationMembership
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


# ---------------------------------------------------------------------------
# GraphQL strings
# ---------------------------------------------------------------------------

_SCHEDULE_EVENT = """
mutation ScheduleEvent($input: ScheduleEventInput!) {
    scheduleEvent(input: $input) {
        id
        title
    }
}
"""

_CREATE_WITH_CODE = """
mutation CreateCalendarEventWithCode($input: CreateEventWithCodeInput!) {
    createCalendarEventWithCode(input: $input) {
        success
        errorCode
        errorMessage
        event { id }
    }
}
"""


# ---------------------------------------------------------------------------
# Shared dates
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2030, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
_START = datetime.datetime(2030, 6, 1, 10, 0, 0, tzinfo=datetime.UTC)
_END = datetime.datetime(2030, 6, 1, 11, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Fixtures / helpers (schedule_event path)
# ---------------------------------------------------------------------------


def _org() -> Organization:
    unique = uuid.uuid4().hex[:6]
    return Organization.objects.create(name=f"Enforce Org {unique}", should_sync_rooms=False)


def _owner_calendar(org: Organization) -> tuple:
    """Return (owner_user, membership, calendar) — personal managed calendar with ownership."""
    from django.contrib.auth import get_user_model

    user_model = get_user_model()
    unique = uuid.uuid4().hex[:6]
    user = user_model.objects.create_user(email=f"owner_{unique}@example.com", password="pw")
    membership = OrganizationMembership.objects.create(user=user, organization=org, is_active=True)
    cal = Calendar.objects.create(
        organization=org,
        name=f"enforce-cal-{unique}",
        external_id=f"enforce-ext-{unique}",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=False,
    )
    CalendarOwnership.objects.create(organization=org, calendar=cal, membership_user_id=user.id)
    # Seed an availability window covering the test slot.
    AvailableTime.objects.create(
        organization=org,
        calendar=cal,
        start_time_tz_unaware=_START - datetime.timedelta(hours=2),
        end_time_tz_unaware=_END + datetime.timedelta(hours=2),
        timezone="UTC",
    )
    return user, membership, cal


def _scoped_system_user(org, membership):
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=f"enforce_su_{uuid.uuid4().hex[:6]}",
        organization=org,
        scoped_to_membership=membership,
    )
    baker.make(
        ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR_EVENT
    )
    return system_user, token, auth_service


def _schedule_input(org: Organization, cal: Calendar, **overrides) -> dict:
    base = {
        "organizationId": org.id,
        "calendarId": cal.id,
        "startTime": _START.isoformat(),
        "endTime": _END.isoformat(),
        "timezone": "UTC",
        "title": "Test Booking",
    }
    base.update(overrides)
    return base


def _post_authenticated(system_user, token, auth_service, variables: dict) -> dict:
    client = APIClient()
    from di_core.containers import container

    assert container is not None  # noqa: S101
    with container.public_api_auth_service.override(auth_service):
        response = client.post(
            "/graphql/",
            data={"query": _SCHEDULE_EVENT, "variables": variables},
            format="json",
            headers={"authorization": f"Bearer {system_user.id}:{token}"},
        )
    assert response.status_code == 200
    return response.json()


# ---------------------------------------------------------------------------
# Fixtures / helpers (code-gated path)
# ---------------------------------------------------------------------------


def _code_calendar(org: Organization) -> Calendar:
    unique = uuid.uuid4().hex[:6]
    cal = Calendar.objects.create(
        organization=org,
        name=f"code-enforce-cal-{unique}",
        external_id=f"code-enforce-{unique}",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=False,
    )
    # Seed availability.
    AvailableTime.objects.create(
        organization=org,
        calendar=cal,
        start_time_tz_unaware=_START - datetime.timedelta(hours=2),
        end_time_tz_unaware=_END + datetime.timedelta(hours=2),
        timezone="UTC",
    )
    return cal


def _booking_code(org: Organization, cal: Calendar) -> tuple[CalendarManagementToken, str]:
    svc = CalendarPermissionService()
    token, code = svc.create_booking_token(
        organization_id=org.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_id=cal.id,
    )
    return token, code


def _code_input(code: str, **overrides) -> dict:
    base = {
        "code": code,
        "title": "Code Booking",
        "description": "",
        "startTime": _START.isoformat(),
        "endTime": _END.isoformat(),
        "timezone": "UTC",
        "externalAttendee": {"email": "patient@example.com", "name": "Pat"},
    }
    base.update(overrides)
    return base


@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
def _post_code_gated(mock_rl, variables: dict) -> dict:
    mock_rl.return_value = iter([None])
    client = APIClient()
    response = client.post(
        "/graphql/",
        data={"query": _CREATE_WITH_CODE, "variables": variables},
        format="json",
    )
    assert response.status_code == 200
    return response.json()


# ---------------------------------------------------------------------------
# Tests — scheduleEvent (authenticated single-calendar path)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestScheduleEventPolicyEnforcement:
    """Policy enforcement via the authenticated ``scheduleEvent`` mutation."""

    def test_no_policy_schedules_successfully(self):
        """Off-state: no BookingPolicy → write behaves exactly as before."""
        org = _org()
        _user, membership, cal = _owner_calendar(org)
        system_user, token, auth_service = _scoped_system_user(org, membership)

        data = _post_authenticated(
            system_user, token, auth_service, {"input": _schedule_input(org, cal)}
        )
        assert "errors" not in data or not data.get("errors"), data
        result = data["data"]["scheduleEvent"]
        assert result is not None and result["title"] == "Test Booking"

    def test_lead_time_violation_returns_graphql_error(self):
        """Lead-time violation → GraphQL error with policy message, no event created."""
        org = _org()
        _user, membership, cal = _owner_calendar(org)
        system_user, token, auth_service = _scoped_system_user(org, membership)

        # Policy: lead = gap + 1h (so booking at _START is too soon relative to _NOW).
        lead_s = int((_START - _NOW).total_seconds()) + 3600
        create_booking_policy(calendar=cal, lead_time_seconds=lead_s)

        with patch("calendar_integration.services.calendar_service._tz") as mock_tz:
            mock_tz.now.return_value = _NOW
            data = _post_authenticated(
                system_user, token, auth_service, {"input": _schedule_input(org, cal)}
            )

        assert data.get("errors"), "Expected a GraphQL error for lead-time violation"
        error_msg = data["errors"][0]["message"].lower()
        assert "booking policy" in error_msg or "not available" in error_msg
        assert (
            not CalendarEvent.objects.filter_by_organization(org.id)
            .filter(calendar_fk_id=cal.id)
            .exists()
        ), "No event should be persisted on violation"

    def test_horizon_violation_returns_graphql_error(self):
        """Max-horizon violation → GraphQL error, no event created."""
        org = _org()
        _user, membership, cal = _owner_calendar(org)
        system_user, token, auth_service = _scoped_system_user(org, membership)

        # Policy: horizon shorter than gap to _START.
        horizon_s = int((_START - _NOW).total_seconds()) - 3600
        create_booking_policy(calendar=cal, max_horizon_seconds=horizon_s)

        with patch("calendar_integration.services.calendar_service._tz") as mock_tz:
            mock_tz.now.return_value = _NOW
            data = _post_authenticated(
                system_user, token, auth_service, {"input": _schedule_input(org, cal)}
            )

        assert data.get("errors"), "Expected a GraphQL error for horizon violation"
        assert (
            not CalendarEvent.objects.filter_by_organization(org.id)
            .filter(calendar_fk_id=cal.id)
            .exists()
        )

    def test_buffer_violation_returns_graphql_error(self):
        """Buffer-envelope violation → GraphQL error, no event created."""
        org = _org()
        _user, membership, cal = _owner_calendar(org)
        system_user, token, auth_service = _scoped_system_user(org, membership)

        # Existing event ending at 09:00; buffer_after = 3601s → dead zone [09:00, 10:00:01).
        CalendarEvent.objects.create(
            organization=org,
            calendar_fk=cal,
            title="Busy",
            description="",
            external_id=f"busy-{uuid.uuid4().hex[:6]}",
            start_time_tz_unaware=datetime.datetime(2030, 6, 1, 8, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )
        create_booking_policy(calendar=cal, buffer_after_seconds=3601)

        with patch("calendar_integration.services.calendar_service._tz") as mock_tz:
            mock_tz.now.return_value = _NOW
            data = _post_authenticated(
                system_user, token, auth_service, {"input": _schedule_input(org, cal)}
            )

        assert data.get("errors"), "Expected a GraphQL error for buffer violation"
        # The existing busy event + the new event (if it were created) - only the pre-existing one.
        events = list(
            CalendarEvent.objects.filter_by_organization(org.id).filter(
                calendar_fk_id=cal.id, title="Test Booking"
            )
        )
        assert len(events) == 0, "New event must NOT be persisted on buffer violation"

    def test_compliant_booking_succeeds(self):
        """A booking that satisfies all policy rules (sufficient lead time) succeeds."""
        org = _org()
        _user, membership, cal = _owner_calendar(org)
        system_user, token, auth_service = _scoped_system_user(org, membership)

        # Policy: lead = 0 (no constraint), no horizon, no buffer.
        create_booking_policy(calendar=cal, lead_time_seconds=0)

        data = _post_authenticated(
            system_user, token, auth_service, {"input": _schedule_input(org, cal)}
        )
        assert "errors" not in data or not data.get("errors"), data
        result = data["data"]["scheduleEvent"]
        assert result is not None


# ---------------------------------------------------------------------------
# Tests — createCalendarEventWithCode (code-gated single-calendar path)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateCalendarEventWithCodePolicyEnforcement:
    """Policy enforcement via the code-gated ``createCalendarEventWithCode`` mutation."""

    def test_no_policy_booking_succeeds_off_state(self):
        """Off-state: no BookingPolicy → code-gated write unchanged."""
        org = _org()
        cal = _code_calendar(org)
        token_obj, code = _booking_code(org, cal)

        data = _post_code_gated(variables={"input": _code_input(code)})
        result = data["data"]["createCalendarEventWithCode"]
        assert result["success"] is True
        assert result["errorCode"] is None
        # Code consumed.
        token_obj.refresh_from_db()
        assert token_obj.used_at is not None

    def test_lead_time_violation_returns_slot_unavailable_code_not_consumed(self):
        """Policy lead-time violation → SLOT_UNAVAILABLE result, code NOT consumed."""
        org = _org()
        cal = _code_calendar(org)
        token_obj, code = _booking_code(org, cal)

        lead_s = int((_START - _NOW).total_seconds()) + 3600
        create_booking_policy(calendar=cal, lead_time_seconds=lead_s)

        with patch("calendar_integration.services.calendar_service._tz") as mock_tz:
            mock_tz.now.return_value = _NOW
            data = _post_code_gated(variables={"input": _code_input(code)})

        result = data["data"]["createCalendarEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "SLOT_UNAVAILABLE"
        assert (
            "booking policy" in result["errorMessage"].lower()
            or "not available" in result["errorMessage"].lower()
        )

        # Code must NOT be consumed on violation.
        token_obj.refresh_from_db()
        assert token_obj.used_at is None

        # No event must be created.
        assert (
            not CalendarEvent.objects.filter_by_organization(org.id)
            .filter(calendar_fk_id=cal.id)
            .exists()
        )

    def test_horizon_violation_returns_slot_unavailable_code_not_consumed(self):
        """Policy horizon violation → SLOT_UNAVAILABLE result, code NOT consumed."""
        org = _org()
        cal = _code_calendar(org)
        token_obj, code = _booking_code(org, cal)

        horizon_s = int((_START - _NOW).total_seconds()) - 3600
        create_booking_policy(calendar=cal, max_horizon_seconds=horizon_s)

        with patch("calendar_integration.services.calendar_service._tz") as mock_tz:
            mock_tz.now.return_value = _NOW
            data = _post_code_gated(variables={"input": _code_input(code)})

        result = data["data"]["createCalendarEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "SLOT_UNAVAILABLE"

        token_obj.refresh_from_db()
        assert token_obj.used_at is None

    def test_buffer_violation_returns_slot_unavailable_code_not_consumed(self):
        """Buffer dead-zone violation → SLOT_UNAVAILABLE, code NOT consumed."""
        org = _org()
        cal = _code_calendar(org)
        token_obj, code = _booking_code(org, cal)

        CalendarEvent.objects.create(
            organization=org,
            calendar_fk=cal,
            title="Busy",
            description="",
            external_id=f"busy-{uuid.uuid4().hex[:6]}",
            start_time_tz_unaware=datetime.datetime(2030, 6, 1, 8, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )
        create_booking_policy(calendar=cal, buffer_after_seconds=3601)

        with patch("calendar_integration.services.calendar_service._tz") as mock_tz:
            mock_tz.now.return_value = _NOW
            data = _post_code_gated(variables={"input": _code_input(code)})

        result = data["data"]["createCalendarEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "SLOT_UNAVAILABLE"
        assert (
            "booking policy" in result["errorMessage"].lower()
            or "not available" in result["errorMessage"].lower()
        )

        token_obj.refresh_from_db()
        assert token_obj.used_at is None

    def test_compliant_code_gated_booking_succeeds(self):
        """A code-gated booking satisfying all policy rules succeeds."""
        org = _org()
        cal = _code_calendar(org)
        token_obj, code = _booking_code(org, cal)

        create_booking_policy(calendar=cal, lead_time_seconds=0)

        data = _post_code_gated(variables={"input": _code_input(code)})
        result = data["data"]["createCalendarEventWithCode"]
        assert result["success"] is True

        token_obj.refresh_from_db()
        assert token_obj.used_at is not None

    def test_discovery_enforcement_agreement_lead_time(self):
        """A slot NOT offered by the slot engine (lead-time) is also rejected at booking time.

        This test asserts the discovery/enforcement agreement: a slot the
        ``BookableSlotsService`` would reject is also rejected by ``create_event``.
        """
        from calendar_integration.services.bookable_slots_service import BookableSlotsService
        from calendar_integration.services.booking_policy_service import BookingPolicyService

        org = _org()
        cal = _code_calendar(org)
        _booking_code(org, cal)

        # Lead time: booking at _START from _NOW is too soon.
        lead_s = int((_START - _NOW).total_seconds()) + 3600
        create_booking_policy(calendar=cal, lead_time_seconds=lead_s)

        # Discovery: slot engine should NOT offer the slot.
        slots_svc = BookableSlotsService(booking_policy_service=BookingPolicyService())
        slots_svc.initialize(org)
        slots = slots_svc.find_bookable_slots_for_calendar(
            calendar_id=cal.id,
            search_window_start=_START - datetime.timedelta(minutes=5),
            search_window_end=_END + datetime.timedelta(minutes=5),
            duration=datetime.timedelta(hours=1),
            now=_NOW,
        )
        assert len(slots) == 0, "Discovery should NOT offer the slot"

        # Enforcement: create_event should also reject.
        _token_obj, code = _booking_code(org, cal)
        with patch("calendar_integration.services.calendar_service._tz") as mock_tz:
            mock_tz.now.return_value = _NOW
            data = _post_code_gated(variables={"input": _code_input(code)})

        result = data["data"]["createCalendarEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "SLOT_UNAVAILABLE"

    def test_discovery_enforcement_agreement_horizon(self):
        """A slot beyond the max-horizon is rejected by both discovery and enforcement."""
        from calendar_integration.services.bookable_slots_service import BookableSlotsService
        from calendar_integration.services.booking_policy_service import BookingPolicyService

        org = _org()
        cal = _code_calendar(org)

        # Horizon shorter than the gap to _START → slot is beyond the window.
        horizon_s = int((_START - _NOW).total_seconds()) - 3600
        create_booking_policy(calendar=cal, max_horizon_seconds=horizon_s)

        # Discovery rejects.
        slots_svc = BookableSlotsService(booking_policy_service=BookingPolicyService())
        slots_svc.initialize(org)
        slots = slots_svc.find_bookable_slots_for_calendar(
            calendar_id=cal.id,
            search_window_start=_START - datetime.timedelta(minutes=5),
            search_window_end=_END + datetime.timedelta(minutes=5),
            duration=datetime.timedelta(hours=1),
            now=_NOW,
        )
        assert len(slots) == 0, "Discovery should NOT offer the slot beyond horizon"

        # Enforcement rejects.
        _token_obj, code = _booking_code(org, cal)
        with patch("calendar_integration.services.calendar_service._tz") as mock_tz:
            mock_tz.now.return_value = _NOW
            data = _post_code_gated(variables={"input": _code_input(code)})

        result = data["data"]["createCalendarEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "SLOT_UNAVAILABLE"

    def test_discovery_enforcement_agreement_buffer(self):
        """A slot inside an existing event's buffer dead zone is rejected by both."""
        from calendar_integration.services.bookable_slots_service import BookableSlotsService
        from calendar_integration.services.booking_policy_service import BookingPolicyService

        org = _org()
        cal = _code_calendar(org)

        # Existing event ending at 09:00; buffer_after = 3601s → dead zone [09:00, 10:00:01).
        CalendarEvent.objects.create(
            organization=org,
            calendar_fk=cal,
            title="Busy",
            description="",
            external_id=f"busy-{uuid.uuid4().hex[:6]}",
            start_time_tz_unaware=datetime.datetime(2030, 6, 1, 8, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )
        create_booking_policy(calendar=cal, buffer_after_seconds=3601)

        # Discovery rejects.
        slots_svc = BookableSlotsService(booking_policy_service=BookingPolicyService())
        slots_svc.initialize(org)
        slots = slots_svc.find_bookable_slots_for_calendar(
            calendar_id=cal.id,
            search_window_start=_START - datetime.timedelta(minutes=5),
            search_window_end=_END + datetime.timedelta(minutes=5),
            duration=datetime.timedelta(hours=1),
            now=_NOW,
        )
        assert len(slots) == 0, "Discovery should NOT offer the slot inside buffer dead zone"

        # Enforcement rejects.
        _token_obj, code = _booking_code(org, cal)
        with patch("calendar_integration.services.calendar_service._tz") as mock_tz:
            mock_tz.now.return_value = _NOW
            data = _post_code_gated(variables={"input": _code_input(code)})

        result = data["data"]["createCalendarEventWithCode"]
        assert result["success"] is False
        assert result["errorCode"] == "SLOT_UNAVAILABLE"

    def test_discovery_enforcement_agreement_positive(self):
        """A slot the engine DOES offer is accepted by the mutation (positive agreement)."""
        from calendar_integration.services.bookable_slots_service import BookableSlotsService
        from calendar_integration.services.booking_policy_service import BookingPolicyService

        org = _org()
        cal = _code_calendar(org)

        # Policy: no lead-time constraint → all future slots offered.
        create_booking_policy(calendar=cal, lead_time_seconds=0)

        # Discovery offers the slot.
        slots_svc = BookableSlotsService(booking_policy_service=BookingPolicyService())
        slots_svc.initialize(org)
        slots = slots_svc.find_bookable_slots_for_calendar(
            calendar_id=cal.id,
            search_window_start=_START - datetime.timedelta(minutes=5),
            search_window_end=_END + datetime.timedelta(minutes=5),
            duration=datetime.timedelta(hours=1),
            now=_NOW,
        )
        assert len(slots) > 0, "Discovery should offer the slot"

        # Enforcement accepts.
        _token_obj, code = _booking_code(org, cal)
        data = _post_code_gated(variables={"input": _code_input(code)})
        result = data["data"]["createCalendarEventWithCode"]
        assert result["success"] is True
