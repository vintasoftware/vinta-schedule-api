"""Unit tests for ``CalendarEventService.get_calendar_events_expanded_for_calendars``.

These create calendar events directly in the DB (skipping the adapter) and call
the service through the ``CalendarService`` facade (initialized with
``initialize_without_provider``) — matching the pattern used in
``test_calendar_service.py`` for bundle-calendar expansion tests.
"""

from __future__ import annotations

import datetime
from collections import Counter

import pytest

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    ChildrenCalendarRelationship,
    RecurrenceRule,
)
from calendar_integration.services.calendar_service import CalendarService
from organizations.models import Organization


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db) -> Organization:
    return Organization.objects.create(
        name="Multi-cal Expansion Org",
        should_sync_rooms=False,
    )


@pytest.fixture
def other_organization(db) -> Organization:
    return Organization.objects.create(
        name="Other Org",
        should_sync_rooms=False,
    )


@pytest.fixture
def calendar_a(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Calendar A",
        external_id="multi_cal_a",
        provider=CalendarProvider.INTERNAL,
        organization=organization,
    )


@pytest.fixture
def calendar_b(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Calendar B",
        external_id="multi_cal_b",
        provider=CalendarProvider.INTERNAL,
        organization=organization,
    )


@pytest.fixture
def calendar_other_org(other_organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Other Org Calendar",
        external_id="other_org_cal",
        provider=CalendarProvider.INTERNAL,
        organization=other_organization,
    )


@pytest.fixture
def service(organization: Organization) -> CalendarService:
    """CalendarService initialized without a provider (no OAuth required)."""
    svc = CalendarService()
    svc.initialize_without_provider(organization=organization)
    return svc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

START = datetime.datetime(2025, 7, 1, 0, 0, tzinfo=datetime.UTC)
END = datetime.datetime(2025, 7, 31, 23, 59, tzinfo=datetime.UTC)


def _make_event(
    title: str,
    calendar: Calendar,
    organization: Organization,
    start_offset_days: int = 0,
    *,
    external_id: str | None = None,
    is_bundle_primary: bool = False,
    bundle_calendar: Calendar | None = None,
    bundle_primary_event: CalendarEvent | None = None,
    recurrence_rule: RecurrenceRule | None = None,
) -> CalendarEvent:
    """Create a non-recurring CalendarEvent inside the default range."""
    start = START + datetime.timedelta(days=start_offset_days, hours=10)
    end = start + datetime.timedelta(hours=1)
    return CalendarEvent.objects.create(
        title=title,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
        calendar=calendar,
        organization=organization,
        external_id=external_id or f"ext_{title.replace(' ', '_').lower()}",
        is_bundle_primary=is_bundle_primary,
        bundle_calendar=bundle_calendar,
        bundle_primary_event=bundle_primary_event,
        recurrence_rule=recurrence_rule,
    )


# ---------------------------------------------------------------------------
# (a) events from two distinct calendars are returned together
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_events_from_two_calendars_returned_together(
    service: CalendarService,
    calendar_a: Calendar,
    calendar_b: Calendar,
    organization: Organization,
) -> None:
    """Events on both calendar A and B appear in the combined result."""
    evt_a = _make_event("Event on A", calendar_a, organization, start_offset_days=0)
    evt_b = _make_event("Event on B", calendar_b, organization, start_offset_days=1)

    results = service.get_calendar_events_expanded_for_calendars(
        [calendar_a.id, calendar_b.id], START, END
    )

    result_ids = {e.id for e in results}
    assert evt_a.id in result_ids
    assert evt_b.id in result_ids
    assert len(results) == 2


