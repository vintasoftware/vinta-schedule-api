"""Tests for BookableSlotsService + slot_engine.

Integration coverage:
- A single managed calendar yields the same slots a one-calendar group would.
- A single unmanaged calendar yields the same slots a one-calendar group would.
- A bundle with two free children yields the slot; a busy child suppresses it.
- Lead-time, max-horizon, buffer-before and buffer-after each drop the expected
  candidates.
- Empty window / step >= window / empty bundle → [].
- A no-policy run is byte-for-byte identical to the un-policied engine output.

Unit coverage (policy filter boundary instants):
- A slot starting exactly at ``now + lead_time`` is kept (inclusive).
- A slot starting exactly at ``now + max_horizon`` is kept (inclusive).
- An envelope ending exactly where a blocking span begins is allowed (touching
  is not overlap); a one-second deeper envelope is rejected.
"""

from __future__ import annotations

import datetime
from datetime import timedelta

from django.utils import timezone

import pytest

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.exceptions import BookableSlotsValidationError
from calendar_integration.factories import create_booking_policy
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    ChildrenCalendarRelationship,
)
from calendar_integration.services import slot_engine
from calendar_integration.services.bookable_slots_service import BookableSlotsService
from calendar_integration.services.booking_policy_service import BookingPolicyService
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.dataclasses import (
    BookableSlotProposal,
    CalendarGroupInputData,
    CalendarGroupSlotInputData,
    EffectivePolicy,
)
from organizations.models import Organization


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Slots Org", should_sync_rooms=False)


@pytest.fixture
def service(organization):
    svc = BookableSlotsService(booking_policy_service=BookingPolicyService())
    svc.initialize(organization=organization)
    return svc


@pytest.fixture
def group_service(organization):
    svc = CalendarGroupService()
    svc.initialize(organization=organization)
    return svc


_counter = 0


def _calendar(org: Organization, *, managed: bool, calendar_type=CalendarType.PERSONAL) -> Calendar:
    global _counter
    _counter += 1
    return Calendar.objects.create(
        organization=org,
        name=f"cal-{_counter}",
        external_id=f"cal-{_counter}",
        provider=CalendarProvider.GOOGLE,
        calendar_type=calendar_type,
        manage_available_windows=managed,
    )


def _available(calendar: Calendar, start, end) -> AvailableTime:
    return AvailableTime.objects.create(
        organization=calendar.organization,
        calendar=calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
    )


def _blocked(calendar: Calendar, start, end) -> BlockedTime:
    global _counter
    _counter += 1
    return BlockedTime.objects.create(
        organization=calendar.organization,
        calendar=calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
        external_id=f"bt-{_counter}",
    )


def _event(calendar: Calendar, start, end) -> CalendarEvent:
    global _counter
    _counter += 1
    return CalendarEvent.objects.create(
        organization=calendar.organization,
        calendar_fk=calendar,
        title="Busy",
        description="",
        external_id=f"ev-{_counter}",
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
    )


def _one_calendar_group(group_service: CalendarGroupService, calendar: Calendar):
    return group_service.create_group(
        CalendarGroupInputData(
            name=f"grp-{calendar.id}",
            description="",
            slots=[
                CalendarGroupSlotInputData(
                    name="Only",
                    calendar_ids=[calendar.id],
                    required_count=1,
                    order=0,
                )
            ],
        )
    )


def _times(proposals: list[BookableSlotProposal]):
    return [(p.start_time, p.end_time) for p in proposals]


# ---------------------------------------------------------------------------
# Single-calendar parity with a one-calendar group
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_single_managed_calendar_matches_one_calendar_group(service, group_service, organization):
    cal = _calendar(organization, managed=True)
    now = timezone.now().replace(microsecond=0)
    window_start = now + timedelta(hours=1)
    good_start = window_start + timedelta(minutes=15)
    good_end = good_start + timedelta(minutes=30)
    _available(cal, good_start, good_end)

    kwargs = dict(
        search_window_start=window_start,
        search_window_end=window_start + timedelta(hours=1),
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=15),
    )

    group = _one_calendar_group(group_service, cal)
    group_proposals = group_service.find_bookable_slots(group_id=group.id, **kwargs)
    cal_proposals = service.find_bookable_slots_for_calendar(calendar_id=cal.id, **kwargs)

    assert _times(cal_proposals) == _times(group_proposals)
    assert _times(cal_proposals) == [(good_start, good_end)]


