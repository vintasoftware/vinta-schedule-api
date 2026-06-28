"""Integration tests for Phase 7 — policy-aware group slot query.

Covers:
- **Regression (byte-for-byte no-policy guarantee)**: with no BookingPolicy
  anywhere, ``CalendarGroupService.find_bookable_slots`` output is identical to
  the pre-feature output of the same method called WITHOUT a booking_policy_service
  injected (the legacy / pre-Phase-7 code path that returns raw proposals).
- **Group-override policy**: an explicit policy on the group filters by lead-time,
  max-horizon, and buffer.
- **Per-participant policies, no group override**: the most-restrictive combination
  across all participant calendars is applied.
- **Buffer event-envelope on a managed calendar**: a participant's existing event
  creates a dead zone that drops candidates even for the managed calendar path
  (which normally ignores events).
"""

from __future__ import annotations

import datetime
from datetime import timedelta

from django.utils import timezone

import pytest

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.factories import create_booking_policy
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
)
from calendar_integration.services.booking_policy_service import BookingPolicyService
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.dataclasses import (
    BookableSlotProposal,
    CalendarGroupInputData,
    CalendarGroupSlotInputData,
)
from organizations.models import Organization


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Policy Group Org", should_sync_rooms=False)


_counter = 0


def _calendar(org: Organization, *, managed: bool) -> Calendar:
    """Create a Calendar with a unique external_id."""
    global _counter
    _counter += 1
    return Calendar.objects.create(
        organization=org,
        name=f"cal-{_counter}",
        external_id=f"cal-{_counter}",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=managed,
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
        external_id=f"bt-{_counter}",
    )


def _event(calendar: Calendar, start: datetime.datetime, end: datetime.datetime) -> CalendarEvent:
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


def _times(proposals: list[BookableSlotProposal]):
    return [(p.start_time, p.end_time) for p in proposals]


def _service_with_policy(organization: Organization) -> CalendarGroupService:
    """Build a CalendarGroupService with a real BookingPolicyService injected."""
    svc = CalendarGroupService(booking_policy_service=BookingPolicyService())
    svc.initialize(organization=organization)
    return svc


def _service_without_policy(organization: Organization) -> CalendarGroupService:
    """Build a CalendarGroupService without a booking_policy_service (pre-feature path)."""
    svc = CalendarGroupService()
    svc.initialize(organization=organization)
    return svc


def _make_two_slot_group(
    service: CalendarGroupService,
    physician: Calendar,
    room: Calendar,
    *,
    org: Organization,
) -> CalendarGroupInputData:
    """Create a group with two slots — one per calendar."""
    return service.create_group(
        CalendarGroupInputData(
            name="Clinic",
            description="",
            slots=[
                CalendarGroupSlotInputData(
                    name="Physician",
                    calendar_ids=[physician.id],
                    required_count=1,
                    order=0,
                ),
                CalendarGroupSlotInputData(
                    name="Room",
                    calendar_ids=[room.id],
                    required_count=1,
                    order=1,
                ),
            ],
        )
    )


# ---------------------------------------------------------------------------
# Regression: no policy → output is byte-for-byte identical to pre-feature
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_no_policy_output_identical_to_pre_feature(organization):
    """With no BookingPolicy anywhere, the policy-aware service must produce
    the exact same result as the pre-feature service (no booking_policy_service
    injected)."""
    physician = _calendar(organization, managed=True)
    room = _calendar(organization, managed=True)

    now = timezone.now().replace(microsecond=0)
    window_start = now + timedelta(hours=1)
    good_start = window_start + timedelta(minutes=15)
    good_end = good_start + timedelta(minutes=30)
    _available(physician, good_start, good_end)
    _available(room, good_start, good_end)

    pre_feature_service = _service_without_policy(organization)
    policy_aware_service = _service_with_policy(organization)

    group_pre = pre_feature_service.create_group(
        CalendarGroupInputData(
            name="Pre-feature",
            slots=[
                CalendarGroupSlotInputData(
                    name="Physician", calendar_ids=[physician.id], required_count=1, order=0
                ),
                CalendarGroupSlotInputData(
                    name="Room", calendar_ids=[room.id], required_count=1, order=1
                ),
            ],
        )
    )
    group_aware = policy_aware_service.create_group(
        CalendarGroupInputData(
            name="Policy-aware",
            slots=[
                CalendarGroupSlotInputData(
                    name="Physician", calendar_ids=[physician.id], required_count=1, order=0
                ),
                CalendarGroupSlotInputData(
                    name="Room", calendar_ids=[room.id], required_count=1, order=1
                ),
            ],
        )
    )

    kwargs: dict = dict(
        search_window_start=window_start,
        search_window_end=window_start + timedelta(hours=1),
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=15),
    )

    pre_proposals = pre_feature_service.find_bookable_slots(group_id=group_pre.id, **kwargs)
    aware_proposals = policy_aware_service.find_bookable_slots(group_id=group_aware.id, **kwargs)

    assert _times(aware_proposals) == _times(pre_proposals)
    assert _times(aware_proposals) == [(good_start, good_end)]