# ---------------------------------------------------------------------------
# (b) a recurring master on one calendar expands to in-range occurrences
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_recurring_master_expands_to_in_range_occurrences(
    service: CalendarService,
    calendar_a: Calendar,
    organization: Organization,
) -> None:
    """A recurring master event on calendar A yields its occurrences within the window."""
    # Daily for 3 occurrences starting 2025-07-10
    rrule = RecurrenceRule.objects.create(
        frequency="DAILY",
        interval=1,
        count=3,
        organization=organization,
    )
    master_start = datetime.datetime(2025, 7, 10, 9, 0, tzinfo=datetime.UTC)
    master_end = master_start + datetime.timedelta(hours=1)
    CalendarEvent.objects.create(
        title="Daily Recurring",
        start_time_tz_unaware=master_start,
        end_time_tz_unaware=master_end,
        timezone="UTC",
        calendar=calendar_a,
        organization=organization,
        external_id="daily_recurring_master",
        recurrence_rule=rrule,
    )

    results = service.get_calendar_events_expanded_for_calendars([calendar_a.id], START, END)

    # Three daily occurrences: July 10, 11, 12. Assert on a Counter (not a set) so
    # a dropped/duplicated occurrence cannot hide.
    expected = Counter(
        {
            ("Daily Recurring", datetime.datetime(2025, 7, 10, 9, 0, tzinfo=datetime.UTC)): 1,
            ("Daily Recurring", datetime.datetime(2025, 7, 11, 9, 0, tzinfo=datetime.UTC)): 1,
            ("Daily Recurring", datetime.datetime(2025, 7, 12, 9, 0, tzinfo=datetime.UTC)): 1,
        }
    )
    assert Counter((e.title, e.start_time) for e in results) == expected
    assert len(results) == 3


# ---------------------------------------------------------------------------
# (c) dedup: bundle primary + representation on different calendars → one event
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_bundle_event_dedup_across_calendars(
    organization: Organization,
    calendar_a: Calendar,
    calendar_b: Calendar,
) -> None:
    """An event reachable via a bundle calendar's primary and a child representation
    appears exactly once in the result.
    """
    # Set up a bundle calendar with children calendar_a (primary) and calendar_b
    bundle = Calendar.objects.create(
        name="Bundle",
        external_id="bundle_cal",
        provider=CalendarProvider.INTERNAL,
        organization=organization,
        calendar_type=CalendarType.BUNDLE,
    )
    ChildrenCalendarRelationship.objects.create(
        bundle_calendar=bundle,
        child_calendar=calendar_a,
        organization=organization,
        is_primary=True,
    )
    ChildrenCalendarRelationship.objects.create(
        bundle_calendar=bundle,
        child_calendar=calendar_b,
        organization=organization,
        is_primary=False,
    )

    primary_event = _make_event(
        "Primary Bundle Event",
        calendar_a,
        organization,
        start_offset_days=0,
        external_id="bundle_primary",
        is_bundle_primary=True,
        bundle_calendar=bundle,
    )
    # Representation lives on calendar_b — should be dropped
    _make_event(
        "[Bundle] Primary Bundle Event",
        calendar_b,
        organization,
        start_offset_days=0,
        external_id="bundle_repr",
        bundle_calendar=bundle,
        bundle_primary_event=primary_event,
    )

    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    results = service.get_calendar_events_expanded_for_calendars(
        [calendar_a.id, calendar_b.id], START, END
    )

    assert len(results) == 1
    assert results[0].id == primary_event.id


# ---------------------------------------------------------------------------
# (d) empty calendar_ids → [] with no query issued
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_empty_calendar_ids_returns_empty_without_query(
    service: CalendarService,
    calendar_a: Calendar,
    organization: Organization,
    django_assert_num_queries,
) -> None:
    """Passing an empty list returns [] immediately without hitting the DB."""
    _make_event("Should Not Appear", calendar_a, organization)

    with django_assert_num_queries(0):
        results = service.get_calendar_events_expanded_for_calendars([], START, END)

    assert results == []


@pytest.mark.django_db
def test_none_calendar_ids_returns_empty_without_query(
    service: CalendarService,
    calendar_a: Calendar,
    organization: Organization,
    django_assert_num_queries,
) -> None:
    """Passing None returns [] immediately without hitting the DB."""
    _make_event("Should Not Appear", calendar_a, organization)

    with django_assert_num_queries(0):
        results = service.get_calendar_events_expanded_for_calendars(
            None,  # type: ignore[arg-type]
            START,
            END,
        )

    assert results == []


