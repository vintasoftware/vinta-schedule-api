"""Integration tests for Phase 8b — booking-time policy enforcement on ``create_grouped_event``.

Coverage:
- **Lead-time violation**: group booking too soon is rejected.
- **Max-horizon violation**: group booking too far ahead is rejected.
- **Buffer violation (event-envelope)**: group booking inside an existing event's dead zone
  is rejected.
- **Compliant booking**: a booking satisfying all rules succeeds.
- **No policy → unchanged behavior** (data-presence off-state): existing group booking tests
  must stay green; no enforcement when no BookingPolicy anywhere.
- **No rows on violation**: neither events nor blocked times are persisted after a rejected
  group booking (the transaction-atomicity guarantee).
- **Buffer event-envelope semantics**: the dead zone is defined by existing events; flush
  (touching boundary) is allowed.
- **Discovery/enforcement agreement**: a time ``find_bookable_slots`` would NOT offer is
  also rejected by ``create_grouped_event``; acceptance scenario #7 (horizon case) asserted
  for the group path.
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

import pytest

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.exceptions import BookingPolicyViolationError
from calendar_integration.factories import create_booking_policy
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroup,
)
from calendar_integration.services.booking_policy_service import BookingPolicyService
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    CalendarGroupEventInputData,
    CalendarGroupInputData,
    CalendarGroupSlotInputData,
    CalendarGroupSlotSelectionInputData,
)
from organizations.models import Organization


# ---------------------------------------------------------------------------
# Fixed instants — far enough in the future that lead/horizon math is stable
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2030, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
_BOOKING_START = datetime.datetime(2030, 6, 1, 10, 0, 0, tzinfo=datetime.UTC)
_BOOKING_END = datetime.datetime(2030, 6, 1, 11, 0, 0, tzinfo=datetime.UTC)

_counter = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unique() -> int:
    global _counter
    _counter += 1
    return _counter


def _managed_cal(org: Organization) -> Calendar:
    n = _unique()
    return Calendar.objects.create(
        organization=org,
        name=f"group-enforce-cal-{n}",
        external_id=f"group-enforce-{n}",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
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


def _event_in_db(
    calendar: Calendar, start: datetime.datetime, end: datetime.datetime
) -> CalendarEvent:
    n = _unique()
    return CalendarEvent.objects.create(
        organization=calendar.organization,
        calendar_fk=calendar,
        title="Existing",
        description="",
        external_id=f"ev-grp-enforce-{n}",
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
    )


def _make_service(org: Organization) -> CalendarGroupService:
    """Build a CalendarGroupService with a real BookingPolicyService injected."""
    cs = CalendarService(booking_policy_service=BookingPolicyService())
    cs.initialize_without_provider(user_or_token=None, organization=org)
    gs = CalendarGroupService(
        calendar_service=cs,
        booking_policy_service=BookingPolicyService(),
    )
    gs.initialize(organization=org)
    return gs


def _make_group(service: CalendarGroupService, *calendars: Calendar) -> CalendarGroup:
    """Create a single-slot group whose pool contains all provided calendars."""
    return service.create_group(
        CalendarGroupInputData(
            name=f"group-enforce-{_unique()}",
            accepts_public_scheduling=True,
            slots=[
                CalendarGroupSlotInputData(
                    name="Main",
                    calendar_ids=[c.id for c in calendars],
                    order=0,
                )
            ],
        )
    )


def _book(
    service: CalendarGroupService,
    group: CalendarGroup,
    calendar: Calendar,
    start: datetime.datetime = _BOOKING_START,
    end: datetime.datetime = _BOOKING_END,
) -> CalendarEvent:
    """Attempt a group booking of `calendar` for the given window."""
    slot = group.slots.get()
    return service.create_grouped_event(
        CalendarGroupEventInputData(
            title="Test Group Booking",
            description="",
            start_time=start,
            end_time=end,
            timezone="UTC",
            group_id=group.id,
            slot_selections=[
                CalendarGroupSlotSelectionInputData(
                    slot_id=slot.id,
                    calendar_ids=[calendar.id],
                )
            ],
        )
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGroupBookingPolicyEnforcement:
    """Policy enforcement on the group ``create_grouped_event`` path."""

    @pytest.fixture
    def org(self, db):
        return Organization.objects.create(name="GroupEnforceOrg", should_sync_rooms=False)

    @pytest.fixture
    def calendar(self, org):
        cal = _managed_cal(org)
        _available(
            cal,
            _BOOKING_START - datetime.timedelta(hours=2),
            _BOOKING_END + datetime.timedelta(hours=2),
        )
        return cal

    # ------------------------------------------------------------------
    # Off-state: no policy → no enforcement, write succeeds
    # ------------------------------------------------------------------

    def test_no_policy_write_unchanged(self, org, calendar):
        """With no BookingPolicy anywhere, create_grouped_event behaves exactly as before."""
        service = _make_service(org)
        group = _make_group(service, calendar)
        event = _book(service, group, calendar)
        assert event is not None
        assert CalendarEvent.objects.filter_by_organization(org.id).filter(id=event.id).exists()

    # ------------------------------------------------------------------
    # Lead-time violation
    # ------------------------------------------------------------------

    def test_lead_time_violation_raises(self, org, calendar):
        """A group booking whose start < now + lead_time is rejected."""
        service = _make_service(org)
        group = _make_group(service, calendar)
        lead_seconds = int((_BOOKING_START - _NOW).total_seconds()) + 3600  # 1 extra hour
        create_booking_policy(calendar_group=group, lead_time_seconds=lead_seconds)

        with pytest.raises(BookingPolicyViolationError):
            with patch("calendar_integration.services.calendar_group_service.timezone") as mock_tz:
                mock_tz.now.return_value = _NOW
                _book(service, group, calendar)

    def test_lead_time_compliant_succeeds(self, org, calendar):
        """A booking with zero lead_time (no constraint) succeeds."""
        service = _make_service(org)
        group = _make_group(service, calendar)
        create_booking_policy(calendar_group=group, lead_time_seconds=0)

        event = _book(service, group, calendar)
        assert event is not None

    # ------------------------------------------------------------------
    # Max-horizon violation (Acceptance scenario #7)
    # ------------------------------------------------------------------

    def test_horizon_violation_raises(self, org, calendar):
        """A group booking whose start > now + max_horizon is rejected.

        Covers acceptance scenario #7: beyond-horizon group booking is
        rejected and nothing is persisted.
        """
        service = _make_service(org)
        group = _make_group(service, calendar)
        short_horizon = int((_BOOKING_START - _NOW).total_seconds()) - 3600
        create_booking_policy(calendar_group=group, max_horizon_seconds=short_horizon)

        with pytest.raises(BookingPolicyViolationError):
            with patch("calendar_integration.services.calendar_group_service.timezone") as mock_tz:
                mock_tz.now.return_value = _NOW
                _book(service, group, calendar)

    def test_horizon_zero_means_unbounded(self, org, calendar):
        """max_horizon_seconds=0 means unbounded; any future booking is allowed."""
        service = _make_service(org)
        group = _make_group(service, calendar)
        create_booking_policy(calendar_group=group, max_horizon_seconds=0)

        event = _book(service, group, calendar)
        assert event is not None

    # ------------------------------------------------------------------
    # Buffer violation (event-envelope / dead-zone)
    # ------------------------------------------------------------------

    def test_buffer_after_violation_raises(self, org, calendar):
        """A group booking inside an existing event's after-buffer dead zone is rejected."""
        service = _make_service(org)
        group = _make_group(service, calendar)
        # Existing event ends at 09:00; buffer_after = 3601s → dead zone ends at 10:00:01.
        # Booking starts at 10:00 → inside the dead zone.
        existing_start = datetime.datetime(2030, 6, 1, 8, 0, 0, tzinfo=datetime.UTC)
        existing_end = datetime.datetime(2030, 6, 1, 9, 0, 0, tzinfo=datetime.UTC)
        _event_in_db(calendar, existing_start, existing_end)
        create_booking_policy(calendar_group=group, buffer_after_seconds=3601)

        with pytest.raises(BookingPolicyViolationError):
            with patch("calendar_integration.services.calendar_group_service.timezone") as mock_tz:
                mock_tz.now.return_value = _NOW
                _book(service, group, calendar)

    def test_buffer_after_flush_boundary_allowed(self, org, calendar):
        """A booking touching exactly the dead-zone boundary (flush) is allowed."""
        service = _make_service(org)
        group = _make_group(service, calendar)
        # Existing event ends at 09:00; buffer_after = 3600s → dead zone [09:00, 10:00).
        # Booking starts at exactly 10:00 — touching, not overlapping → allowed.
        existing_start = datetime.datetime(2030, 6, 1, 8, 0, 0, tzinfo=datetime.UTC)
        existing_end = datetime.datetime(2030, 6, 1, 9, 0, 0, tzinfo=datetime.UTC)
        _event_in_db(calendar, existing_start, existing_end)
        create_booking_policy(calendar_group=group, buffer_after_seconds=3600)

        event = _book(service, group, calendar)
        assert event is not None

    def test_buffer_before_violation_raises(self, org, calendar):
        """A group booking inside an existing event's before-buffer dead zone is rejected."""
        service = _make_service(org)
        group = _make_group(service, calendar)
        # Existing event starts at 11:30; buffer_before = 1801s → dead zone starts at 10:59:59.
        # Booking ends at 11:00 → inside the dead zone.
        existing_start = datetime.datetime(2030, 6, 1, 11, 30, 0, tzinfo=datetime.UTC)
        existing_end = datetime.datetime(2030, 6, 1, 12, 30, 0, tzinfo=datetime.UTC)
        _event_in_db(calendar, existing_start, existing_end)
        create_booking_policy(calendar_group=group, buffer_before_seconds=1801)

        with pytest.raises(BookingPolicyViolationError):
            with patch("calendar_integration.services.calendar_group_service.timezone") as mock_tz:
                mock_tz.now.return_value = _NOW
                _book(service, group, calendar)

    # ------------------------------------------------------------------
    # No rows on violation (transaction atomicity guarantee)
    # ------------------------------------------------------------------

    def test_no_rows_persisted_on_lead_time_violation(self, org, calendar):
        """A lead-time violation rolls back the entire group write — zero rows persisted."""
        service = _make_service(org)
        group = _make_group(service, calendar)
        lead_seconds = int((_BOOKING_START - _NOW).total_seconds()) + 3600
        create_booking_policy(calendar_group=group, lead_time_seconds=lead_seconds)

        with pytest.raises(BookingPolicyViolationError):
            with patch("calendar_integration.services.calendar_group_service.timezone") as mock_tz:
                mock_tz.now.return_value = _NOW
                _book(service, group, calendar)

        # No events or blocked times should exist.
        assert CalendarEvent.objects.filter_by_organization(org.id).count() == 0
        assert BlockedTime.objects.filter_by_organization(org.id).count() == 0
        assert CalendarEventGroupSelection.objects.filter_by_organization(org.id).count() == 0

    def test_no_rows_persisted_on_horizon_violation(self, org, calendar):
        """A max-horizon violation rolls back the entire group write — zero rows persisted."""
        service = _make_service(org)
        group = _make_group(service, calendar)
        short_horizon = int((_BOOKING_START - _NOW).total_seconds()) - 3600
        create_booking_policy(calendar_group=group, max_horizon_seconds=short_horizon)

        with pytest.raises(BookingPolicyViolationError):
            with patch("calendar_integration.services.calendar_group_service.timezone") as mock_tz:
                mock_tz.now.return_value = _NOW
                _book(service, group, calendar)

        assert CalendarEvent.objects.filter_by_organization(org.id).count() == 0
        assert BlockedTime.objects.filter_by_organization(org.id).count() == 0
        assert CalendarEventGroupSelection.objects.filter_by_organization(org.id).count() == 0

    # ------------------------------------------------------------------
    # Discovery/enforcement agreement
    # ------------------------------------------------------------------

    def test_discovery_enforcement_agreement_lead_time(self, org, calendar):
        """A time find_bookable_slots would not offer is also rejected by create_grouped_event.

        Acceptance scenario #7 (horizon case): asserted below in a dedicated test.
        This test covers the lead-time case.
        """
        booking_policy_service = BookingPolicyService()
        booking_policy_service.initialize(org)

        service = _make_service(org)
        group = _make_group(service, calendar)
        lead_seconds = int((_BOOKING_START - _NOW).total_seconds()) + 3600
        create_booking_policy(calendar_group=group, lead_time_seconds=lead_seconds)

        # Discovery: find_bookable_slots should NOT offer _BOOKING_START.
        gs_discovery = CalendarGroupService(booking_policy_service=booking_policy_service)
        gs_discovery.initialize(organization=org)
        slots = gs_discovery.find_bookable_slots(
            group_id=group.id,
            search_window_start=_BOOKING_START - datetime.timedelta(minutes=5),
            search_window_end=_BOOKING_END + datetime.timedelta(minutes=5),
            duration=datetime.timedelta(hours=1),
            now=_NOW,
        )
        assert len(slots) == 0, "Discovery should NOT offer the slot (lead-time too tight)"

        # Enforcement: create_grouped_event must also reject.
        with pytest.raises(BookingPolicyViolationError):
            with patch("calendar_integration.services.calendar_group_service.timezone") as mock_tz:
                mock_tz.now.return_value = _NOW
                _book(service, group, calendar)

    def test_discovery_enforcement_agreement_horizon(self, org, calendar):
        """A time beyond max-horizon is rejected by both discovery and enforcement.

        This is the group-path assertion of Acceptance scenario #7.
        """
        booking_policy_service = BookingPolicyService()
        booking_policy_service.initialize(org)

        service = _make_service(org)
        group = _make_group(service, calendar)
        short_horizon = int((_BOOKING_START - _NOW).total_seconds()) - 3600
        create_booking_policy(calendar_group=group, max_horizon_seconds=short_horizon)

        # Discovery: find_bookable_slots should NOT offer _BOOKING_START (beyond horizon).
        gs_discovery = CalendarGroupService(booking_policy_service=booking_policy_service)
        gs_discovery.initialize(organization=org)
        slots = gs_discovery.find_bookable_slots(
            group_id=group.id,
            search_window_start=_BOOKING_START - datetime.timedelta(minutes=5),
            search_window_end=_BOOKING_END + datetime.timedelta(minutes=5),
            duration=datetime.timedelta(hours=1),
            now=_NOW,
        )
        assert len(slots) == 0, "Discovery should NOT offer the slot (beyond horizon)"

        # Enforcement: create_grouped_event must also reject.
        with pytest.raises(BookingPolicyViolationError):
            with patch("calendar_integration.services.calendar_group_service.timezone") as mock_tz:
                mock_tz.now.return_value = _NOW
                _book(service, group, calendar)

    def test_discovery_enforcement_agreement_positive(self, org, calendar):
        """A time find_bookable_slots DOES offer is accepted by create_grouped_event."""
        booking_policy_service = BookingPolicyService()
        booking_policy_service.initialize(org)

        service = _make_service(org)
        group = _make_group(service, calendar)
        # Policy with zero lead (no constraint) → all future slots offered.
        create_booking_policy(calendar_group=group, lead_time_seconds=0)

        # Discovery: slot should be offered.
        gs_discovery = CalendarGroupService(booking_policy_service=booking_policy_service)
        gs_discovery.initialize(organization=org)
        slots = gs_discovery.find_bookable_slots(
            group_id=group.id,
            search_window_start=_BOOKING_START - datetime.timedelta(minutes=5),
            search_window_end=_BOOKING_END + datetime.timedelta(minutes=5),
            duration=datetime.timedelta(hours=1),
            now=_NOW,
        )
        assert len(slots) > 0, "Discovery should offer the slot (zero lead time)"

        # Enforcement: booking must succeed.
        event = _book(service, group, calendar)
        assert event is not None

    # ------------------------------------------------------------------
    # Policy resolves via resolve_for_group (org-default fallthrough)
    # ------------------------------------------------------------------

    def test_org_default_policy_enforced_for_group(self, org, calendar):
        """An org-default policy is enforced for a group with no explicit policy."""
        service = _make_service(org)
        group = _make_group(service, calendar)
        # No group-specific policy — set org default with a blocking lead time.
        lead_seconds = int((_BOOKING_START - _NOW).total_seconds()) + 3600
        create_booking_policy(
            is_organization_default=True, organization=org, lead_time_seconds=lead_seconds
        )

        with pytest.raises(BookingPolicyViolationError):
            with patch("calendar_integration.services.calendar_group_service.timezone") as mock_tz:
                mock_tz.now.return_value = _NOW
                _book(service, group, calendar)