@pytest.mark.django_db
def test_single_unmanaged_calendar_matches_one_calendar_group(service, group_service, organization):
    cal = _calendar(organization, managed=False)
    now = timezone.now().replace(microsecond=0)
    window_start = now + timedelta(hours=1)
    # Unmanaged: free unless an event/blocked-time overlaps. Block the first 30 min.
    _blocked(cal, window_start, window_start + timedelta(minutes=30))

    kwargs = dict(
        search_window_start=window_start,
        search_window_end=window_start + timedelta(hours=1),
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=15),
    )

    group = _one_calendar_group(group_service, cal)
    group_proposals = group_service.find_bookable_slots(group_id=group.id, **kwargs)
    cal_proposals = service.find_bookable_slots_for_calendar(calendar_id=cal.id, **kwargs)

    assert _times(cal_proposals) == _times(group_proposals)
    # The 0:00 and 0:15 candidates overlap the block; 0:30 is free.
    assert _times(cal_proposals) == [
        (window_start + timedelta(minutes=30), window_start + timedelta(minutes=60))
    ]


# ---------------------------------------------------------------------------
# Bundle: all children must be free
# ---------------------------------------------------------------------------


def _make_bundle(org: Organization, children: list[Calendar]) -> Calendar:
    bundle = _calendar(org, managed=False, calendar_type=CalendarType.BUNDLE)
    for i, child in enumerate(children):
        ChildrenCalendarRelationship.objects.create(
            organization=org,
            bundle_calendar=bundle,
            child_calendar=child,
            is_primary=(i == 0),
        )
    return bundle


@pytest.mark.django_db
def test_bundle_two_free_children_yields_slot(service, organization):
    c1 = _calendar(organization, managed=True)
    c2 = _calendar(organization, managed=True)
    now = timezone.now().replace(microsecond=0)
    window_start = now + timedelta(hours=1)
    good_start = window_start + timedelta(minutes=15)
    good_end = good_start + timedelta(minutes=30)
    _available(c1, good_start, good_end)
    _available(c2, good_start, good_end)
    bundle = _make_bundle(organization, [c1, c2])

    proposals = service.find_bookable_slots_for_calendar(
        calendar_id=bundle.id,
        search_window_start=window_start,
        search_window_end=window_start + timedelta(hours=1),
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=15),
    )
    assert _times(proposals) == [(good_start, good_end)]


@pytest.mark.django_db
def test_bundle_busy_child_suppresses_slot(service, organization):
    c1 = _calendar(organization, managed=True)
    c2 = _calendar(organization, managed=True)
    now = timezone.now().replace(microsecond=0)
    window_start = now + timedelta(hours=1)
    good_start = window_start + timedelta(minutes=15)
    good_end = good_start + timedelta(minutes=30)
    # Only c1 is available; c2 has no availability → bundle window not offered.
    _available(c1, good_start, good_end)
    bundle = _make_bundle(organization, [c1, c2])

    proposals = service.find_bookable_slots_for_calendar(
        calendar_id=bundle.id,
        search_window_start=window_start,
        search_window_end=window_start + timedelta(hours=1),
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=15),
    )
    assert proposals == []


@pytest.mark.django_db
def test_empty_bundle_returns_empty(service, organization):
    bundle = _calendar(organization, managed=False, calendar_type=CalendarType.BUNDLE)
    now = timezone.now().replace(microsecond=0)
    proposals = service.find_bookable_slots_for_calendar(
        calendar_id=bundle.id,
        search_window_start=now,
        search_window_end=now + timedelta(hours=2),
        duration=timedelta(minutes=30),
    )
    assert proposals == []


# ---------------------------------------------------------------------------
# Empty window / step >= window
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_empty_window_returns_empty(service, organization):
    cal = _calendar(organization, managed=True)
    now = timezone.now().replace(microsecond=0)
    proposals = service.find_bookable_slots_for_calendar(
        calendar_id=cal.id,
        search_window_start=now,
        search_window_end=now,  # zero-width window
        duration=timedelta(minutes=30),
    )
    assert proposals == []