# ---------------------------------------------------------------------------
# (e) events outside the [start, end] range are excluded
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_events_outside_range_excluded(
    service: CalendarService,
    calendar_a: Calendar,
    organization: Organization,
) -> None:
    """Events before start or after end do not appear."""
    before_start = datetime.datetime(2025, 6, 30, 10, 0, tzinfo=datetime.UTC)
    after_end = datetime.datetime(2025, 8, 1, 10, 0, tzinfo=datetime.UTC)
    inside_start = datetime.datetime(2025, 7, 15, 10, 0, tzinfo=datetime.UTC)

    CalendarEvent.objects.create(
        title="Before Range",
        start_time_tz_unaware=before_start,
        end_time_tz_unaware=before_start + datetime.timedelta(hours=1),
        timezone="UTC",
        calendar=calendar_a,
        organization=organization,
        external_id="before_range",
    )
    CalendarEvent.objects.create(
        title="After Range",
        start_time_tz_unaware=after_end,
        end_time_tz_unaware=after_end + datetime.timedelta(hours=1),
        timezone="UTC",
        calendar=calendar_a,
        organization=organization,
        external_id="after_range",
    )
    inside_event = CalendarEvent.objects.create(
        title="Inside Range",
        start_time_tz_unaware=inside_start,
        end_time_tz_unaware=inside_start + datetime.timedelta(hours=1),
        timezone="UTC",
        calendar=calendar_a,
        organization=organization,
        external_id="inside_range",
    )

    results = service.get_calendar_events_expanded_for_calendars([calendar_a.id], START, END)

    assert len(results) == 1
    assert results[0].id == inside_event.id


# ---------------------------------------------------------------------------
# (f) calendars from another org are excluded even when their ids are passed
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_other_org_calendars_excluded_by_org_guard(
    service: CalendarService,
    calendar_a: Calendar,
    calendar_other_org: Calendar,
    organization: Organization,
    other_organization: Organization,
) -> None:
    """Passing an id from another org's calendar returns no events for that calendar."""
    _make_event("My Org Event", calendar_a, organization, start_offset_days=0)
    # Create an event on the other org's calendar
    CalendarEvent.objects.create(
        title="Other Org Event",
        start_time_tz_unaware=START + datetime.timedelta(hours=10),
        end_time_tz_unaware=START + datetime.timedelta(hours=11),
        timezone="UTC",
        calendar=calendar_other_org,
        organization=other_organization,
        external_id="other_org_event",
    )

    # Pass both calendar ids — the other org's calendar must be silently excluded.
    results = service.get_calendar_events_expanded_for_calendars(
        [calendar_a.id, calendar_other_org.id], START, END
    )

    titles = [e.title for e in results]
    assert "My Org Event" in titles
    assert "Other Org Event" not in titles
    assert len(results) == 1


# ---------------------------------------------------------------------------
# Additional: result is sorted by start_time
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_results_sorted_by_start_time(
    service: CalendarService,
    calendar_a: Calendar,
    calendar_b: Calendar,
    organization: Organization,
) -> None:
    """Events from two calendars are returned sorted by start_time ascending."""
    evt_b = _make_event("B first chronologically", calendar_b, organization, start_offset_days=0)
    evt_a = _make_event("A second chronologically", calendar_a, organization, start_offset_days=2)

    results = service.get_calendar_events_expanded_for_calendars(
        [calendar_a.id, calendar_b.id], START, END
    )

    assert len(results) == 2
    assert results[0].id == evt_b.id
    assert results[1].id == evt_a.id


