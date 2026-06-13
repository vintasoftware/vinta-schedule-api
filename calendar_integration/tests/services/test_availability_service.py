"""Unit tests for AvailabilityService.

Tests construct AvailabilityService directly (bypassing CalendarService facade)
using a real CalendarServiceContext plus a lightweight fake host for the three
host-routed concerns (get_calendar_events_expanded, bulk_create_manual_blocked_times,
_create_recurrence_rule_if_needed). All DB-touching tests are marked django_db.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable
from typing import Any
from unittest.mock import MagicMock

import pytest
from allauth.socialaccount.models import SocialAccount

from calendar_integration.constants import CalendarProvider
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    BlockedTimeRecurrenceException,
    Calendar,
    CalendarEvent,
    RecurrenceRule,
)
from calendar_integration.services.availability_service import AvailabilityService
from calendar_integration.services.calendar_service_context import CalendarServiceContext
from calendar_integration.services.recurrence_manager import RecurrenceManager
from organizations.models import Organization
from users.models import Profile, User


# ---------------------------------------------------------------------------
# Fake host
# ---------------------------------------------------------------------------


class FakeHost:
    """Minimal AvailabilityServiceHost used in unit tests.

    Provides controllable implementations for the three concerns routed through
    the host: event reads, blocked-time bulk creation, and recurrence-rule
    creation. Calendar_events and blocked_times injected via constructor so
    individual tests can set expectations.
    """

    def __init__(
        self,
        events: list[CalendarEvent] | None = None,
        organization: Organization | None = None,
    ) -> None:
        self._events: list[CalendarEvent] = events or []
        self._organization = organization

    def get_calendar_events_expanded(
        self,
        calendar: Calendar,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[CalendarEvent]:
        return self._events

    def bulk_create_manual_blocked_times(
        self,
        calendar: Calendar,
        blocked_times: Iterable[tuple[datetime.datetime, datetime.datetime, str, str, str | None]],
    ) -> Iterable[BlockedTime]:
        times_list = list(blocked_times)
        created: list[BlockedTime] = []
        for i, (start_time, end_time, timezone, reason, rrule_string) in enumerate(times_list):
            recurrence_rule = self._create_recurrence_rule_if_needed(rrule_string)
            bt = BlockedTime.objects.create(
                calendar=calendar,
                start_time_tz_unaware=start_time,
                end_time_tz_unaware=end_time,
                timezone=timezone,
                reason=reason,
                external_id=f"fake-host-{start_time.isoformat()}-{i}",
                organization_id=calendar.organization_id,
                recurrence_rule=recurrence_rule,
            )
            created.append(bt)
        return created

    def _create_recurrence_rule_if_needed(self, rrule_string: str | None) -> RecurrenceRule | None:
        if not rrule_string or not self._organization:
            return None
        rule = RecurrenceRule.from_rrule_string(rrule_string, self._organization)
        rule.save()
        return rule


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Test Org")


@pytest.fixture
def user(db: Any) -> User:
    u = User.objects.create_user(email="test_avail@example.com", password="pass")
    Profile.objects.create(user=u)
    return u


@pytest.fixture
def social_account(db: Any, user: User) -> SocialAccount:
    return SocialAccount.objects.create(user=user, provider=CalendarProvider.GOOGLE, uid="99999")


@pytest.fixture
def calendar(db: Any, organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Test Calendar",
        external_id="avail_cal_001",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )


@pytest.fixture
def managed_calendar(db: Any, organization: Organization) -> Calendar:
    """Calendar with manage_available_windows=True for window-subtraction tests."""
    return Calendar.objects.create(
        name="Managed Calendar",
        external_id="avail_cal_managed",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
        manage_available_windows=True,
    )


@pytest.fixture
def context(organization: Organization, user: User) -> CalendarServiceContext:
    return CalendarServiceContext(
        organization=organization,
        user_or_token=user,
        account=None,
        calendar_adapter=None,
        calendar_permission_service=None,
        calendar_side_effects_service=None,
    )


@pytest.fixture
def recurrence_manager() -> RecurrenceManager:
    return RecurrenceManager()


def make_service(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    events: list[CalendarEvent] | None = None,
    organization: Organization | None = None,
) -> AvailabilityService:
    host = FakeHost(events=events, organization=organization or context.organization)
    return AvailabilityService(context=context, recurrence_manager=recurrence_manager, host=host)


# ---------------------------------------------------------------------------
# Tests: create_blocked_time / create_available_time
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_blocked_time_creates_db_row(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """create_blocked_time persists a BlockedTime row."""
    service = make_service(context, recurrence_manager, organization=organization)
    start = datetime.datetime(2025, 7, 1, 9, 0, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(hours=1)

    bt = service.create_blocked_time(
        calendar=calendar,
        start_time=start,
        end_time=end,
        timezone="UTC",
        reason="Focus time",
    )

    assert bt.pk is not None
    assert bt.reason == "Focus time"
    assert BlockedTime.objects.filter(pk=bt.pk, organization_id=organization.id).exists()


@pytest.mark.django_db
def test_create_available_time_creates_db_row(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    managed_calendar: Calendar,
    organization: Organization,
) -> None:
    """create_available_time persists an AvailableTime row."""
    service = make_service(context, recurrence_manager, organization=organization)
    start = datetime.datetime(2025, 7, 1, 10, 0, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(hours=2)

    at = service.create_available_time(
        calendar=managed_calendar,
        start_time=start,
        end_time=end,
        timezone="UTC",
    )

    assert at.pk is not None
    assert AvailableTime.objects.filter(pk=at.pk, organization_id=organization.id).exists()


# ---------------------------------------------------------------------------
# Tests: get_availability_windows_in_range (window subtraction)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_get_availability_windows_in_range_no_busy_time_returns_full_range(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
) -> None:
    """Without blocked times or events, the full range is available."""
    service = make_service(context, recurrence_manager, events=[])
    start = datetime.datetime(2025, 7, 2, 8, 0, tzinfo=datetime.UTC)
    end = datetime.datetime(2025, 7, 2, 18, 0, tzinfo=datetime.UTC)

    windows = list(service.get_availability_windows_in_range(calendar, start, end))

    assert len(windows) == 1
    assert windows[0].start_time == start
    assert windows[0].end_time == end
    assert windows[0].can_book_partially is True


@pytest.mark.django_db
def test_get_availability_windows_in_range_blocked_time_splits_window(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """A blocked time in the middle splits availability into before/after segments."""
    service = make_service(context, recurrence_manager, events=[], organization=organization)
    start = datetime.datetime(2025, 7, 3, 8, 0, tzinfo=datetime.UTC)
    end = datetime.datetime(2025, 7, 3, 18, 0, tzinfo=datetime.UTC)

    # Create a blocked time in the middle (10:00 - 11:00)
    BlockedTime.objects.create(
        calendar=calendar,
        start_time_tz_unaware=datetime.datetime(2025, 7, 3, 10, 0, tzinfo=datetime.UTC),
        end_time_tz_unaware=datetime.datetime(2025, 7, 3, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        reason="Meeting",
        external_id="blocked_split_001",
        organization=organization,
    )

    windows = list(service.get_availability_windows_in_range(calendar, start, end))

    # Should have 2 windows: 08:00-10:00 and 11:00-18:00
    assert len(windows) == 2
    assert windows[0].start_time == start
    assert windows[0].end_time == datetime.datetime(2025, 7, 3, 10, 0, tzinfo=datetime.UTC)
    assert windows[1].start_time == datetime.datetime(2025, 7, 3, 11, 0, tzinfo=datetime.UTC)
    assert windows[1].end_time == end


@pytest.mark.django_db
def test_get_availability_windows_in_range_event_blocks_time(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """A calendar event reduces available windows (injected via FakeHost)."""
    event_start = datetime.datetime(2025, 7, 4, 14, 0, tzinfo=datetime.UTC)
    event_end = datetime.datetime(2025, 7, 4, 15, 0, tzinfo=datetime.UTC)
    # Build a mock event that behaves like a CalendarEvent with start_time/end_time
    mock_event = MagicMock(spec=CalendarEvent)
    mock_event.start_time = event_start
    mock_event.end_time = event_end
    mock_event.id = 999

    service = make_service(context, recurrence_manager, events=[mock_event])
    range_start = datetime.datetime(2025, 7, 4, 8, 0, tzinfo=datetime.UTC)
    range_end = datetime.datetime(2025, 7, 4, 18, 0, tzinfo=datetime.UTC)

    windows = list(service.get_availability_windows_in_range(calendar, range_start, range_end))

    # Should have 2 windows: before and after the event
    assert len(windows) == 2
    assert windows[0].end_time == event_start
    assert windows[1].start_time == event_end


# ---------------------------------------------------------------------------
# Tests: get_blocked_times_expanded
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_get_blocked_times_expanded_returns_non_recurring(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """Non-recurring blocked time is returned when it overlaps the query range."""
    service = make_service(context, recurrence_manager, organization=organization)
    start = datetime.datetime(2025, 7, 7, 9, 0, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(hours=1)

    bt = BlockedTime.objects.create(
        calendar=calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
        reason="Block",
        external_id="expanded_bt_001",
        organization=organization,
    )

    results = service.get_blocked_times_expanded(
        calendar=calendar,
        start_date=start - datetime.timedelta(hours=1),
        end_date=end + datetime.timedelta(hours=1),
    )
    ids = [r.id for r in results]
    assert bt.id in ids


@pytest.mark.django_db
def test_get_blocked_times_expanded_recurring_generates_instances(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """A recurring blocked time generates multiple occurrences in the range."""
    service = make_service(context, recurrence_manager, organization=organization)

    rule = RecurrenceRule.from_rrule_string("FREQ=DAILY;COUNT=5", organization)
    rule.save()

    start = datetime.datetime(2025, 8, 4, 9, 0, tzinfo=datetime.UTC)  # Monday
    end = start + datetime.timedelta(hours=1)
    BlockedTime.objects.create(
        calendar=calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
        reason="Daily block",
        external_id="recurring_bt_001",
        organization=organization,
        recurrence_rule=rule,
    )

    range_start = start
    range_end = start + datetime.timedelta(days=10)
    results = service.get_blocked_times_expanded(
        calendar=calendar,
        start_date=range_start,
        end_date=range_end,
    )

    # 5 occurrences from COUNT=5
    assert len(results) == 5


# ---------------------------------------------------------------------------
# Tests: create_recurring_blocked_time_exception
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_recurring_blocked_time_exception_cancels_occurrence(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """Cancelling a recurring blocked-time occurrence creates a cancellation exception."""
    service = make_service(context, recurrence_manager, organization=organization)

    rule = RecurrenceRule.from_rrule_string("FREQ=WEEKLY;COUNT=5;BYDAY=MO", organization)
    rule.save()

    parent_start = datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC)  # Monday
    parent_blocked = BlockedTime.objects.create(
        calendar=calendar,
        start_time_tz_unaware=parent_start,
        end_time_tz_unaware=parent_start + datetime.timedelta(hours=1),
        timezone="UTC",
        reason="Weekly block",
        external_id="except_bt_001",
        organization=organization,
        recurrence_rule=rule,
    )

    # Cancel the second occurrence (one week later)
    exception_date = (parent_start + datetime.timedelta(weeks=1)).date()
    result = service.create_recurring_blocked_time_exception(
        parent_blocked_time=parent_blocked,
        exception_date=exception_date,
        is_cancelled=True,
    )

    # Cancelled exception returns None
    assert result is None
    # A BlockedTimeRecurrenceException should exist marking the cancelled occurrence
    assert BlockedTimeRecurrenceException.objects.filter(
        parent_blocked_time=parent_blocked,
        is_cancelled=True,
    ).exists()


# ---------------------------------------------------------------------------
# Tests: subtract_busy_intervals (static helper)
# ---------------------------------------------------------------------------


def test_subtract_busy_intervals_no_busy_returns_full_window() -> None:
    """With no busy intervals, the full window is returned."""
    start = datetime.datetime(2025, 7, 1, 8, 0, tzinfo=datetime.UTC)
    end = datetime.datetime(2025, 7, 1, 18, 0, tzinfo=datetime.UTC)

    free = AvailabilityService._subtract_busy_intervals(start, end, [])

    assert free == [(start, end)]


def test_subtract_busy_intervals_fully_covered_returns_empty() -> None:
    """A busy interval covering the whole window returns empty list."""
    start = datetime.datetime(2025, 7, 1, 8, 0, tzinfo=datetime.UTC)
    end = datetime.datetime(2025, 7, 1, 18, 0, tzinfo=datetime.UTC)

    free = AvailabilityService._subtract_busy_intervals(start, end, [(start, end)])

    assert free == []


def test_subtract_busy_intervals_splits_correctly() -> None:
    """Two non-overlapping busy intervals split the window into three free slots."""
    window_start = datetime.datetime(2025, 7, 1, 8, 0, tzinfo=datetime.UTC)
    window_end = datetime.datetime(2025, 7, 1, 18, 0, tzinfo=datetime.UTC)
    busy1_start = datetime.datetime(2025, 7, 1, 10, 0, tzinfo=datetime.UTC)
    busy1_end = datetime.datetime(2025, 7, 1, 11, 0, tzinfo=datetime.UTC)
    busy2_start = datetime.datetime(2025, 7, 1, 13, 0, tzinfo=datetime.UTC)
    busy2_end = datetime.datetime(2025, 7, 1, 14, 0, tzinfo=datetime.UTC)

    free = AvailabilityService._subtract_busy_intervals(
        window_start, window_end, [(busy1_start, busy1_end), (busy2_start, busy2_end)]
    )

    assert len(free) == 3
    assert free[0] == (window_start, busy1_start)
    assert free[1] == (busy1_end, busy2_start)
    assert free[2] == (busy2_end, window_end)