@pytest.mark.django_db
def test_step_larger_than_window_returns_empty(service, organization):
    cal = _calendar(organization, managed=True)
    now = timezone.now().replace(microsecond=0)
    window_start = now + timedelta(hours=1)
    _available(cal, window_start, window_start + timedelta(hours=1))
    # duration (30m) fits, but the window is only 20m wide → no candidate fits.
    proposals = service.find_bookable_slots_for_calendar(
        calendar_id=cal.id,
        search_window_start=window_start,
        search_window_end=window_start + timedelta(minutes=20),
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=15),
    )
    assert proposals == []


@pytest.mark.django_db
def test_invalid_durations_rejected(service, organization):
    cal = _calendar(organization, managed=True)
    now = timezone.now().replace(microsecond=0)
    with pytest.raises(BookableSlotsValidationError):
        service.find_bookable_slots_for_calendar(
            calendar_id=cal.id,
            search_window_start=now,
            search_window_end=now + timedelta(hours=1),
            duration=timedelta(0),
        )
    with pytest.raises(BookableSlotsValidationError):
        service.find_bookable_slots_for_calendar(
            calendar_id=cal.id,
            search_window_start=now,
            search_window_end=now + timedelta(hours=1),
            duration=timedelta(minutes=30),
            slot_step=timedelta(0),
        )


# ---------------------------------------------------------------------------
# No-policy byte-for-byte regression
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_no_policy_matches_one_calendar_group(service, group_service, organization):
    """With no policy anywhere, the service result is byte-for-byte identical to the
    pre-feature reference: ``CalendarGroupService.find_bookable_slots`` for a
    one-calendar group containing the same single calendar."""
    cal = _calendar(organization, managed=False)
    now = timezone.now().replace(microsecond=0)
    window_start = now + timedelta(hours=1)
    window_end = window_start + timedelta(hours=3)
    _event(cal, window_start + timedelta(minutes=30), window_start + timedelta(minutes=90))

    kwargs = dict(
        search_window_start=window_start,
        search_window_end=window_end,
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=15),
    )

    group = _one_calendar_group(group_service, cal)
    group_proposals = group_service.find_bookable_slots(group_id=group.id, **kwargs)
    service_proposals = service.find_bookable_slots_for_calendar(calendar_id=cal.id, **kwargs)

    assert _times(service_proposals) == _times(group_proposals)


# ---------------------------------------------------------------------------
# Lead-time / horizon / buffer rules drop expected candidates
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_lead_time_drops_early_candidates(service, organization):
    cal = _calendar(organization, managed=False)  # always free, no events
    now = timezone.now().replace(microsecond=0)
    create_booking_policy(calendar=cal, lead_time_seconds=int(timedelta(hours=2).total_seconds()))

    window_start = now
    window_end = now + timedelta(hours=4)
    proposals = service.find_bookable_slots_for_calendar(
        calendar_id=cal.id,
        search_window_start=window_start,
        search_window_end=window_end,
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=30),
        now=now,
    )
    # Every kept slot must start at or after now + 2h.
    cutoff = now + timedelta(hours=2)
    assert proposals
    assert all(p.start_time >= cutoff for p in proposals)
    # The candidate at exactly now+2h is present (inclusive boundary).
    assert any(p.start_time == cutoff for p in proposals)


@pytest.mark.django_db
def test_max_horizon_drops_far_candidates(service, organization):
    cal = _calendar(organization, managed=False)
    now = timezone.now().replace(microsecond=0)
    create_booking_policy(calendar=cal, max_horizon_seconds=int(timedelta(hours=2).total_seconds()))

    proposals = service.find_bookable_slots_for_calendar(
        calendar_id=cal.id,
        search_window_start=now,
        search_window_end=now + timedelta(hours=4),
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=30),
        now=now,
    )
    horizon = now + timedelta(hours=2)
    assert proposals
    assert all(p.start_time <= horizon for p in proposals)
    assert any(p.start_time == horizon for p in proposals)


