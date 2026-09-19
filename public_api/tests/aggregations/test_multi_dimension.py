"""Grouping on a bucket and a scalar dimension at once.

The result is the cross-product of the combinations that actually have rows —
not the full grid — and it is still one query.
"""

import datetime
from zoneinfo import ZoneInfo

from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarProvider
from calendar_integration.models import Calendar, CalendarEvent
from common.organization_context import organization_context
from organizations.models import Organization
from public_api.aggregations.dimensions import (
    CalendarEventGroupByInput,
    CalendarEventGroupKey,
    CalendarEventScalarGroupByField,
    CalendarEventTemporalGroupBy,
    CalendarEventTemporalGroupByField,
    build_group_key,
    resolve_group_by_inputs,
)
from public_api.aggregations.executor import execute_aggregate_plan
from public_api.aggregations.plan import AggregatableEntity, AggregateOp, AggregateQueryPlan
from public_api.aggregations.registry import build_metric
from public_api.aggregations.types import TemporalGranularity


UTC = ZoneInfo("UTC")
EVENT = AggregatableEntity.CALENDAR_EVENT


def _day_then_calendar() -> list[CalendarEventGroupByInput]:
    return [
        CalendarEventGroupByInput(
            temporal=CalendarEventTemporalGroupBy(
                field=CalendarEventTemporalGroupByField.START_TIME,
                granularity=TemporalGranularity.DAY,
            )
        ),
        CalendarEventGroupByInput(scalar=CalendarEventScalarGroupByField.CALENDAR_ID),
    ]


def _make_event(
    organization: Organization,
    calendar: Calendar,
    *,
    external_id: str,
    day: int,
    minutes: int = 30,
) -> CalendarEvent:
    start = datetime.datetime(2026, 3, day, 12, 0)
    return baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        external_id=external_id,
        title=external_id,
        timezone="UTC",
        start_time_tz_unaware=start,
        end_time_tz_unaware=start + datetime.timedelta(minutes=minutes),
    )


@pytest.fixture
def organization() -> Organization:
    return baker.make(Organization, name="Multi Dimension Org")


@pytest.fixture
def calendar_a(organization) -> Calendar:
    return baker.make(
        Calendar,
        organization=organization,
        name="Calendar A",
        external_id="multi-cal-a",
        provider=CalendarProvider.INTERNAL,
    )


@pytest.fixture
def calendar_b(organization) -> Calendar:
    return baker.make(
        Calendar,
        organization=organization,
        name="Calendar B",
        external_id="multi-cal-b",
        provider=CalendarProvider.INTERNAL,
    )


@pytest.fixture
def events(organization, calendar_a, calendar_b) -> None:
    """A deliberately ragged grid: two days, two calendars, three combinations.

    Calendar B has nothing on the 2nd, so ``(2026-03-02, B)`` must be absent
    rather than present with a zero.
    """
    _make_event(organization, calendar_a, external_id="a-2-first", day=2)
    _make_event(organization, calendar_a, external_id="a-2-second", day=2)
    _make_event(organization, calendar_a, external_id="a-3", day=3)
    _make_event(organization, calendar_b, external_id="b-3", day=3)


def _plan(**kwargs) -> tuple[AggregateQueryPlan, tuple]:
    resolved = resolve_group_by_inputs(EVENT, _day_then_calendar(), UTC)
    plan = AggregateQueryPlan(
        entity=EVENT,
        dimensions=tuple(one.dimension for one in resolved),
        metrics=(
            build_metric(EVENT, "count", AggregateOp.COUNT, alias="count"),
            build_metric(EVENT, "duration_minutes", AggregateOp.SUM, alias="duration_sum"),
        ),
        **kwargs,
    )
    return plan, resolved


@pytest.mark.django_db
class TestTwoDimensions:
    def test_only_non_empty_combinations_come_back(
        self, organization, calendar_a, calendar_b, events
    ):
        plan, _ = _plan()

        with organization_context(organization):
            rows = execute_aggregate_plan(plan, CalendarEvent.objects.all())

        second = datetime.datetime(2026, 3, 2, tzinfo=UTC)
        third = datetime.datetime(2026, 3, 3, tzinfo=UTC)
        assert rows == [
            {
                "start_time_day": second,
                "calendar_id": calendar_a.id,
                "count": 2,
                "duration_sum": 60.0,
            },
            {
                "start_time_day": third,
                "calendar_id": calendar_a.id,
                "count": 1,
                "duration_sum": 30.0,
            },
            {
                "start_time_day": third,
                "calendar_id": calendar_b.id,
                "count": 1,
                "duration_sum": 30.0,
            },
        ]
        # Three rows, not the four a dense 2x2 grid would have.
        assert len(rows) == 3

    def test_it_is_still_a_single_grouped_query(self, organization, calendar_a, calendar_b, events):
        plan, _ = _plan()

        with organization_context(organization):
            with CaptureQueriesContext(connection) as captured:
                rows = execute_aggregate_plan(plan, CalendarEvent.objects.all())

        assert len(rows) == 3
        assert len(captured.captured_queries) == 1
        sql = captured.captured_queries[0]["sql"].upper()
        assert "GROUP BY" in sql
        assert "DATE_TRUNC" in sql

    def test_rows_map_onto_a_group_key_carrying_both_dimensions(
        self, organization, calendar_a, calendar_b, events
    ):
        plan, resolved = _plan()

        with organization_context(organization):
            rows = execute_aggregate_plan(plan, CalendarEvent.objects.all())

        keys = [build_group_key(EVENT, resolved, row) for row in rows]

        assert keys[0] == CalendarEventGroupKey(
            calendar_id=calendar_a.id,
            start_time=datetime.datetime(2026, 3, 2, tzinfo=UTC),
        )
        assert all(key.appointment_type_id is None for key in keys)
        assert all(key.end_time is None for key in keys)

    def test_the_bucket_orders_before_the_scalar_dimension(
        self, organization, calendar_a, calendar_b, events
    ):
        """Group-by order is row order, so paging is stable across pages."""
        plan, _ = _plan()

        with organization_context(organization):
            rows = execute_aggregate_plan(plan, CalendarEvent.objects.all())

        assert [(row["start_time_day"], row["calendar_id"]) for row in rows] == sorted(
            (row["start_time_day"], row["calendar_id"]) for row in rows
        )

    def test_limit_takes_a_stable_page_of_the_grid(
        self, organization, calendar_a, calendar_b, events
    ):
        first_page, _ = _plan(limit=2)
        second_page, _ = _plan(limit=2, offset=2)

        with organization_context(organization):
            first = execute_aggregate_plan(first_page, CalendarEvent.objects.all())
            second = execute_aggregate_plan(second_page, CalendarEvent.objects.all())

        assert len(first) == 2
        assert len(second) == 1
        assert [row["calendar_id"] for row in first] == [calendar_a.id, calendar_a.id]
        assert [row["calendar_id"] for row in second] == [calendar_b.id]