@pytest.mark.django_db
def test_no_policy_with_unmanaged_calendar_identical(organization):
    """Regression with unmanaged (blocking-span) calendars: no policy → identical."""
    cal = _calendar(organization, managed=False)
    now = timezone.now().replace(microsecond=0)
    window_start = now + timedelta(hours=1)
    # Block the first 30-minute window.
    _blocked(cal, window_start, window_start + timedelta(minutes=30))

    pre_feature_service = _service_without_policy(organization)
    policy_aware_service = _service_with_policy(organization)

    pre_group = pre_feature_service.create_group(
        CalendarGroupInputData(
            name="Pre-feature unmanaged",
            slots=[
                CalendarGroupSlotInputData(
                    name="Only", calendar_ids=[cal.id], required_count=1, order=0
                )
            ],
        )
    )
    aware_group = policy_aware_service.create_group(
        CalendarGroupInputData(
            name="Policy-aware unmanaged",
            slots=[
                CalendarGroupSlotInputData(
                    name="Only", calendar_ids=[cal.id], required_count=1, order=0
                )
            ],
        )
    )

    kwargs: dict = dict(
        search_window_start=window_start,
        search_window_end=window_start + timedelta(hours=1),
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=15),
    )
    pre_proposals = pre_feature_service.find_bookable_slots(group_id=pre_group.id, **kwargs)
    aware_proposals = policy_aware_service.find_bookable_slots(group_id=aware_group.id, **kwargs)

    assert _times(aware_proposals) == _times(pre_proposals)


# ---------------------------------------------------------------------------
# Group-override policy: lead-time, max-horizon, buffer filtering
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_group_policy_lead_time_filters_early_candidates(organization):
    """An explicit group policy with lead_time drops candidates too close to now."""
    service = _service_with_policy(organization)
    cal = _calendar(organization, managed=False)  # always free, no events

    group = service.create_group(
        CalendarGroupInputData(
            name="Group",
            slots=[
                CalendarGroupSlotInputData(
                    name="Only", calendar_ids=[cal.id], required_count=1, order=0
                )
            ],
        )
    )
    create_booking_policy(
        calendar_group=group,
        lead_time_seconds=int(timedelta(hours=2).total_seconds()),
    )

    now = timezone.now().replace(microsecond=0)
    proposals = service.find_bookable_slots(
        group_id=group.id,
        search_window_start=now,
        search_window_end=now + timedelta(hours=4),
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=30),
        now=now,
    )
    cutoff = now + timedelta(hours=2)
    assert proposals
    assert all(p.start_time >= cutoff for p in proposals)
    # The candidate exactly at now + 2h is inclusive.
    assert any(p.start_time == cutoff for p in proposals)