@pytest.mark.django_db
def test_buffer_before_drops_candidates_before_event(service, organization):
    # Event-envelope: buffer_before protects time BEFORE the event. The event's
    # dead zone is [event_start - buffer_before, event_end], so candidates that end
    # too close before the event start are dropped. Place the event after the
    # candidate window.
    cal = _calendar(organization, managed=False)
    now = timezone.now().replace(microsecond=0)
    base = now + timedelta(hours=1)
    event_start = base + timedelta(hours=1)
    event_end = event_start + timedelta(minutes=30)
    _event(cal, event_start, event_end)
    create_booking_policy(
        calendar=cal, buffer_before_seconds=int(timedelta(minutes=30).total_seconds())
    )

    proposals = service.find_bookable_slots_for_calendar(
        calendar_id=cal.id,
        search_window_start=base,  # candidates strictly before the event
        search_window_end=event_start,
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=15),
        now=now,
    )
    # Dead zone = [event_start - 30m, event_end]. A candidate is kept iff its bare
    # window does not overlap it: i.e. end <= event_start - 30m.
    cutoff = event_start - timedelta(minutes=30)
    assert proposals
    assert all(p.end_time <= cutoff for p in proposals)
    # The candidate ending exactly at the cutoff survives (touching is not overlap).
    assert any(p.end_time == cutoff for p in proposals)


@pytest.mark.django_db
def test_buffer_after_drops_candidates_after_event(service, organization):
    # Event-envelope: buffer_after protects time AFTER the event. The event's dead
    # zone is [event_start, event_end + buffer_after], so candidates that start too
    # soon after the event end are dropped. Place the event before the candidate
    # window.
    cal = _calendar(organization, managed=False)
    now = timezone.now().replace(microsecond=0)
    base = now + timedelta(hours=1)
    event_start = base
    event_end = base + timedelta(minutes=30)
    _event(cal, event_start, event_end)
    create_booking_policy(
        calendar=cal, buffer_after_seconds=int(timedelta(minutes=30).total_seconds())
    )

    proposals = service.find_bookable_slots_for_calendar(
        calendar_id=cal.id,
        search_window_start=event_end,  # candidates strictly after the event
        search_window_end=event_end + timedelta(hours=2),
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=15),
        now=now,
    )
    # Dead zone = [event_start, event_end + 30m]. A candidate is kept iff its bare
    # window does not overlap it: i.e. start >= event_end + 30m.
    cutoff = event_end + timedelta(minutes=30)
    assert proposals
    assert all(p.start_time >= cutoff for p in proposals)
    # The candidate starting exactly at the cutoff survives (touching is not overlap).
    assert any(p.start_time == cutoff for p in proposals)


@pytest.mark.django_db
def test_buffer_scenario_4_asymmetric(service, organization):
    # SPEC acceptance scenario #4: event 14:00-15:00, buffer_before=10m,
    # buffer_after=20m → dead zone 13:50-15:20. The first 30-min slot at/after the
    # event must start at 15:20 (NOT 15:10), and a slot ending exactly at 13:50 is
    # still offered (touching is not overlap).
    cal = _calendar(organization, managed=False)
    # Pin now far in the past so lead/horizon never interfere.
    now = datetime.datetime(2020, 1, 1, 0, 0, tzinfo=datetime.UTC)
    day = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)
    event_start = day.replace(hour=14)
    event_end = day.replace(hour=15)
    _event(cal, event_start, event_end)
    create_booking_policy(
        calendar=cal,
        buffer_before_seconds=int(timedelta(minutes=10).total_seconds()),
        buffer_after_seconds=int(timedelta(minutes=20).total_seconds()),
    )

    # Start at 12:50 so the 15-min grid lands on :05/:20/:35/:50 — both the
    # touching candidate (13:20-13:50) and the first post-event slot (15:20) are
    # reachable.
    proposals = service.find_bookable_slots_for_calendar(
        calendar_id=cal.id,
        search_window_start=day.replace(hour=12, minute=50),
        search_window_end=day.replace(hour=16),
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=15),
        now=now,
    )

    dead_zone_start = day.replace(hour=13, minute=50)
    dead_zone_end = day.replace(hour=15, minute=20)
    # No offered slot overlaps the dead zone [13:50, 15:20].
    assert proposals
    assert all(
        not (p.start_time < dead_zone_end and dead_zone_start < p.end_time) for p in proposals
    )
    # The first slot at/after the event starts at 15:20 (not 15:10).
    post_event = [p for p in proposals if p.start_time >= event_end]
    assert post_event
    assert min(p.start_time for p in post_event) == dead_zone_end
    # A slot ending exactly at 13:50 (start 13:20) is still offered (touching != overlap).
    assert any(
        p.start_time == day.replace(hour=13, minute=20) and p.end_time == dead_zone_start
        for p in proposals
    )


