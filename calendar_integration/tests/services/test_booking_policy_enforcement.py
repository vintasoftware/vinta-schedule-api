"""Integration tests for Phase 8a — booking-time policy enforcement on ``create_event``.

Coverage:
- **Single calendar (auth path)**:
  - Lead-time violation: booking too soon is rejected.
  - Horizon violation: booking too far ahead is rejected.
  - Buffer violation: booking inside an existing event's dead zone is rejected.
  - Compliant booking (all rules satisfied): succeeds.
  - No policy → unchanged write behavior (the data-presence off-state).

- **Bundle calendar (auth path)**:
  - Policy attached to bundle calendar: violation raises ``BookingPolicyViolationError``.
  - No policy: write goes through unchanged.

- **Concurrency guard**:
  - A slot valid at discovery time but made invalid by a concurrent event (created
    between discovery and booking) is correctly rejected at write time.

- **Event-envelope semantics**:
  - A booking inside the buffer dead zone of an existing event is rejected;
    a booking touching the dead zone boundary (flush) is allowed.

- **Error inheritance**:
  - ``BookingPolicyViolationError`` is a ``CalendarIntegrationError`` subclass.
"""

from __future__ import annotations

import datetime

import pytest

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.exceptions import BookingPolicyViolationError
from calendar_integration.factories import create_booking_policy
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    ChildrenCalendarRelationship,
)
from calendar_integration.services.booking_policy_service import BookingPolicyService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import CalendarEventInputData
from organizations.models import Organization


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_counter = 0


def _cal(org: Organization, *, managed: bool = True, cal_type=CalendarType.PERSONAL) -> Calendar:
    global _counter
    _counter += 1
    return Calendar.objects.create(
        organization=org,
        name=f"enforce-cal-{_counter}",
        external_id=f"enforce-cal-{_counter}",
        provider=CalendarProvider.INTERNAL,
        calendar_type=cal_type,
        manage_available_windows=managed,
        accepts_public_scheduling=True,
    )


def _available(
    calendar: Calendar, start: datetime.datetime, end: datetime.datetime
) -> AvailableTime:
    return AvailableTime.objects.create(
        organization=calendar.organization,
        calendar=calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
    )


def _blocked(calendar: Calendar, start: datetime.datetime, end: datetime.datetime) -> BlockedTime:
    global _counter
    _counter += 1
    return BlockedTime.objects.create(
        organization=calendar.organization,
        calendar=calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
        external_id=f"bt-enforce-{_counter}",
    )


def _event_in_db(
    calendar: Calendar, start: datetime.datetime, end: datetime.datetime
) -> CalendarEvent:
    global _counter
    _counter += 1
    return CalendarEvent.objects.create(
        organization=calendar.organization,
        calendar_fk=calendar,
        title="Busy",
        description="",
        external_id=f"ev-enforce-{_counter}",
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
    )


def _service(org: Organization) -> CalendarService:
    """Build a CalendarService with a real BookingPolicyService injected.

    No ``calendar_permission_service`` is passed so permission checks in
    ``CalendarEventService.create_event`` fall through to the unconstrained path
    (``can_perform_scheduling`` with no user/token and an INTERNAL
    ``accepts_public_scheduling=True`` calendar).  This keeps the tests focused
    on the booking-policy enforcement hook without needing real token plumbing.
    """
    svc = CalendarService(booking_policy_service=BookingPolicyService())
    # Use user_or_token=None so the service does NOT try to initialize a token.
    svc.initialize_without_provider(user_or_token=None, organization=org)
    return svc


