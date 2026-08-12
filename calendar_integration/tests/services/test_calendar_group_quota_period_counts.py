"""Integration tests for the quota period-counting Postgres function
(CALENDAR_GROUP_SCOPED_AVAILABILITY Phase 3a):
``calculate_calendar_group_quota_period_counts`` / its JSON wrapper
``get_calendar_group_quota_period_counts_json``, exercised through the Django
ORM wrapper ``GetCalendarGroupQuotaPeriodCountsJSON``.

Nothing reads this function in application code yet (that's Phase 3b) -- this
phase's job is to prove the counting primitive itself is correct: day / week /
month bucketing, Monday vs Sunday week starts, cancellation (event row
deleted) freeing the count immediately, a reschedule moving the count to its
new period, and bookings made outside the group never counting.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.database_functions import GetCalendarGroupQuotaPeriodCountsJSON
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
)
from tenancy.models import Organization, WeekStart


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Quota Counting Org", should_sync_rooms=False)


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        organization=organization,
        name="Dr. Reyes",
        external_id="quota-count-cal",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
    )


@pytest.fixture
def group(organization: Organization) -> CalendarGroup:
    return CalendarGroup.objects.create(organization=organization, name="Surgery")


@pytest.fixture
def group_slot(
    organization: Organization, group: CalendarGroup, calendar: Calendar
) -> CalendarGroupSlot:
    slot = CalendarGroupSlot.objects.create(organization=organization, group=group, name="Lead")
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=calendar
    )
    return slot


@pytest.fixture
def other_slot(
    organization: Organization, group: CalendarGroup, calendar: Calendar
) -> CalendarGroupSlot:
    """A second slot in the same group, sharing the same calendar -- proves
    counts are scoped per (calendar, slot), not just per calendar."""
    slot = CalendarGroupSlot.objects.create(organization=organization, group=group, name="Assist")
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=calendar
    )
    return slot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_group_booking(
    organization: Organization,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
    start: datetime.datetime,
    tz: str = "UTC",
    duration_hours: int = 1,
) -> CalendarEvent:
    """A booking made THROUGH `group_slot` for `calendar` -- CalendarEvent plus
    the CalendarEventGroupSelection link, mirroring what
    CalendarGroupService.create_grouped_event writes."""
    end = start + datetime.timedelta(hours=duration_hours)
    event = CalendarEvent.objects.create(
        organization=organization,
        calendar=calendar,
        title="Booking",
        external_id=f"quota-ev-{uuid.uuid4()}",
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone=tz,
    )
    CalendarEventGroupSelection.objects.create(
        organization=organization, event=event, slot=group_slot, calendar=calendar
    )
    return event


def _create_direct_booking(
    organization: Organization,
    calendar: Calendar,
    start: datetime.datetime,
    tz: str = "UTC",
    duration_hours: int = 1,
) -> CalendarEvent:
    """A booking made directly on the calendar, with NO group selection --
    must never count toward any group's quota."""
    end = start + datetime.timedelta(hours=duration_hours)
    return CalendarEvent.objects.create(
        organization=organization,
        calendar=calendar,
        title="Direct booking",
        external_id=f"quota-direct-ev-{uuid.uuid4()}",
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone=tz,
    )


def _quota_counts(
    organization: Organization,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
    period_type: str,
    week_start: str,
    range_start: datetime.datetime,
    range_end: datetime.datetime,
) -> list[dict]:
    row = (
        Calendar.objects.filter_by_organization(organization.id)
        .filter(id=calendar.id)
        .annotate(
            quota_counts=GetCalendarGroupQuotaPeriodCountsJSON(
                "id",
                group_slot.id,
                organization.id,
                period_type,
                week_start,
                range_start,
                range_end,
            )
        )
        .values_list("quota_counts", flat=True)
        .first()
    )
    return list(row) if row else []