@pytest.mark.django_db
def test_buffer_fetches_managed_blocking_spans(service, organization):
    """A managed calendar normally ignores events; with a buffer the engine must
    still subtract the event via the buffer-envelope overlap."""
    cal = _calendar(organization, managed=True)
    now = timezone.now().replace(microsecond=0)
    window_start = now + timedelta(hours=1)
    window_end = window_start + timedelta(hours=2)
    # Managed calendar: make the whole window available.
    _available(cal, window_start, window_end)
    # But there is an existing event mid-window. Without a buffer the managed path
    # ignores it; with a buffer the candidate overlapping it must be dropped.
    event_start = window_start + timedelta(minutes=30)
    event_end = event_start + timedelta(minutes=30)
    _event(cal, event_start, event_end)

    kwargs = dict(
        search_window_start=window_start,
        search_window_end=window_end,
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=30),
        now=now,
    )

    # No policy → managed path ignores the event entirely.
    no_policy = service.find_bookable_slots_for_calendar(calendar_id=cal.id, **kwargs)
    assert any(p.start_time == event_start for p in no_policy)

    # Add a buffer policy → the event window is now subtracted.
    create_booking_policy(calendar=cal, buffer_before_seconds=1, buffer_after_seconds=1)
    with_buffer = service.find_bookable_slots_for_calendar(calendar_id=cal.id, **kwargs)
    assert all(not (p.start_time < event_end and event_start < p.end_time) for p in with_buffer)
    # The candidate that overlaps the event is gone.
    assert not any(p.start_time == event_start for p in with_buffer)


# ---------------------------------------------------------------------------
# Unit tests — policy filter boundary semantics
# ---------------------------------------------------------------------------


class TestPolicyFilterBoundaries:
    def _proposal(self, start: datetime.datetime, minutes: int = 30) -> BookableSlotProposal:
        return BookableSlotProposal(start_time=start, end_time=start + timedelta(minutes=minutes))

    def test_lead_boundary_inclusive(self):
        now = timezone.now().replace(microsecond=0)
        policy = EffectivePolicy(
            lead_time=timedelta(hours=1),
            max_horizon=None,
            buffer_before=timedelta(0),
            buffer_after=timedelta(0),
        )
        at_cutoff = self._proposal(now + timedelta(hours=1))
        before_cutoff = self._proposal(now + timedelta(minutes=59))
        result = slot_engine.apply_policy_filter([before_cutoff, at_cutoff], policy, now, {})
        assert result == [at_cutoff]

    def test_horizon_boundary_inclusive(self):
        now = timezone.now().replace(microsecond=0)
        policy = EffectivePolicy(
            lead_time=timedelta(0),
            max_horizon=timedelta(hours=2),
            buffer_before=timedelta(0),
            buffer_after=timedelta(0),
        )
        at_horizon = self._proposal(now + timedelta(hours=2))
        past_horizon = self._proposal(now + timedelta(hours=2, seconds=1))
        result = slot_engine.apply_policy_filter([at_horizon, past_horizon], policy, now, {})
        assert result == [at_horizon]

    def test_envelope_touching_blocking_span_allowed(self):
        now = timezone.now().replace(microsecond=0)
        policy = EffectivePolicy(
            lead_time=timedelta(0),
            max_horizon=None,
            buffer_before=timedelta(minutes=15),
            buffer_after=timedelta(0),
        )
        # Event-envelope: the blocking span's dead zone starts at bs - buffer_before.
        # Candidate ends at T; place the span so its dead-zone start = T (i.e. the
        # span begins at T + 15m) → touching, NOT overlap → allowed.
        candidate_start = now + timedelta(hours=1)
        candidate_end = candidate_start + timedelta(minutes=30)
        span_start = candidate_end + timedelta(minutes=15)  # dead zone starts at candidate_end
        spans = {1: [(span_start, span_start + timedelta(minutes=30))]}
        proposal = self._proposal(candidate_start)
        result = slot_engine.apply_policy_filter([proposal], policy, now, spans)
        assert result == [proposal]

        # Move the span one second earlier so its dead zone overlaps the candidate → rejected.
        spans_overlap = {
            1: [(span_start - timedelta(seconds=1), span_start + timedelta(minutes=30))]
        }
        result2 = slot_engine.apply_policy_filter([proposal], policy, now, spans_overlap)
        assert result2 == []