@pytest.mark.django_db
def test_group_policy_max_horizon_filters_far_candidates(organization):
    """An explicit group policy with max_horizon drops candidates beyond the horizon."""
    service = _service_with_policy(organization)
    cal = _calendar(organization, managed=False)

    group = service.create_group(
        CalendarGroupInputData(
            name="Group",
            slots=[
                CalendarGroupSlotInputData(
                    name="Only", calendar_ids=[cal.id], required_count=1, order=0
                )
            ],
        )
    )
    create_booking_policy(
        calendar_group=group,
        max_horizon_seconds=int(timedelta(hours=2).total_seconds()),
    )

    now = timezone.now().replace(microsecond=0)
    proposals = service.find_bookable_slots(
        group_id=group.id,
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
def test_group_policy_buffer_after_drops_candidates_after_event(organization):
    """Buffer-after: a participant's event creates a dead zone that drops candidates
    that start too soon after the event end."""
    service = _service_with_policy(organization)
    cal = _calendar(organization, managed=False)

    group = service.create_group(
        CalendarGroupInputData(
            name="Group",
            slots=[
                CalendarGroupSlotInputData(
                    name="Only", calendar_ids=[cal.id], required_count=1, order=0
                )
            ],
        )
    )
    create_booking_policy(
        calendar_group=group,
        buffer_after_seconds=int(timedelta(minutes=30).total_seconds()),
    )

    now = timezone.now().replace(microsecond=0)
    base = now + timedelta(hours=1)
    event_start = base
    event_end = base + timedelta(minutes=30)
    _event(cal, event_start, event_end)

    proposals = service.find_bookable_slots(
        group_id=group.id,
        search_window_start=event_end,
        search_window_end=event_end + timedelta(hours=2),
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=15),
        now=now,
    )
    # Dead zone: [event_start, event_end + 30m]; candidates starting < event_end + 30m are dropped.
    cutoff = event_end + timedelta(minutes=30)
    assert proposals
    assert all(p.start_time >= cutoff for p in proposals)
    assert any(p.start_time == cutoff for p in proposals)


# ---------------------------------------------------------------------------
# Per-participant policies only (no group-level override)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_per_participant_most_restrictive_applied(organization):
    """When there is no explicit group policy, the most-restrictive combination of
    per-participant policies applies: max(lead_time) from the two calendars."""
    service = _service_with_policy(organization)
    cal_a = _calendar(organization, managed=False)
    cal_b = _calendar(organization, managed=False)

    group = service.create_group(
        CalendarGroupInputData(
            name="Group",
            slots=[
                CalendarGroupSlotInputData(
                    name="Slot A", calendar_ids=[cal_a.id], required_count=1, order=0
                ),
                CalendarGroupSlotInputData(
                    name="Slot B", calendar_ids=[cal_b.id], required_count=1, order=1
                ),
            ],
        )
    )
    # cal_a has lead_time=1h, cal_b has lead_time=2h → combined lead=2h.
    create_booking_policy(calendar=cal_a, lead_time_seconds=int(timedelta(hours=1).total_seconds()))
    create_booking_policy(calendar=cal_b, lead_time_seconds=int(timedelta(hours=2).total_seconds()))

    now = timezone.now().replace(microsecond=0)
    proposals = service.find_bookable_slots(
        group_id=group.id,
        search_window_start=now,
        search_window_end=now + timedelta(hours=4),
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=30),
        now=now,
    )
    # Combined lead = 2h, so all proposals must start >= now + 2h.
    cutoff = now + timedelta(hours=2)
    assert proposals
    assert all(p.start_time >= cutoff for p in proposals)
    assert any(p.start_time == cutoff for p in proposals)


# ---------------------------------------------------------------------------
# Buffer event-envelope for managed calendars
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_group_policy_buffer_managed_calendar_event_creates_dead_zone(organization):
    """A managed calendar normally ignores CalendarEvent/BlockedTime in the availability
    check. But when a buffer policy is active, the buffer-envelope must subtract
    the participant's events — even for managed calendars.
    """
    service = _service_with_policy(organization)
    cal = _calendar(organization, managed=True)  # managed: checked via AvailableTime coverage

    group = service.create_group(
        CalendarGroupInputData(
            name="Group",
            slots=[
                CalendarGroupSlotInputData(
                    name="Only", calendar_ids=[cal.id], required_count=1, order=0
                )
            ],
        )
    )
    create_booking_policy(
        calendar_group=group,
        buffer_before_seconds=1,
        buffer_after_seconds=1,
    )

    now = timezone.now().replace(microsecond=0)
    window_start = now + timedelta(hours=1)
    window_end = window_start + timedelta(hours=2)
    # Managed calendar: the whole window is available.
    _available(cal, window_start, window_end)
    # There is also an event mid-window. Without a buffer the managed path ignores
    # it; with a buffer the candidate overlapping the event's dead zone is dropped.
    event_start = window_start + timedelta(minutes=30)
    event_end = event_start + timedelta(minutes=30)
    _event(cal, event_start, event_end)

    kwargs: dict = dict(
        search_window_start=window_start,
        search_window_end=window_end,
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=30),
        now=now,
    )

    # No policy (pre-feature service): the event is ignored → slot at event_start offered.
    pre_feature_service = _service_without_policy(organization)
    # Need a separate group for the pre-feature service on the same calendar.
    group_pre = pre_feature_service.create_group(
        CalendarGroupInputData(
            name="Pre-feature",
            slots=[
                CalendarGroupSlotInputData(
                    name="Only", calendar_ids=[cal.id], required_count=1, order=0
                )
            ],
        )
    )
    no_policy_proposals = pre_feature_service.find_bookable_slots(group_id=group_pre.id, **kwargs)
    assert any(p.start_time == event_start for p in no_policy_proposals)

    # With buffer policy: the event window is subtracted — no candidate overlapping it.
    with_buffer_proposals = service.find_bookable_slots(group_id=group.id, **kwargs)
    assert all(
        not (p.start_time < event_end and event_start < p.end_time) for p in with_buffer_proposals
    )
    assert not any(p.start_time == event_start for p in with_buffer_proposals)