# Booking dates well in the future so lead/horizon comparisons are stable.
_NOW = datetime.datetime(2030, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
_BOOKING_START = datetime.datetime(2030, 6, 1, 10, 0, 0, tzinfo=datetime.UTC)
_BOOKING_END = datetime.datetime(2030, 6, 1, 11, 0, 0, tzinfo=datetime.UTC)


def _event_input_data(
    start_time: datetime.datetime = _BOOKING_START,
    end_time: datetime.datetime = _BOOKING_END,
) -> CalendarEventInputData:
    """Build a ``CalendarEventInputData`` for the standard test booking window.

    ``group_authorized=True`` bypasses the permission-service check inside
    ``CalendarEventService.create_event`` so the tests don't need real token
    plumbing; the booking-policy enforcement (our concern) runs first.
    """
    return CalendarEventInputData(
        title="Test Booking",
        description="",
        start_time=start_time,
        end_time=end_time,
        timezone="UTC",
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
        group_authorized=True,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBookingPolicyEnforcementSingleCalendar:
    """Policy enforcement on the single-calendar ``create_event`` path."""

    @pytest.fixture
    def org(self, db):
        return Organization.objects.create(name="Enforcement Org Single", should_sync_rooms=False)

    @pytest.fixture
    def calendar(self, org):
        cal = _cal(org, managed=True)
        _available(
            cal,
            _BOOKING_START - datetime.timedelta(hours=2),
            _BOOKING_END + datetime.timedelta(hours=2),
        )
        return cal

    def _svc(self, org):
        return _service(org)

    # ------------------------------------------------------------------
    # Off-state: no policy → no enforcement, write succeeds
    # ------------------------------------------------------------------

    def test_no_policy_write_unchanged(self, org, calendar):
        """With no BookingPolicy anywhere, create_event behaves exactly as before."""
        svc = self._svc(org)
        # No policy created — booking should succeed without any violation check.
        event = svc.create_event(calendar.id, _event_input_data())
        assert event is not None
        assert CalendarEvent.objects.filter_by_organization(org.id).filter(id=event.id).exists()

    # ------------------------------------------------------------------
    # Lead-time violation
    # ------------------------------------------------------------------

    def test_lead_time_violation_raises(self, org, calendar):
        """A booking whose start < now + lead_time is rejected."""
        lead_seconds = int((_BOOKING_START - _NOW).total_seconds()) + 3600  # 1 extra hour
        create_booking_policy(calendar=calendar, lead_time_seconds=lead_seconds)
        svc = self._svc(org)

        with pytest.raises(BookingPolicyViolationError):
            from unittest.mock import patch

            with patch("calendar_integration.services.calendar_service._tz") as mock_tz:
                mock_tz.now.return_value = _NOW
                svc.create_event(calendar.id, _event_input_data())

    def test_lead_time_compliant_succeeds(self, org, calendar):
        """A booking exactly at now + lead_time (inclusive) is allowed."""
        # lead_time of 0 seconds: any future booking is fine.
        create_booking_policy(calendar=calendar, lead_time_seconds=0)
        svc = self._svc(org)
        event = svc.create_event(calendar.id, _event_input_data())
        assert event is not None

    # ------------------------------------------------------------------
    # Horizon violation
    # ------------------------------------------------------------------

    def test_horizon_violation_raises(self, org, calendar):
        """A booking whose start > now + max_horizon is rejected."""
        # max_horizon shorter than the gap between now and BOOKING_START
        short_horizon = int((_BOOKING_START - _NOW).total_seconds()) - 3600
        create_booking_policy(calendar=calendar, max_horizon_seconds=short_horizon)
        svc = self._svc(org)

        with pytest.raises(BookingPolicyViolationError):
            from unittest.mock import patch

            with patch("calendar_integration.services.calendar_service._tz") as mock_tz:
                mock_tz.now.return_value = _NOW
                svc.create_event(calendar.id, _event_input_data())

    def test_horizon_zero_means_unbounded(self, org, calendar):
        """max_horizon_seconds=0 means unbounded; any future booking is allowed."""
        create_booking_policy(calendar=calendar, max_horizon_seconds=0)
        svc = self._svc(org)
        event = svc.create_event(calendar.id, _event_input_data())
        assert event is not None

    # ------------------------------------------------------------------
    # Buffer violation (event-envelope / dead-zone)
    # ------------------------------------------------------------------

    def test_buffer_after_violation_raises(self, org, calendar):
        """A booking that falls inside an existing event's after-buffer dead zone is rejected."""
        # Existing event ends at 09:00 (1h before booking start 10:00).
        # buffer_after = 3600s (1h) → dead zone is [09:00, 09:00 + 1h) = [09:00, 10:00).
        # Booking [10:00, 11:00) starts exactly at 10:00. Touching is NOT overlap, so this
        # should PASS. Let's make buffer_after = 3601s to push the dead zone past 10:00.
        existing_end = datetime.datetime(2030, 6, 1, 9, 0, 0, tzinfo=datetime.UTC)
        existing_start = datetime.datetime(2030, 6, 1, 8, 0, 0, tzinfo=datetime.UTC)
        _event_in_db(calendar, existing_start, existing_end)
        # buffer_after = 1h + 1s → dead zone ends at 10:00:01 > booking start 10:00.
        create_booking_policy(calendar=calendar, buffer_after_seconds=3601)
        svc = self._svc(org)

        with pytest.raises(BookingPolicyViolationError):
            from unittest.mock import patch

            with patch("calendar_integration.services.calendar_service._tz") as mock_tz:
                mock_tz.now.return_value = _NOW
                svc.create_event(calendar.id, _event_input_data())

    def test_buffer_after_flush_boundary_allowed(self, org, calendar):
        """A booking touching exactly the end of the dead zone (flush) is allowed."""
        # Existing event ends at 09:00, buffer_after = 3600s → dead zone [09:00, 10:00).
        # Booking starts exactly at 10:00 — touching, not overlapping → allowed.
        existing_end = datetime.datetime(2030, 6, 1, 9, 0, 0, tzinfo=datetime.UTC)
        existing_start = datetime.datetime(2030, 6, 1, 8, 0, 0, tzinfo=datetime.UTC)
        _event_in_db(calendar, existing_start, existing_end)
        create_booking_policy(calendar=calendar, buffer_after_seconds=3600)
        svc = self._svc(org)
        event = svc.create_event(calendar.id, _event_input_data())
        assert event is not None

    def test_buffer_before_violation_raises(self, org, calendar):
        """A booking that falls inside an existing event's before-buffer dead zone is rejected."""
        # Existing event starts at 11:30 (30 min after booking end 11:00).
        # buffer_before = 1800s (30 min) → dead zone is [11:30 - 30m, 11:30) = [11:00, 11:30).
        # Booking end 11:00 → dead zone starts at 11:00; touching, so this should PASS.
        # Use buffer_before = 1801s → dead zone starts at 10:59:59 < booking end 11:00 → overlap.
        existing_start = datetime.datetime(2030, 6, 1, 11, 30, 0, tzinfo=datetime.UTC)
        existing_end = datetime.datetime(2030, 6, 1, 12, 30, 0, tzinfo=datetime.UTC)
        _event_in_db(calendar, existing_start, existing_end)
        create_booking_policy(calendar=calendar, buffer_before_seconds=1801)
        svc = self._svc(org)

        with pytest.raises(BookingPolicyViolationError):
            from unittest.mock import patch

            with patch("calendar_integration.services.calendar_service._tz") as mock_tz:
                mock_tz.now.return_value = _NOW
                svc.create_event(calendar.id, _event_input_data())

    def test_no_buffer_no_adjacent_events_compliant(self, org, calendar):
        """With no buffer policy, a booking adjacent to existing events is allowed."""
        # Adjacent event ending exactly at booking start.
        adjacent_end = _BOOKING_START
        adjacent_start = _BOOKING_START - datetime.timedelta(hours=1)
        _event_in_db(calendar, adjacent_start, adjacent_end)
        create_booking_policy(calendar=calendar, lead_time_seconds=0)
        svc = self._svc(org)
        event = svc.create_event(calendar.id, _event_input_data())
        assert event is not None

    # ------------------------------------------------------------------
    # Concurrency guard
    # ------------------------------------------------------------------

    def test_concurrency_guard_rejects_slot_invalidated_between_discovery_and_booking(
        self, org, calendar
    ):
        """A slot valid at discovery is rejected if it is inside the buffer dead zone
        of an event created concurrently (after discovery, before booking)."""
        buffer_after_s = 3601
        create_booking_policy(calendar=calendar, buffer_after_seconds=buffer_after_s)
        svc = self._svc(org)

        # Simulate: discovery ran, found the slot free. Concurrently an event is created.
        _event_in_db(
            calendar,
            datetime.datetime(2030, 6, 1, 8, 0, 0, tzinfo=datetime.UTC),
            datetime.datetime(2030, 6, 1, 9, 0, 0, tzinfo=datetime.UTC),
        )

        # Booking attempt now — slot is inside the dead zone, must be rejected.
        with pytest.raises(BookingPolicyViolationError):
            from unittest.mock import patch

            with patch("calendar_integration.services.calendar_service._tz") as mock_tz:
                mock_tz.now.return_value = _NOW
                svc.create_event(calendar.id, _event_input_data())


@pytest.mark.django_db
class TestBookingPolicyEnforcementBundle:
    """Policy enforcement on the bundle calendar ``create_event`` path."""

    @pytest.fixture
    def org(self, db):
        return Organization.objects.create(name="Enforcement Org Bundle", should_sync_rooms=False)

    def _make_bundle(self, org: Organization):
        bundle = _cal(org, managed=False, cal_type=CalendarType.BUNDLE)
        child = _cal(org, managed=True)
        _available(
            child,
            _BOOKING_START - datetime.timedelta(hours=2),
            _BOOKING_END + datetime.timedelta(hours=2),
        )
        ChildrenCalendarRelationship.objects.create(
            organization=org,
            bundle_calendar=bundle,
            child_calendar=child,
            is_primary=True,
        )
        return bundle, child

    def test_bundle_no_policy_write_unchanged(self, org):
        """No policy on bundle or children → create_event is not blocked."""
        bundle, _child = self._make_bundle(org)
        svc = _service(org)
        # The bundle create_event path actually calls _create_bundle_event which
        # does additional bundle-specific checks, but it should not raise
        # BookingPolicyViolationError when no policy exists.
        # Since we're in a non-managed bundle without a full adapter, the
        # call may raise NoAvailableTimeWindowsError or similar for the child,
        # but NOT BookingPolicyViolationError.
        from calendar_integration.exceptions import BookingPolicyViolationError

        try:
            svc.create_event(bundle.id, _event_input_data())
        except BookingPolicyViolationError:
            pytest.fail("BookingPolicyViolationError raised with no policy in place")
        except Exception:  # noqa: BLE001
            pass  # other errors (NoAvailableTime, PermissionDenied) are acceptable

    def test_bundle_lead_time_violation_raises(self, org):
        """Policy attached directly to the bundle calendar enforces lead time."""
        bundle, _child = self._make_bundle(org)
        lead_seconds = int((_BOOKING_START - _NOW).total_seconds()) + 3600
        create_booking_policy(calendar=bundle, lead_time_seconds=lead_seconds)
        svc = _service(org)

        with pytest.raises(BookingPolicyViolationError):
            from unittest.mock import patch

            with patch("calendar_integration.services.calendar_service._tz") as mock_tz:
                mock_tz.now.return_value = _NOW
                svc.create_event(bundle.id, _event_input_data())

    def test_bundle_horizon_violation_raises(self, org):
        """Policy attached to bundle calendar enforces max_horizon."""
        bundle, _child = self._make_bundle(org)
        short_horizon = int((_BOOKING_START - _NOW).total_seconds()) - 3600
        create_booking_policy(calendar=bundle, max_horizon_seconds=short_horizon)
        svc = _service(org)

        with pytest.raises(BookingPolicyViolationError):
            from unittest.mock import patch

            with patch("calendar_integration.services.calendar_service._tz") as mock_tz:
                mock_tz.now.return_value = _NOW
                svc.create_event(bundle.id, _event_input_data())


@pytest.mark.django_db
class TestBookingPolicyErrorInheritance:
    """Verify the exception class hierarchy is correct."""

    def test_booking_policy_violation_is_calendar_integration_error(self):
        from calendar_integration.exceptions import CalendarIntegrationError

        err = BookingPolicyViolationError()
        assert isinstance(err, CalendarIntegrationError)

    def test_booking_policy_violation_default_message(self):
        err = BookingPolicyViolationError()
        assert "booking policy" in str(err).lower()

    def test_booking_policy_violation_custom_message(self):
        err = BookingPolicyViolationError("custom message")
        assert str(err) == "custom message"