def _utc(y: int, m: int, d: int, h: int = 9) -> datetime.datetime:
    return datetime.datetime(y, m, d, h, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Day period
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_day_period_buckets_by_calendar_day(
    organization: Organization, calendar: Calendar, group_slot: CalendarGroupSlot
) -> None:
    # Two bookings on the same day, one on the next day.
    _create_group_booking(organization, calendar, group_slot, _utc(2026, 1, 5, 9))
    _create_group_booking(organization, calendar, group_slot, _utc(2026, 1, 5, 14))
    _create_group_booking(organization, calendar, group_slot, _utc(2026, 1, 6, 9))

    counts = _quota_counts(
        organization,
        calendar,
        group_slot,
        period_type="day",
        week_start=WeekStart.MONDAY,
        range_start=_utc(2026, 1, 1, 0),
        range_end=_utc(2026, 1, 10, 0),
    )

    by_day = {c["period_start"][:10]: c["booking_count"] for c in counts}
    assert by_day == {"2026-01-05": 2, "2026-01-06": 1}


# ---------------------------------------------------------------------------
# Month period
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_month_period_buckets_by_calendar_month(
    organization: Organization, calendar: Calendar, group_slot: CalendarGroupSlot
) -> None:
    _create_group_booking(organization, calendar, group_slot, _utc(2026, 1, 5))
    _create_group_booking(organization, calendar, group_slot, _utc(2026, 1, 28))
    _create_group_booking(organization, calendar, group_slot, _utc(2026, 2, 3))

    counts = _quota_counts(
        organization,
        calendar,
        group_slot,
        period_type="month",
        week_start=WeekStart.MONDAY,
        range_start=_utc(2026, 1, 1, 0),
        range_end=_utc(2026, 3, 1, 0),
    )

    by_month = {c["period_start"][:7]: c["booking_count"] for c in counts}
    assert by_month == {"2026-01": 2, "2026-02": 1}


# ---------------------------------------------------------------------------
# Week period -- Monday vs Sunday week start
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_week_period_monday_start_groups_sunday_into_preceding_week(
    organization: Organization, calendar: Calendar, group_slot: CalendarGroupSlot
) -> None:
    """Monday 2026-01-05 and Sunday 2026-01-11 are the same Mon-Sun ISO week --
    with a Monday week start, both bookings fall in one bucket."""
    _create_group_booking(organization, calendar, group_slot, _utc(2026, 1, 5))  # Monday
    _create_group_booking(organization, calendar, group_slot, _utc(2026, 1, 11))  # Sunday

    counts = _quota_counts(
        organization,
        calendar,
        group_slot,
        period_type="week",
        week_start=WeekStart.MONDAY,
        range_start=_utc(2026, 1, 1, 0),
        range_end=_utc(2026, 1, 20, 0),
    )

    assert len(counts) == 1
    assert counts[0]["period_start"][:10] == "2026-01-05"
    assert counts[0]["booking_count"] == 2


@pytest.mark.django_db
def test_week_period_sunday_start_splits_monday_and_sunday_into_different_weeks(
    organization: Organization, calendar: Calendar, group_slot: CalendarGroupSlot
) -> None:
    """Same two bookings as above, but with a Sunday week start: Sunday
    2026-01-11 starts its OWN week, separate from the Monday-2026-01-05
    booking's week (which starts Sunday 2026-01-04 -- the day before Monday
    -- under this setting). Under a Monday week start both bookings land in
    the SAME bucket (the previous test); under a Sunday week start they land
    in DIFFERENT buckets -- the exact edge the plan calls out."""
    _create_group_booking(organization, calendar, group_slot, _utc(2026, 1, 5))  # Monday
    _create_group_booking(organization, calendar, group_slot, _utc(2026, 1, 11))  # Sunday

    counts = _quota_counts(
        organization,
        calendar,
        group_slot,
        period_type="week",
        week_start=WeekStart.SUNDAY,
        range_start=_utc(2025, 12, 25, 0),
        range_end=_utc(2026, 1, 20, 0),
    )

    by_week_start = {c["period_start"][:10]: c["booking_count"] for c in counts}
    assert by_week_start == {"2026-01-04": 1, "2026-01-11": 1}


# ---------------------------------------------------------------------------
# Cancellation frees quota immediately
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_cancelled_booking_excluded(
    organization: Organization, calendar: Calendar, group_slot: CalendarGroupSlot
) -> None:
    kept = _create_group_booking(organization, calendar, group_slot, _utc(2026, 1, 5))
    cancelled = _create_group_booking(organization, calendar, group_slot, _utc(2026, 1, 5, 14))

    # Cancelling a grouped booking deletes the CalendarEvent row (mirrors
    # CalendarGroupService.cancel_grouped_event), which cascades its
    # CalendarEventGroupSelection.
    cancelled.delete()

    counts = _quota_counts(
        organization,
        calendar,
        group_slot,
        period_type="day",
        week_start=WeekStart.MONDAY,
        range_start=_utc(2026, 1, 1, 0),
        range_end=_utc(2026, 1, 10, 0),
    )

    assert len(counts) == 1
    assert counts[0]["booking_count"] == 1
    assert kept.id  # sanity: the surviving booking still exists


# ---------------------------------------------------------------------------
# Reschedule across a period boundary moves the count
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_reschedule_across_period_boundary_moves_the_count(
    organization: Organization, calendar: Calendar, group_slot: CalendarGroupSlot
) -> None:
    booking = _create_group_booking(organization, calendar, group_slot, _utc(2026, 1, 5))

    counts_before = _quota_counts(
        organization,
        calendar,
        group_slot,
        period_type="day",
        week_start=WeekStart.MONDAY,
        range_start=_utc(2026, 1, 1, 0),
        range_end=_utc(2026, 1, 10, 0),
    )
    assert {c["period_start"][:10]: c["booking_count"] for c in counts_before} == {"2026-01-05": 1}

    # Reschedule to a different day -- same event id, same
    # CalendarEventGroupSelection row (it isn't touched), only the time moves.
    booking.start_time_tz_unaware = _utc(2026, 1, 7)
    booking.end_time_tz_unaware = _utc(2026, 1, 7, 1)
    booking.save(update_fields=["start_time_tz_unaware", "end_time_tz_unaware"])

    counts_after = _quota_counts(
        organization,
        calendar,
        group_slot,
        period_type="day",
        week_start=WeekStart.MONDAY,
        range_start=_utc(2026, 1, 1, 0),
        range_end=_utc(2026, 1, 10, 0),
    )

    by_day_after = {c["period_start"][:10]: c["booking_count"] for c in counts_after}
    assert by_day_after == {"2026-01-07": 1}
    assert "2026-01-05" not in by_day_after


# ---------------------------------------------------------------------------
# Bookings outside the group (or in a different slot) are excluded
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_direct_booking_outside_group_excluded(
    organization: Organization, calendar: Calendar, group_slot: CalendarGroupSlot
) -> None:
    _create_group_booking(organization, calendar, group_slot, _utc(2026, 1, 5))
    _create_direct_booking(organization, calendar, _utc(2026, 1, 5, 14))

    counts = _quota_counts(
        organization,
        calendar,
        group_slot,
        period_type="day",
        week_start=WeekStart.MONDAY,
        range_start=_utc(2026, 1, 1, 0),
        range_end=_utc(2026, 1, 10, 0),
    )

    assert len(counts) == 1
    assert counts[0]["booking_count"] == 1


@pytest.mark.django_db
def test_booking_through_a_different_slot_excluded(
    organization: Organization,
    calendar: Calendar,
    group_slot: CalendarGroupSlot,
    other_slot: CalendarGroupSlot,
) -> None:
    """A booking made through `other_slot` (same calendar, same group, different
    slot) must not count toward `group_slot`'s quota."""
    _create_group_booking(organization, calendar, group_slot, _utc(2026, 1, 5))
    _create_group_booking(organization, calendar, other_slot, _utc(2026, 1, 5, 14))

    counts = _quota_counts(
        organization,
        calendar,
        group_slot,
        period_type="day",
        week_start=WeekStart.MONDAY,
        range_start=_utc(2026, 1, 1, 0),
        range_end=_utc(2026, 1, 10, 0),
    )

    assert len(counts) == 1
    assert counts[0]["booking_count"] == 1


@pytest.mark.django_db
def test_search_window_excludes_bookings_outside_it(
    organization: Organization, calendar: Calendar, group_slot: CalendarGroupSlot
) -> None:
    _create_group_booking(organization, calendar, group_slot, _utc(2026, 1, 5))
    _create_group_booking(organization, calendar, group_slot, _utc(2026, 2, 5))  # outside range

    counts = _quota_counts(
        organization,
        calendar,
        group_slot,
        period_type="day",
        week_start=WeekStart.MONDAY,
        range_start=_utc(2026, 1, 1, 0),
        range_end=_utc(2026, 1, 10, 0),
    )

    assert len(counts) == 1
    assert counts[0]["period_start"][:10] == "2026-01-05"


# ---------------------------------------------------------------------------
# Bucketing is done in ONE consistent frame (UTC), regardless of each
# booking's own CalendarEvent.timezone -- varying the per-event timezone must
# never split (or bypass) a quota bucket.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_bookings_with_different_event_timezones_on_same_real_day_are_summed_into_one_bucket(
    organization: Organization, calendar: Calendar, group_slot: CalendarGroupSlot
) -> None:
    """Two bookings at the exact same real UTC instant (2026-01-05T09:00:00Z),
    but with DIFFERENT CalendarEvent.timezone values (UTC and
    America/New_York -- achieved by choosing each event's wall-clock
    start_time_tz_unaware so it converts to the same real UTC instant). If
    bucketing keyed on the event's own timezone (the bug this guards
    against), these would land in two separate buckets purely because of the
    booker-supplied timezone value, silently letting a "1 per day" cap be
    bypassed. Bucketing in one consistent frame (UTC) must sum them into a
    single bucket."""
    _create_group_booking(
        organization,
        calendar,
        group_slot,
        _utc(2026, 1, 5, 9),  # wall clock in UTC -> real instant 2026-01-05T09:00:00Z
        tz="UTC",
    )
    _create_group_booking(
        organization,
        calendar,
        group_slot,
        _utc(2026, 1, 5, 4),  # wall clock in America/New_York (EST, UTC-5) -> same real instant
        tz="America/New_York",
    )

    counts = _quota_counts(
        organization,
        calendar,
        group_slot,
        period_type="day",
        week_start=WeekStart.MONDAY,
        range_start=_utc(2026, 1, 1, 0),
        range_end=_utc(2026, 1, 10, 0),
    )

    assert len(counts) == 1
    assert counts[0]["period_start"][:10] == "2026-01-05"
    assert counts[0]["booking_count"] == 2


# ---------------------------------------------------------------------------
# No bookings -> no buckets, not an error
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_no_bookings_returns_empty(
    organization: Organization, calendar: Calendar, group_slot: CalendarGroupSlot
) -> None:
    counts = _quota_counts(
        organization,
        calendar,
        group_slot,
        period_type="day",
        week_start=WeekStart.MONDAY,
        range_start=_utc(2026, 1, 1, 0),
        range_end=_utc(2026, 1, 10, 0),
    )

    assert counts == []