# ---------------------------------------------------------------------------
# SHOULD-FIX 1: Group-wide buffer suppression for required_count < pool
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_buffer_drops_candidate_when_non_required_calendar_in_dead_zone(organization):
    """Buffer suppression is conservative: a candidate is dropped if ANY participant
    calendar has an event in the buffer dead zone, even if that calendar is not
    counted toward required_count.

    Scenario: one slot with pool=[cal_a, cal_b], required_count=1, buffer_after=30m.
    cal_a is free for the candidate. cal_b has an event that ends 10 minutes before
    the candidate starts — i.e. 10m into the 30m dead zone.  Because the buffer rule
    is "reject if ANY participant would reject", the candidate must be dropped.
    """
    service = _service_with_policy(organization)
    cal_a = _calendar(organization, managed=False)
    cal_b = _calendar(organization, managed=False)

    group = service.create_group(
        CalendarGroupInputData(
            name="Group",
            slots=[
                CalendarGroupSlotInputData(
                    name="Only",
                    calendar_ids=[cal_a.id, cal_b.id],
                    required_count=1,  # only 1 of 2 needed
                    order=0,
                ),
            ],
        )
    )
    create_booking_policy(
        calendar_group=group,
        buffer_after_seconds=int(timedelta(minutes=30).total_seconds()),
    )

    now = timezone.now().replace(microsecond=0)
    # cal_b has an event that ends exactly at event_end.
    # Dead zone = [event_start, event_end + 30m) (half-open).
    # The search window starts at event_end so step-aligned candidates are:
    #   event_end+0m, event_end+15m, event_end+30m, ...
    # Candidates at +0m and +15m are inside the dead zone; +30m is exactly at
    # the dead zone boundary (half-open: not inside) → must appear.
    event_end = now + timedelta(hours=1)
    event_start = event_end - timedelta(minutes=30)
    _event(cal_b, event_start, event_end)

    window_start = event_end  # step-aligned so candidates land at +0m, +15m, +30m, …
    window_end = window_start + timedelta(hours=2)

    proposals = service.find_bookable_slots(
        group_id=group.id,
        search_window_start=window_start,
        search_window_end=window_end,
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=15),
        now=now,
    )
    # Candidates inside the dead zone (event_end+0m, event_end+15m) must NOT appear.
    dead_zone_candidates = [
        p for p in proposals if p.start_time < event_end + timedelta(minutes=30)
    ]
    assert not dead_zone_candidates, (
        f"Candidates inside buffer dead zone must be dropped: {dead_zone_candidates}"
    )
    # The candidate at safe_start = event_end+30m (just outside the dead zone) must appear.
    safe_start = event_end + timedelta(minutes=30)
    assert any(p.start_time == safe_start for p in proposals), (
        f"Expected a candidate at {safe_start} (first slot outside dead zone)"
    )