# ---------------------------------------------------------------------------
# (g) two DISTINCT recurring series on two calendars producing occurrences at the
#     SAME start_time → BOTH series survive (regression: dedup collision)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_distinct_recurring_series_same_start_time_not_collapsed(
    service: CalendarService,
    calendar_a: Calendar,
    calendar_b: Calendar,
    organization: Organization,
) -> None:
    """Two distinct daily recurring masters — one on calendar A, one on calendar B —
    configured to fire at the SAME start_time each day must both fully expand. Neither
    series may be silently dropped by the dedup (the generated occurrences have pk=None
    and would otherwise collide on ``(None, start_time)``).
    """
    num_days = 4
    common_start = datetime.datetime(2025, 7, 10, 9, 0, tzinfo=datetime.UTC)

    rrule_a = RecurrenceRule.objects.create(
        frequency="DAILY",
        interval=1,
        count=num_days,
        organization=organization,
    )
    rrule_b = RecurrenceRule.objects.create(
        frequency="DAILY",
        interval=1,
        count=num_days,
        organization=organization,
    )
    CalendarEvent.objects.create(
        title="Series A",
        start_time_tz_unaware=common_start,
        end_time_tz_unaware=common_start + datetime.timedelta(hours=1),
        timezone="UTC",
        calendar=calendar_a,
        organization=organization,
        external_id="series_a_master",
        recurrence_rule=rrule_a,
    )
    CalendarEvent.objects.create(
        title="Series B",
        start_time_tz_unaware=common_start,
        end_time_tz_unaware=common_start + datetime.timedelta(hours=1),
        timezone="UTC",
        calendar=calendar_b,
        organization=organization,
        external_id="series_b_master",
        recurrence_rule=rrule_b,
    )

    results = service.get_calendar_events_expanded_for_calendars(
        [calendar_a.id, calendar_b.id], START, END
    )

    # Both series fire on the same num_days dates at the same time. Assert on a Counter
    # of (title, start_time) so a collision-dropped occurrence is caught.
    expected: Counter[tuple[str, datetime.datetime]] = Counter()
    for day in range(num_days):
        occurrence_start = common_start + datetime.timedelta(days=day)
        expected[("Series A", occurrence_start)] += 1
        expected[("Series B", occurrence_start)] += 1

    assert Counter((e.title, e.start_time) for e in results) == expected
    assert len(results) == 2 * num_days


# ---------------------------------------------------------------------------
# (h) single recurring series via its one owning calendar (keep-all sanity)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_single_recurring_series_via_owning_calendar_full_count(
    service: CalendarService,
    calendar_a: Calendar,
    calendar_b: Calendar,
    organization: Organization,
) -> None:
    """A single recurring series, passed via its one owning calendar (alongside an
    unrelated calendar id), appears the expected number of times — the keep-all path
    for generated occurrences must not under- or over-count.
    """
    num_days = 5
    master_start = datetime.datetime(2025, 7, 10, 9, 0, tzinfo=datetime.UTC)
    rrule = RecurrenceRule.objects.create(
        frequency="DAILY",
        interval=1,
        count=num_days,
        organization=organization,
    )
    CalendarEvent.objects.create(
        title="Solo Series",
        start_time_tz_unaware=master_start,
        end_time_tz_unaware=master_start + datetime.timedelta(hours=1),
        timezone="UTC",
        calendar=calendar_a,
        organization=organization,
        external_id="solo_series_master",
        recurrence_rule=rrule,
    )

    results = service.get_calendar_events_expanded_for_calendars(
        [calendar_a.id, calendar_b.id], START, END
    )

    expected = Counter(
        ("Solo Series", master_start + datetime.timedelta(days=day)) for day in range(num_days)
    )
    assert Counter((e.title, e.start_time) for e in results) == expected
    assert len(results) == num_days


# ---------------------------------------------------------------------------
# (i) calendar_ids passed as a set → works
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_calendar_ids_as_set_is_accepted(
    service: CalendarService,
    calendar_a: Calendar,
    calendar_b: Calendar,
    organization: Organization,
) -> None:
    """``calendar_ids`` may be any iterable, including a set."""
    evt_a = _make_event("Event on A", calendar_a, organization, start_offset_days=0)
    evt_b = _make_event("Event on B", calendar_b, organization, start_offset_days=1)

    results = service.get_calendar_events_expanded_for_calendars(
        {calendar_a.id, calendar_b.id}, START, END
    )

    result_ids = {e.id for e in results}
    assert result_ids == {evt_a.id, evt_b.id}
    assert len(results) == 2
