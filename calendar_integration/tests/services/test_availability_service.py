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

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AvailableTime,
    AvailableTimeBulkModification,
    AvailableTimeRecurrenceException,
    BlockedTime,
    BlockedTimeBulkModification,
    BlockedTimeRecurrenceException,
    Calendar,
    CalendarEvent,
    ChildrenCalendarRelationship,
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


# ---------------------------------------------------------------------------
# Tests: create_recurring_available_time_exception
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_recurring_available_time_exception_cancels_occurrence(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    managed_calendar: Calendar,
    organization: Organization,
) -> None:
    """Cancelling a recurring available-time occurrence creates an AvailableTimeRecurrenceException."""
    service = make_service(context, recurrence_manager, organization=organization)

    rule = RecurrenceRule.from_rrule_string("FREQ=WEEKLY;COUNT=5;BYDAY=MO", organization)
    rule.save()

    parent_start = datetime.datetime(2025, 9, 1, 10, 0, tzinfo=datetime.UTC)  # Monday
    parent_available = AvailableTime.objects.create(
        calendar=managed_calendar,
        start_time_tz_unaware=parent_start,
        end_time_tz_unaware=parent_start + datetime.timedelta(hours=2),
        timezone="UTC",
        organization=organization,
        recurrence_rule=rule,
    )

    # Cancel the second occurrence (one week later)
    exception_date = (parent_start + datetime.timedelta(weeks=1)).date()
    result = service.create_recurring_available_time_exception(
        parent_available_time=parent_available,
        exception_date=exception_date,
        is_cancelled=True,
    )

    # Cancelled exception returns None
    assert result is None
    # An AvailableTimeRecurrenceException should exist marking the cancelled occurrence
    assert AvailableTimeRecurrenceException.objects.filter(
        parent_available_time=parent_available,
        is_cancelled=True,
    ).exists()


@pytest.mark.django_db
def test_create_recurring_available_time_exception_modifies_occurrence(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    managed_calendar: Calendar,
    organization: Organization,
) -> None:
    """Modifying a recurring available-time occurrence creates a modified AvailableTime."""
    service = make_service(context, recurrence_manager, organization=organization)

    rule = RecurrenceRule.from_rrule_string("FREQ=WEEKLY;COUNT=5;BYDAY=TU", organization)
    rule.save()

    parent_start = datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC)  # Tuesday
    parent_available = AvailableTime.objects.create(
        calendar=managed_calendar,
        start_time_tz_unaware=parent_start,
        end_time_tz_unaware=parent_start + datetime.timedelta(hours=2),
        timezone="UTC",
        organization=organization,
        recurrence_rule=rule,
    )

    exception_date = (parent_start + datetime.timedelta(weeks=1)).date()
    modified_start = datetime.datetime(2025, 9, 9, 11, 0, tzinfo=datetime.UTC)
    modified_end = datetime.datetime(2025, 9, 9, 13, 0, tzinfo=datetime.UTC)

    result = service.create_recurring_available_time_exception(
        parent_available_time=parent_available,
        exception_date=exception_date,
        modified_start_time=modified_start,
        modified_end_time=modified_end,
        is_cancelled=False,
    )

    # Modified exception returns the new AvailableTime
    assert result is not None
    assert result.pk is not None
    assert result.start_time == modified_start
    assert result.end_time == modified_end
    # A non-cancelled exception should exist
    assert AvailableTimeRecurrenceException.objects.filter(
        parent_available_time=parent_available,
        is_cancelled=False,
    ).exists()