# ---------------------------------------------------------------------------
# SHOULD-FIX 2: Group override beats participant policies
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_explicit_group_policy_overrides_participant_policies(organization):
    """An explicit group BookingPolicy short-circuits participant policy resolution.

    The group has lead_time=1h; each participant calendar has lead_time=3h.
    resolve_for_group returns the group's 1h policy (step 1 short-circuits).
    The resulting proposals must start >= now+1h, NOT blocked to >= now+3h.
    """
    service = _service_with_policy(organization)
    cal_a = _calendar(organization, managed=False)
    cal_b = _calendar(organization, managed=False)

    group = service.create_group(
        CalendarGroupInputData(
            name="Group",
            slots=[
                CalendarGroupSlotInputData(
                    name="Slot A", calendar_ids=[cal_a.id], required_count=1, order=0
                ),
                CalendarGroupSlotInputData(
                    name="Slot B", calendar_ids=[cal_b.id], required_count=1, order=1
                ),
            ],
        )
    )
    # Group-level policy: 1h lead time.
    create_booking_policy(
        calendar_group=group,
        lead_time_seconds=int(timedelta(hours=1).total_seconds()),
    )
    # Per-participant policies: 3h lead time — more restrictive than the group policy.
    create_booking_policy(calendar=cal_a, lead_time_seconds=int(timedelta(hours=3).total_seconds()))
    create_booking_policy(calendar=cal_b, lead_time_seconds=int(timedelta(hours=3).total_seconds()))

    now = timezone.now().replace(microsecond=0)
    proposals = service.find_bookable_slots(
        group_id=group.id,
        search_window_start=now,
        search_window_end=now + timedelta(hours=5),
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=30),
        now=now,
    )
    # Group policy (1h) wins: candidates from now+1h onward are offered.
    group_cutoff = now + timedelta(hours=1)
    participant_cutoff = now + timedelta(hours=3)
    assert proposals, "Expected at least one candidate after group lead_time=1h"
    # All candidates must be >= 1h (group policy honoured).
    assert all(p.start_time >= group_cutoff for p in proposals)
    # Some candidates between 1h and 3h must be present (participant policies NOT applied).
    assert any(group_cutoff <= p.start_time < participant_cutoff for p in proposals), (
        "Expected candidates between group_cutoff (1h) and participant_cutoff (3h); "
        "participant policies must NOT override the explicit group policy."
    )


# ---------------------------------------------------------------------------
# SHOULD-FIX 3: Managed calendar buffer width test (multi-minute buffer)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_managed_calendar_buffer_width_honored(organization):
    """The buffer WIDTH is honored on the managed-calendar path.

    A managed calendar normally skips CalendarEvent/BlockedTime in the
    availability check.  When a buffer policy is active, events are fetched
    for the buffer computation.  This test uses a 30-minute buffer_after
    and confirms that a candidate OUTSIDE the bare event window but INSIDE
    the dead zone (buffer extension) is dropped — proving the buffer WIDTH
    matters, not just event overlap.
    """
    service = _service_with_policy(organization)
    cal = _calendar(organization, managed=True)

    group = service.create_group(
        CalendarGroupInputData(
            name="Group",
            slots=[
                CalendarGroupSlotInputData(
                    name="Only", calendar_ids=[cal.id], required_count=1, order=0
                )
            ],
        )
    )
    create_booking_policy(
        calendar_group=group,
        buffer_after_seconds=int(timedelta(minutes=30).total_seconds()),  # 30m dead zone
    )

    now = timezone.now().replace(microsecond=0)
    window_start = now + timedelta(hours=1)
    window_end = window_start + timedelta(hours=3)
    # Managed calendar: whole window is available.
    _available(cal, window_start, window_end)

    # Event ends 10 minutes into the search window.
    event_start = window_start - timedelta(minutes=20)
    event_end = window_start + timedelta(minutes=10)  # 10m into the window
    _event(cal, event_start, event_end)

    # Dead zone: [event_start, event_end + 30m] = [now+40m, now+1h40m].
    # The candidate at window_start (now+1h) is inside the dead zone (< now+1h40m).
    dead_zone_end = event_end + timedelta(minutes=30)

    proposals = service.find_bookable_slots(
        group_id=group.id,
        search_window_start=window_start,
        search_window_end=window_end,
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=10),
        now=now,
    )

    # Candidates starting < dead_zone_end must be dropped.
    in_dead_zone = [p for p in proposals if p.start_time < dead_zone_end]
    assert not in_dead_zone, (
        f"Expected no candidates inside the 30m dead zone (before {dead_zone_end}), "
        f"got: {in_dead_zone}"
    )
    # Candidates at or after dead_zone_end should be offered.
    assert any(p.start_time >= dead_zone_end for p in proposals), (
        f"Expected candidates after dead_zone_end={dead_zone_end}"
    )