# ---------------------------------------------------------------------------
# Tests: create_recurring_blocked_time_bulk_modification
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_recurring_blocked_time_bulk_modification_creates_continuation(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """Bulk-modifying a recurring blocked time creates a continuation and a record."""
    service = make_service(context, recurrence_manager, organization=organization)

    # Daily recurring blocked time, COUNT=7 to have enough occurrences to split
    rule = RecurrenceRule.from_rrule_string("FREQ=DAILY;COUNT=7", organization)
    rule.save()

    parent_start = datetime.datetime(2025, 10, 1, 9, 0, tzinfo=datetime.UTC)
    parent_blocked = BlockedTime.objects.create(
        calendar=calendar,
        start_time_tz_unaware=parent_start,
        end_time_tz_unaware=parent_start + datetime.timedelta(hours=1),
        timezone="UTC",
        reason="Daily block",
        external_id="bulk_bt_001",
        organization=organization,
        recurrence_rule=rule,
    )

    # Modify from the 4th occurrence (day 4, index 3)
    modification_start = parent_start + datetime.timedelta(days=3)

    result = service.create_recurring_blocked_time_bulk_modification(
        parent_blocked_time=parent_blocked,
        modification_start_date=modification_start,
        modified_reason="Modified reason",
        is_bulk_cancelled=False,
    )

    # A continuation object is returned
    assert result is not None
    assert result.pk is not None
    assert result.reason == "Modified reason"
    assert result.start_time == modification_start

    # A BlockedTimeBulkModification record should exist for the parent
    parent_blocked.refresh_from_db()
    assert BlockedTimeBulkModification.objects.filter(
        parent_blocked_time=parent_blocked,
        is_bulk_cancelled=False,
    ).exists()


@pytest.mark.django_db
def test_create_recurring_blocked_time_bulk_modification_cancels_series(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """Bulk-cancelling a recurring blocked time from a date records the cancellation."""
    service = make_service(context, recurrence_manager, organization=organization)

    rule = RecurrenceRule.from_rrule_string("FREQ=DAILY;COUNT=5", organization)
    rule.save()

    parent_start = datetime.datetime(2025, 10, 6, 9, 0, tzinfo=datetime.UTC)
    parent_blocked = BlockedTime.objects.create(
        calendar=calendar,
        start_time_tz_unaware=parent_start,
        end_time_tz_unaware=parent_start + datetime.timedelta(hours=1),
        timezone="UTC",
        reason="Cancel block",
        external_id="bulk_bt_cancel_001",
        organization=organization,
        recurrence_rule=rule,
    )

    modification_start = parent_start + datetime.timedelta(days=2)

    result = service.create_recurring_blocked_time_bulk_modification(
        parent_blocked_time=parent_blocked,
        modification_start_date=modification_start,
        is_bulk_cancelled=True,
    )

    # Returns None for a cancellation
    assert result is None
    # A cancellation record must exist
    assert BlockedTimeBulkModification.objects.filter(
        parent_blocked_time=parent_blocked,
        is_bulk_cancelled=True,
    ).exists()


# ---------------------------------------------------------------------------
# Tests: create_recurring_available_time_bulk_modification
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_recurring_available_time_bulk_modification_creates_continuation(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    managed_calendar: Calendar,
    organization: Organization,
) -> None:
    """Bulk-modifying a recurring available time creates a continuation and a record."""
    service = make_service(context, recurrence_manager, organization=organization)

    rule = RecurrenceRule.from_rrule_string("FREQ=DAILY;COUNT=7", organization)
    rule.save()

    parent_start = datetime.datetime(2025, 11, 1, 10, 0, tzinfo=datetime.UTC)
    parent_available = AvailableTime.objects.create(
        calendar=managed_calendar,
        start_time_tz_unaware=parent_start,
        end_time_tz_unaware=parent_start + datetime.timedelta(hours=2),
        timezone="UTC",
        organization=organization,
        recurrence_rule=rule,
    )

    modification_start = parent_start + datetime.timedelta(days=3)

    result = service.create_recurring_available_time_bulk_modification(
        parent_available_time=parent_available,
        modification_start_date=modification_start,
        is_bulk_cancelled=False,
    )

    # A continuation object is returned
    assert result is not None
    assert result.pk is not None
    assert result.start_time == modification_start

    # A AvailableTimeBulkModification record should exist
    parent_available.refresh_from_db()
    assert AvailableTimeBulkModification.objects.filter(
        parent_available_time=parent_available,
        is_bulk_cancelled=False,
    ).exists()


@pytest.mark.django_db
def test_create_recurring_available_time_bulk_modification_cancels_series(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    managed_calendar: Calendar,
    organization: Organization,
) -> None:
    """Bulk-cancelling a recurring available time from a date records the cancellation."""
    service = make_service(context, recurrence_manager, organization=organization)

    rule = RecurrenceRule.from_rrule_string("FREQ=DAILY;COUNT=5", organization)
    rule.save()

    parent_start = datetime.datetime(2025, 11, 6, 10, 0, tzinfo=datetime.UTC)
    parent_available = AvailableTime.objects.create(
        calendar=managed_calendar,
        start_time_tz_unaware=parent_start,
        end_time_tz_unaware=parent_start + datetime.timedelta(hours=2),
        timezone="UTC",
        organization=organization,
        recurrence_rule=rule,
    )

    modification_start = parent_start + datetime.timedelta(days=2)

    result = service.create_recurring_available_time_bulk_modification(
        parent_available_time=parent_available,
        modification_start_date=modification_start,
        is_bulk_cancelled=True,
    )

    assert result is None
    assert AvailableTimeBulkModification.objects.filter(
        parent_available_time=parent_available,
        is_bulk_cancelled=True,
    ).exists()


# ---------------------------------------------------------------------------
# Tests: modify/cancel recurring blocked-time from-date (thin wrappers)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_modify_recurring_blocked_time_from_date_delegates_with_not_cancelled(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """modify_recurring_blocked_time_from_date delegates with is_bulk_cancelled=False."""
    service = make_service(context, recurrence_manager, organization=organization)

    rule = RecurrenceRule.from_rrule_string("FREQ=DAILY;COUNT=6", organization)
    rule.save()

    parent_start = datetime.datetime(2025, 12, 1, 9, 0, tzinfo=datetime.UTC)
    parent_blocked = BlockedTime.objects.create(
        calendar=calendar,
        start_time_tz_unaware=parent_start,
        end_time_tz_unaware=parent_start + datetime.timedelta(hours=1),
        timezone="UTC",
        reason="Original reason",
        external_id="modify_from_date_bt_001",
        organization=organization,
        recurrence_rule=rule,
    )

    modification_start = parent_start + datetime.timedelta(days=2)
    result = service.modify_recurring_blocked_time_from_date(
        parent_blocked_time=parent_blocked,
        modification_start_date=modification_start,
        modified_reason="New reason",
    )

    # A continuation is returned (not None) — confirms is_bulk_cancelled=False
    assert result is not None
    assert result.reason == "New reason"
    assert BlockedTimeBulkModification.objects.filter(
        parent_blocked_time=parent_blocked,
        is_bulk_cancelled=False,
    ).exists()


@pytest.mark.django_db
def test_cancel_recurring_blocked_time_from_date_delegates_with_cancelled(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """cancel_recurring_blocked_time_from_date delegates with is_bulk_cancelled=True."""
    service = make_service(context, recurrence_manager, organization=organization)

    rule = RecurrenceRule.from_rrule_string("FREQ=DAILY;COUNT=5", organization)
    rule.save()

    parent_start = datetime.datetime(2025, 12, 8, 9, 0, tzinfo=datetime.UTC)
    parent_blocked = BlockedTime.objects.create(
        calendar=calendar,
        start_time_tz_unaware=parent_start,
        end_time_tz_unaware=parent_start + datetime.timedelta(hours=1),
        timezone="UTC",
        reason="Cancel this",
        external_id="cancel_from_date_bt_001",
        organization=organization,
        recurrence_rule=rule,
    )

    modification_start = parent_start + datetime.timedelta(days=2)
    # cancel_recurring_blocked_time_from_date returns None (it's a fire-and-forget cancellation)
    service.cancel_recurring_blocked_time_from_date(
        parent_blocked_time=parent_blocked,
        modification_start_date=modification_start,
    )

    assert BlockedTimeBulkModification.objects.filter(
        parent_blocked_time=parent_blocked,
        is_bulk_cancelled=True,
    ).exists()


# ---------------------------------------------------------------------------
# Tests: modify/cancel recurring available-time from-date (thin wrappers)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_modify_recurring_available_time_from_date_delegates_with_not_cancelled(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    managed_calendar: Calendar,
    organization: Organization,
) -> None:
    """modify_recurring_available_time_from_date delegates with is_bulk_cancelled=False."""
    service = make_service(context, recurrence_manager, organization=organization)

    rule = RecurrenceRule.from_rrule_string("FREQ=DAILY;COUNT=6", organization)
    rule.save()

    parent_start = datetime.datetime(2025, 12, 15, 10, 0, tzinfo=datetime.UTC)
    parent_available = AvailableTime.objects.create(
        calendar=managed_calendar,
        start_time_tz_unaware=parent_start,
        end_time_tz_unaware=parent_start + datetime.timedelta(hours=2),
        timezone="UTC",
        organization=organization,
        recurrence_rule=rule,
    )

    modification_start = parent_start + datetime.timedelta(days=2)
    result = service.modify_recurring_available_time_from_date(
        parent_available_time=parent_available,
        modification_start_date=modification_start,
    )

    # A continuation is returned — confirms is_bulk_cancelled=False
    assert result is not None
    assert AvailableTimeBulkModification.objects.filter(
        parent_available_time=parent_available,
        is_bulk_cancelled=False,
    ).exists()


@pytest.mark.django_db
def test_cancel_recurring_available_time_from_date_delegates_with_cancelled(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    managed_calendar: Calendar,
    organization: Organization,
) -> None:
    """cancel_recurring_available_time_from_date delegates with is_bulk_cancelled=True."""
    service = make_service(context, recurrence_manager, organization=organization)

    rule = RecurrenceRule.from_rrule_string("FREQ=DAILY;COUNT=5", organization)
    rule.save()

    parent_start = datetime.datetime(2025, 12, 22, 10, 0, tzinfo=datetime.UTC)
    parent_available = AvailableTime.objects.create(
        calendar=managed_calendar,
        start_time_tz_unaware=parent_start,
        end_time_tz_unaware=parent_start + datetime.timedelta(hours=2),
        timezone="UTC",
        organization=organization,
        recurrence_rule=rule,
    )

    modification_start = parent_start + datetime.timedelta(days=2)
    # cancel_recurring_available_time_from_date returns None (fire-and-forget cancellation)
    service.cancel_recurring_available_time_from_date(
        parent_available_time=parent_available,
        modification_start_date=modification_start,
    )

    assert AvailableTimeBulkModification.objects.filter(
        parent_available_time=parent_available,
        is_bulk_cancelled=True,
    ).exists()


# ---------------------------------------------------------------------------
# Tests: batch_modify_available_times
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_batch_modify_available_times_create_operation(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    managed_calendar: Calendar,
    organization: Organization,
) -> None:
    """batch_modify_available_times with 'create' action persists a new AvailableTime."""
    service = make_service(context, recurrence_manager, organization=organization)

    start = datetime.datetime(2025, 7, 10, 9, 0, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(hours=2)

    result = service.batch_modify_available_times(
        calendar=managed_calendar,
        operations=[
            {
                "action": "create",
                "start_time": start,
                "end_time": end,
                "timezone": "UTC",
            }
        ],
    )

    assert len(result) == 1
    assert result[0].start_time == start
    assert result[0].end_time == end
    assert (
        AvailableTime.objects.filter(
            calendar=managed_calendar,
            organization_id=organization.id,
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_batch_modify_available_times_update_operation(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    managed_calendar: Calendar,
    organization: Organization,
) -> None:
    """batch_modify_available_times with 'update' action modifies an existing AvailableTime."""
    service = make_service(context, recurrence_manager, organization=organization)

    start = datetime.datetime(2025, 7, 11, 9, 0, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(hours=2)
    existing = AvailableTime.objects.create(
        calendar=managed_calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
        organization=organization,
    )

    new_start = datetime.datetime(2025, 7, 11, 10, 0, tzinfo=datetime.UTC)
    new_end = new_start + datetime.timedelta(hours=3)

    result = service.batch_modify_available_times(
        calendar=managed_calendar,
        operations=[
            {
                "action": "update",
                "id": existing.pk,
                "start_time": new_start,
                "end_time": new_end,
                "timezone": "UTC",
            }
        ],
    )

    assert len(result) == 1
    updated = result[0]
    # start_time is a GeneratedField; compare start_time (UTC-aware) directly
    assert updated.start_time == new_start
    assert updated.end_time == new_end
    assert updated.timezone == "UTC"


@pytest.mark.django_db
def test_batch_modify_available_times_delete_operation(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    managed_calendar: Calendar,
    organization: Organization,
) -> None:
    """batch_modify_available_times with 'delete' action removes an existing AvailableTime."""
    service = make_service(context, recurrence_manager, organization=organization)

    start = datetime.datetime(2025, 7, 12, 9, 0, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(hours=2)
    existing = AvailableTime.objects.create(
        calendar=managed_calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
        organization=organization,
    )

    result = service.batch_modify_available_times(
        calendar=managed_calendar,
        operations=[
            {
                "action": "delete",
                "id": existing.pk,
            }
        ],
    )

    assert result == []
    assert not AvailableTime.objects.filter(pk=existing.pk).exists()


@pytest.mark.django_db
def test_batch_modify_available_times_update_missing_id_raises(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    managed_calendar: Calendar,
    organization: Organization,
) -> None:
    """batch_modify_available_times with 'update' on unknown id raises ValueError."""
    service = make_service(context, recurrence_manager, organization=organization)

    with pytest.raises(ValueError, match="not found in this calendar"):
        service.batch_modify_available_times(
            calendar=managed_calendar,
            operations=[
                {
                    "action": "update",
                    "id": 99999999,
                    "start_time": datetime.datetime(2025, 7, 13, 9, 0, tzinfo=datetime.UTC),
                    "end_time": datetime.datetime(2025, 7, 13, 10, 0, tzinfo=datetime.UTC),
                    "timezone": "UTC",
                }
            ],
        )


@pytest.mark.django_db
def test_batch_modify_available_times_delete_missing_id_raises(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    managed_calendar: Calendar,
    organization: Organization,
) -> None:
    """batch_modify_available_times with 'delete' on unknown id raises ValueError."""
    service = make_service(context, recurrence_manager, organization=organization)

    with pytest.raises(ValueError, match="not found in this calendar"):
        service.batch_modify_available_times(
            calendar=managed_calendar,
            operations=[
                {
                    "action": "delete",
                    "id": 99999999,
                }
            ],
        )


@pytest.mark.django_db
def test_batch_modify_available_times_calendar_not_managed_raises(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """batch_modify_available_times raises ValueError when calendar does not manage windows."""
    service = make_service(context, recurrence_manager, organization=organization)

    with pytest.raises(ValueError, match="does not manage available windows"):
        service.batch_modify_available_times(
            calendar=calendar,  # manage_available_windows=False
            operations=[],
        )


# ---------------------------------------------------------------------------
# Tests: bulk_create_availability_windows branches
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_bulk_create_availability_windows_creates_without_rrule(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    managed_calendar: Calendar,
    organization: Organization,
) -> None:
    """bulk_create_availability_windows without rrule_string sets recurrence_rule=None."""
    service = make_service(context, recurrence_manager, organization=organization)

    start = datetime.datetime(2025, 8, 1, 9, 0, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(hours=1)

    result = list(
        service.bulk_create_availability_windows(
            calendar=managed_calendar,
            availability_windows=[(start, end, "UTC", None)],
        )
    )

    assert len(result) == 1
    assert result[0].pk is not None
    assert result[0].recurrence_rule is None


@pytest.mark.django_db
def test_bulk_create_availability_windows_creates_with_rrule(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    managed_calendar: Calendar,
    organization: Organization,
) -> None:
    """bulk_create_availability_windows with rrule_string creates a RecurrenceRule and links it."""
    service = make_service(context, recurrence_manager, organization=organization)

    start = datetime.datetime(2025, 8, 5, 9, 0, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(hours=1)

    result = list(
        service.bulk_create_availability_windows(
            calendar=managed_calendar,
            availability_windows=[(start, end, "UTC", "FREQ=DAILY;COUNT=3")],
        )
    )

    assert len(result) == 1
    assert result[0].pk is not None
    assert result[0].recurrence_rule is not None
    assert result[0].recurrence_rule.count == 3


@pytest.mark.django_db
def test_bulk_create_availability_windows_not_managed_raises(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """bulk_create_availability_windows raises ValueError for unmanaged calendar."""
    service = make_service(context, recurrence_manager, organization=organization)

    start = datetime.datetime(2025, 8, 1, 9, 0, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(hours=1)

    with pytest.raises(ValueError, match="does not manage available windows"):
        list(
            service.bulk_create_availability_windows(
                calendar=calendar,  # manage_available_windows=False
                availability_windows=[(start, end, "UTC", None)],
            )
        )


# ---------------------------------------------------------------------------
# Tests: get_available_times_expanded (recurring available times)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_get_available_times_expanded_recurring_generates_instances(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    managed_calendar: Calendar,
    organization: Organization,
) -> None:
    """A recurring available time generates multiple occurrences in the range."""
    service = make_service(context, recurrence_manager, organization=organization)

    rule = RecurrenceRule.from_rrule_string("FREQ=DAILY;COUNT=5", organization)
    rule.save()

    start = datetime.datetime(2025, 9, 8, 10, 0, tzinfo=datetime.UTC)  # Monday
    end = start + datetime.timedelta(hours=2)
    AvailableTime.objects.create(
        calendar=managed_calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
        organization=organization,
        recurrence_rule=rule,
    )

    range_start = start
    range_end = start + datetime.timedelta(days=10)
    results = service.get_available_times_expanded(
        calendar=managed_calendar,
        start_date=range_start,
        end_date=range_end,
    )

    # COUNT=5 means 5 occurrences total
    assert len(results) == 5


@pytest.mark.django_db
def test_get_available_times_expanded_non_recurring_included(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    managed_calendar: Calendar,
    organization: Organization,
) -> None:
    """Non-recurring available times are returned if they overlap the query range."""
    service = make_service(context, recurrence_manager, organization=organization)

    start = datetime.datetime(2025, 9, 15, 10, 0, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(hours=2)
    at = AvailableTime.objects.create(
        calendar=managed_calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
        organization=organization,
    )

    range_start = start - datetime.timedelta(hours=1)
    range_end = end + datetime.timedelta(hours=1)
    results = service.get_available_times_expanded(
        calendar=managed_calendar,
        start_date=range_start,
        end_date=range_end,
    )

    assert len(results) == 1
    assert results[0].pk == at.pk


# ---------------------------------------------------------------------------
# Tests: get_unavailable_time_windows_in_range — additional branches
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_get_unavailable_time_windows_in_range_with_blocked_time(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """get_unavailable_time_windows_in_range returns blocked-time windows."""
    service = make_service(context, recurrence_manager, events=[], organization=organization)

    start = datetime.datetime(2025, 10, 1, 9, 0, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(hours=1)
    BlockedTime.objects.create(
        calendar=calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
        reason="Blocked",
        external_id="unavail_bt_001",
        organization=organization,
    )

    range_start = start - datetime.timedelta(hours=1)
    range_end = end + datetime.timedelta(hours=1)
    windows = service.get_unavailable_time_windows_in_range(
        calendar=calendar,
        start_datetime=range_start,
        end_datetime=range_end,
    )

    assert len(windows) == 1
    assert windows[0].reason == "blocked_time"
    assert windows[0].start_time == start
    assert windows[0].end_time == end


@pytest.mark.django_db
def test_get_unavailable_time_windows_in_range_with_bundle_events(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """Bundle events are included if the child calendar belongs to a bundle."""
    event_start = datetime.datetime(2025, 10, 5, 14, 0, tzinfo=datetime.UTC)
    event_end = datetime.datetime(2025, 10, 5, 15, 0, tzinfo=datetime.UTC)

    # Create a bundle calendar with our calendar as a child
    bundle_calendar = Calendar.objects.create(
        name="Bundle Calendar",
        external_id="bundle_cal_001",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
        calendar_type=CalendarType.BUNDLE,
    )
    # Use the through model to avoid OrganizationForeignKey lookup issues
    ChildrenCalendarRelationship.objects.create(
        bundle_calendar=bundle_calendar,
        child_calendar=calendar,
        organization=organization,
    )

    # Create a bundle event on the bundle calendar
    CalendarEvent.objects.create(
        calendar=calendar,
        bundle_calendar=bundle_calendar,
        title="Bundle Event",
        external_id="bundle_event_001",
        start_time_tz_unaware=event_start,
        end_time_tz_unaware=event_end,
        timezone="UTC",
        organization=organization,
    )

    # FakeHost returns no events for this calendar specifically
    service = make_service(context, recurrence_manager, events=[], organization=organization)

    range_start = event_start - datetime.timedelta(hours=1)
    range_end = event_end + datetime.timedelta(hours=1)
    windows = service.get_unavailable_time_windows_in_range(
        calendar=calendar,
        start_datetime=range_start,
        end_datetime=range_end,
    )

    # The bundle event should be included as unavailable
    assert any(w.reason == "calendar_event" for w in windows)


# ---------------------------------------------------------------------------
# Tests: get_availability_windows_in_range — managed calendar branch
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_get_availability_windows_in_range_managed_calendar_subtracts_busy(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    managed_calendar: Calendar,
    organization: Organization,
) -> None:
    """Managed calendar: availability windows are available times minus busy intervals."""
    service = make_service(context, recurrence_manager, events=[], organization=organization)

    window_start = datetime.datetime(2025, 10, 10, 8, 0, tzinfo=datetime.UTC)
    window_end = datetime.datetime(2025, 10, 10, 18, 0, tzinfo=datetime.UTC)
    # Create an available-time window for the whole day
    AvailableTime.objects.create(
        calendar=managed_calendar,
        start_time_tz_unaware=window_start,
        end_time_tz_unaware=window_end,
        timezone="UTC",
        organization=organization,
    )

    # Create a blocked time in the middle
    bt_start = datetime.datetime(2025, 10, 10, 11, 0, tzinfo=datetime.UTC)
    bt_end = datetime.datetime(2025, 10, 10, 12, 0, tzinfo=datetime.UTC)
    BlockedTime.objects.create(
        calendar=managed_calendar,
        start_time_tz_unaware=bt_start,
        end_time_tz_unaware=bt_end,
        timezone="UTC",
        reason="Lunch",
        external_id="managed_busy_001",
        organization=organization,
    )

    windows = list(
        service.get_availability_windows_in_range(managed_calendar, window_start, window_end)
    )

    # Should have 2 windows after subtracting the blocked time
    assert len(windows) == 2
    assert windows[0].start_time == window_start
    assert windows[0].end_time == bt_start
    assert windows[1].start_time == bt_end
    assert windows[1].end_time == window_end


@pytest.mark.django_db
def test_get_availability_windows_in_range_no_unavailable_windows_returns_full_range(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """Without busy intervals, the entire date range is returned as a single window."""
    service = make_service(context, recurrence_manager, events=[], organization=organization)

    start = datetime.datetime(2025, 10, 15, 8, 0, tzinfo=datetime.UTC)
    end = datetime.datetime(2025, 10, 15, 18, 0, tzinfo=datetime.UTC)
    windows = list(service.get_availability_windows_in_range(calendar, start, end))

    assert len(windows) == 1
    assert windows[0].start_time == start
    assert windows[0].end_time == end
    assert windows[0].can_book_partially is True


@pytest.mark.django_db
def test_get_availability_windows_in_range_adjacent_busy_intervals(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """Adjacent blocked times that abut produce only the gaps before first and after last."""
    service = make_service(context, recurrence_manager, events=[], organization=organization)

    range_start = datetime.datetime(2025, 10, 20, 8, 0, tzinfo=datetime.UTC)
    range_end = datetime.datetime(2025, 10, 20, 18, 0, tzinfo=datetime.UTC)

    # Two adjacent blocks that cover 10:00-12:00 together
    BlockedTime.objects.create(
        calendar=calendar,
        start_time_tz_unaware=datetime.datetime(2025, 10, 20, 10, 0, tzinfo=datetime.UTC),
        end_time_tz_unaware=datetime.datetime(2025, 10, 20, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        reason="Block A",
        external_id="adj_bt_001",
        organization=organization,
    )
    BlockedTime.objects.create(
        calendar=calendar,
        start_time_tz_unaware=datetime.datetime(2025, 10, 20, 11, 0, tzinfo=datetime.UTC),
        end_time_tz_unaware=datetime.datetime(2025, 10, 20, 12, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        reason="Block B",
        external_id="adj_bt_002",
        organization=organization,
    )

    windows = list(service.get_availability_windows_in_range(calendar, range_start, range_end))

    # Should have 2 windows: 08:00-10:00 and 12:00-18:00
    assert len(windows) == 2
    assert windows[0].end_time == datetime.datetime(2025, 10, 20, 10, 0, tzinfo=datetime.UTC)
    assert windows[1].start_time == datetime.datetime(2025, 10, 20, 12, 0, tzinfo=datetime.UTC)


@pytest.mark.django_db
def test_get_availability_windows_in_range_busy_covers_start(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """Busy interval covering the start of the range leaves only the trailing gap."""
    service = make_service(context, recurrence_manager, events=[], organization=organization)

    range_start = datetime.datetime(2025, 10, 22, 8, 0, tzinfo=datetime.UTC)
    range_end = datetime.datetime(2025, 10, 22, 18, 0, tzinfo=datetime.UTC)

    BlockedTime.objects.create(
        calendar=calendar,
        start_time_tz_unaware=range_start,
        end_time_tz_unaware=datetime.datetime(2025, 10, 22, 12, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        reason="Morning block",
        external_id="start_cover_bt_001",
        organization=organization,
    )

    windows = list(service.get_availability_windows_in_range(calendar, range_start, range_end))

    # Only one window at the end
    assert len(windows) == 1
    assert windows[0].start_time == datetime.datetime(2025, 10, 22, 12, 0, tzinfo=datetime.UTC)
    assert windows[0].end_time == range_end


@pytest.mark.django_db
def test_get_availability_windows_in_range_gap_between_blocks_produces_middle_window(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """When two non-adjacent busy intervals exist the gap between them is an available window."""
    service = make_service(context, recurrence_manager, events=[], organization=organization)

    range_start = datetime.datetime(2025, 11, 3, 8, 0, tzinfo=datetime.UTC)
    range_end = datetime.datetime(2025, 11, 3, 18, 0, tzinfo=datetime.UTC)

    # Block 10-11
    BlockedTime.objects.create(
        calendar=calendar,
        start_time_tz_unaware=datetime.datetime(2025, 11, 3, 10, 0, tzinfo=datetime.UTC),
        end_time_tz_unaware=datetime.datetime(2025, 11, 3, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        reason="Block X",
        external_id="gap_bt_001",
        organization=organization,
    )
    # Block 13-14 (non-adjacent, gap between 11:00 and 13:00)
    BlockedTime.objects.create(
        calendar=calendar,
        start_time_tz_unaware=datetime.datetime(2025, 11, 3, 13, 0, tzinfo=datetime.UTC),
        end_time_tz_unaware=datetime.datetime(2025, 11, 3, 14, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        reason="Block Y",
        external_id="gap_bt_002",
        organization=organization,
    )

    windows = list(service.get_availability_windows_in_range(calendar, range_start, range_end))

    # Should be 3 windows: 08-10, 11-13, 14-18
    assert len(windows) == 3
    assert windows[1].start_time == datetime.datetime(2025, 11, 3, 11, 0, tzinfo=datetime.UTC)
    assert windows[1].end_time == datetime.datetime(2025, 11, 3, 13, 0, tzinfo=datetime.UTC)


@pytest.mark.django_db
def test_get_availability_windows_in_range_busy_at_end_no_trailing_window(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """When the busy interval ends at the range boundary there is no trailing available window."""
    service = make_service(context, recurrence_manager, events=[], organization=organization)

    range_start = datetime.datetime(2025, 11, 5, 8, 0, tzinfo=datetime.UTC)
    range_end = datetime.datetime(2025, 11, 5, 18, 0, tzinfo=datetime.UTC)

    # Busy from 12 to end of range
    BlockedTime.objects.create(
        calendar=calendar,
        start_time_tz_unaware=datetime.datetime(2025, 11, 5, 12, 0, tzinfo=datetime.UTC),
        end_time_tz_unaware=range_end,
        timezone="UTC",
        reason="Block to end",
        external_id="end_bt_001",
        organization=organization,
    )

    windows = list(service.get_availability_windows_in_range(calendar, range_start, range_end))

    # Only one window before the block; no trailing window
    assert len(windows) == 1
    assert windows[0].start_time == range_start
    assert windows[0].end_time == datetime.datetime(2025, 11, 5, 12, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Tests: create_recurring_blocked_time_exception — modify path and first-occurrence
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_recurring_blocked_time_exception_modifies_future_occurrence(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """Modifying a future recurring blocked-time occurrence creates a modified BlockedTime."""
    service = make_service(context, recurrence_manager, organization=organization)

    rule = RecurrenceRule.from_rrule_string("FREQ=WEEKLY;COUNT=5;BYDAY=WE", organization)
    rule.save()

    parent_start = datetime.datetime(2026, 1, 7, 9, 0, tzinfo=datetime.UTC)  # Wednesday
    parent_blocked = BlockedTime.objects.create(
        calendar=calendar,
        start_time_tz_unaware=parent_start,
        end_time_tz_unaware=parent_start + datetime.timedelta(hours=1),
        timezone="UTC",
        reason="Weekly block",
        external_id="modify_bt_001",
        organization=organization,
        recurrence_rule=rule,
    )

    # Modify the second occurrence (one week later)
    exception_date = (parent_start + datetime.timedelta(weeks=1)).date()
    modified_start = datetime.datetime(2026, 1, 14, 10, 0, tzinfo=datetime.UTC)
    modified_end = datetime.datetime(2026, 1, 14, 11, 30, tzinfo=datetime.UTC)

    result = service.create_recurring_blocked_time_exception(
        parent_blocked_time=parent_blocked,
        exception_date=exception_date,
        modified_start_time=modified_start,
        modified_end_time=modified_end,
        is_cancelled=False,
    )

    assert result is not None
    assert result.pk is not None
    assert result.start_time == modified_start
    assert result.end_time == modified_end
    # A non-cancelled exception should exist
    assert BlockedTimeRecurrenceException.objects.filter(
        parent_blocked_time=parent_blocked,
        is_cancelled=False,
    ).exists()


@pytest.mark.django_db
def test_create_recurring_blocked_time_exception_third_occurrence(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """Cancelling the third occurrence of a recurring blocked time creates a cancellation record."""
    service = make_service(context, recurrence_manager, organization=organization)

    rule = RecurrenceRule.from_rrule_string("FREQ=WEEKLY;COUNT=5;BYDAY=TH", organization)
    rule.save()

    parent_start = datetime.datetime(2026, 1, 8, 9, 0, tzinfo=datetime.UTC)  # Thursday
    parent_blocked = BlockedTime.objects.create(
        calendar=calendar,
        start_time_tz_unaware=parent_start,
        end_time_tz_unaware=parent_start + datetime.timedelta(hours=1),
        timezone="UTC",
        reason="Thursday block",
        external_id="third_occur_bt_001",
        organization=organization,
        recurrence_rule=rule,
    )

    # Cancel the THIRD occurrence (two weeks later)
    exception_date = (parent_start + datetime.timedelta(weeks=2)).date()
    result = service.create_recurring_blocked_time_exception(
        parent_blocked_time=parent_blocked,
        exception_date=exception_date,
        is_cancelled=True,
    )

    assert result is None
    assert BlockedTimeRecurrenceException.objects.filter(
        parent_blocked_time=parent_blocked,
        is_cancelled=True,
    ).exists()


# ---------------------------------------------------------------------------
# Tests: batch_modify_available_times — partial update (only some fields changed)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_batch_modify_available_times_update_only_timezone(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    managed_calendar: Calendar,
    organization: Organization,
) -> None:
    """batch_modify_available_times update can change only the timezone field."""
    service = make_service(context, recurrence_manager, organization=organization)

    start = datetime.datetime(2025, 7, 20, 9, 0, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(hours=1)
    existing = AvailableTime.objects.create(
        calendar=managed_calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
        organization=organization,
    )

    result = service.batch_modify_available_times(
        calendar=managed_calendar,
        operations=[
            {
                "action": "update",
                "id": existing.pk,
                "timezone": "America/Sao_Paulo",
            }
        ],
    )

    assert len(result) == 1
    assert result[0].timezone == "America/Sao_Paulo"
    # start/end unchanged
    assert result[0].start_time_tz_unaware == existing.start_time_tz_unaware


@pytest.mark.django_db
def test_batch_modify_available_times_update_with_rrule_string(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    managed_calendar: Calendar,
    organization: Organization,
) -> None:
    """batch_modify_available_times update with rrule_string creates a new RecurrenceRule."""
    service = make_service(context, recurrence_manager, organization=organization)

    start = datetime.datetime(2025, 7, 25, 9, 0, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(hours=1)
    existing = AvailableTime.objects.create(
        calendar=managed_calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
        organization=organization,
    )
    # No recurrence rule yet
    assert existing.recurrence_rule is None

    result = service.batch_modify_available_times(
        calendar=managed_calendar,
        operations=[
            {
                "action": "update",
                "id": existing.pk,
                "rrule_string": "FREQ=DAILY;COUNT=3",
            }
        ],
    )

    assert len(result) == 1
    assert result[0].recurrence_rule is not None
    assert result[0].recurrence_rule.count == 3


@pytest.mark.django_db
def test_batch_modify_available_times_update_only_start_time(
    context: CalendarServiceContext,
    recurrence_manager: RecurrenceManager,
    managed_calendar: Calendar,
    organization: Organization,
) -> None:
    """batch_modify_available_times update with only start_time (no timezone key) covers False branch."""
    service = make_service(context, recurrence_manager, organization=organization)

    start = datetime.datetime(2025, 7, 28, 9, 0, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(hours=2)
    existing = AvailableTime.objects.create(
        calendar=managed_calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
        organization=organization,
    )

    new_start = datetime.datetime(2025, 7, 28, 10, 0, tzinfo=datetime.UTC)
    result = service.batch_modify_available_times(
        calendar=managed_calendar,
        operations=[
            {
                "action": "update",
                "id": existing.pk,
                "start_time": new_start,
                # deliberately omit "end_time", "timezone", "rrule_string"
            }
        ],
    )

    assert len(result) == 1
    # Only start_time was updated; timezone and end unchanged
    assert result[0].start_time == new_start
    assert result[0].timezone == "UTC"
