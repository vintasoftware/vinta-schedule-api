"""Grouping by a temporal dimension and a scalar one at the same time.

The property under test is that the result is the *non-empty* cross product:
the combinations that have rows, and no others, in one query. A day that only
one calendar used produces one row, not one per calendar with zeros in the
rest.
"""

import datetime
import uuid
from zoneinfo import ZoneInfo

from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import Calendar, CalendarEvent
from common.organization_context import organization_context
from organizations.models import Organization
from public_api.aggregations import (
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    CalendarEventGroupByInput,
    CalendarEventScalarGroupByField,
    CalendarEventTemporalGroupByField,
    CalendarEventTemporalGroupByInput,
    MetricSpec,
    TemporalGranularity,
    build_aggregate_queryset,
    build_group_key,
    resolve_dimensions,
)


pytestmark = pytest.mark.django_db


UTC = ZoneInfo("UTC")
COUNT_METRIC = MetricSpec(alias="count", field_path="id", op=AggregateOp.COUNT)


@pytest.fixture
def org():
    return Organization.objects.create(
        name=f"Multi Dimension Org {uuid.uuid4().hex[:8]}", should_sync_rooms=False
    )


def _make_calendar(org: Organization, label: str) -> Calendar:
    return Calendar.objects.create(
        organization=org,
        name=label,
        external_id=f"{label}-{uuid.uuid4().hex[:8]}",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
    )


def _make_event(org: Organization, calendar: Calendar, utc_start: datetime.datetime):
    return CalendarEvent.objects.create(
        organization=org,
        calendar=calendar,
        title="Visit",
        description="",
        external_id=f"ev-{uuid.uuid4().hex[:12]}",
        start_time_tz_unaware=utc_start,
        end_time_tz_unaware=utc_start + datetime.timedelta(minutes=30),
        timezone="UTC",
    )


def _day_and_calendar_plan() -> AggregateQueryPlan:
    """``[START_TIME:DAY, CALENDAR_ID]``, built the way a resolver will."""
    dimensions = resolve_dimensions(
        [
            CalendarEventGroupByInput(
                temporal=CalendarEventTemporalGroupByInput(
                    field=CalendarEventTemporalGroupByField.START_TIME,
                    granularity=TemporalGranularity.DAY,
                )
            ),
            CalendarEventGroupByInput(field=CalendarEventScalarGroupByField.CALENDAR_ID),
        ],
        "UTC",
    )
    return AggregateQueryPlan(
        entity=AggregatableEntity.CALENDAR_EVENT,
        dimensions=dimensions,
        metrics=(COUNT_METRIC,),
    )


def _midnight(day: int) -> datetime.datetime:
    return datetime.datetime(2026, 10, day, tzinfo=UTC)


@pytest.fixture
def two_calendars_over_three_days(org):
    """Six of the nine day-by-calendar combinations are empty.

    Day 1: first x2, second x1
    Day 2: first x1        (second has nothing)
    Day 3:          second x3
    """
    with organization_context(org):
        first = _make_calendar(org, "First")
        second = _make_calendar(org, "Second")
        _make_event(org, first, datetime.datetime(2026, 10, 1, 9, 0))
        _make_event(org, first, datetime.datetime(2026, 10, 1, 14, 0))
        _make_event(org, second, datetime.datetime(2026, 10, 1, 10, 0))
        _make_event(org, first, datetime.datetime(2026, 10, 2, 9, 0))
        for hour in (8, 9, 10):
            _make_event(org, second, datetime.datetime(2026, 10, 3, hour, 0))
        yield first, second


class TestCrossProduct:
    def test_only_non_empty_combinations_come_back(self, org, two_calendars_over_three_days):
        first, second = two_calendars_over_three_days
        with organization_context(org):
            rows = list(
                build_aggregate_queryset(_day_and_calendar_plan(), CalendarEvent.objects.all())
            )

        assert rows == [
            {"start_time_bucket": _midnight(1), "calendar_id": first.id, "count": 2},
            {"start_time_bucket": _midnight(1), "calendar_id": second.id, "count": 1},
            {"start_time_bucket": _midnight(2), "calendar_id": first.id, "count": 1},
            {"start_time_bucket": _midnight(3), "calendar_id": second.id, "count": 3},
        ]

    def test_the_empty_combinations_are_absent_rather_than_zero(
        self, org, two_calendars_over_three_days
    ):
        first, second = two_calendars_over_three_days
        with organization_context(org):
            rows = list(
                build_aggregate_queryset(_day_and_calendar_plan(), CalendarEvent.objects.all())
            )

        present = {(row["start_time_bucket"], row["calendar_id"]) for row in rows}
        assert (_midnight(2), second.id) not in present
        assert (_midnight(3), first.id) not in present
        # Three days x two calendars would be nine rows if the series were dense.
        assert len(rows) == 4

    def test_the_whole_cross_product_is_one_query(self, org, two_calendars_over_three_days):
        with organization_context(org):
            queryset = build_aggregate_queryset(
                _day_and_calendar_plan(), CalendarEvent.objects.all()
            )
            with CaptureQueriesContext(connection) as captured:
                rows = list(queryset)

        assert len(rows) == 4
        assert len(captured.captured_queries) == 1
        sql = captured.captured_queries[0]["sql"].upper()
        assert "DATE_TRUNC" in sql
        assert "GROUP BY" in sql

    def test_rows_are_ordered_by_every_dimension_in_request_order(
        self, org, two_calendars_over_three_days
    ):
        first, second = two_calendars_over_three_days
        with organization_context(org):
            rows = list(
                build_aggregate_queryset(_day_and_calendar_plan(), CalendarEvent.objects.all())
            )

        keys = [(row["start_time_bucket"], row["calendar_id"]) for row in rows]
        assert keys == sorted(keys)
        assert keys[0] == (_midnight(1), min(first.id, second.id))

    def test_the_counts_add_up_to_the_ungrouped_total(self, org, two_calendars_over_three_days):
        with organization_context(org):
            rows = list(
                build_aggregate_queryset(_day_and_calendar_plan(), CalendarEvent.objects.all())
            )
            total = CalendarEvent.objects.count()

        assert sum(row["count"] for row in rows) == total == 7


class TestGroupKeysFromMultiDimensionRows:
    def test_each_row_maps_onto_a_group_key_carrying_both_dimensions(
        self, org, two_calendars_over_three_days
    ):
        first, _second = two_calendars_over_three_days
        with organization_context(org):
            rows = list(
                build_aggregate_queryset(_day_and_calendar_plan(), CalendarEvent.objects.all())
            )

        key = build_group_key(AggregatableEntity.CALENDAR_EVENT, rows[0])
        assert key.start_time_bucket == _midnight(1)
        assert key.calendar_id == first.id
        # Dimensions this query did not name stay null.
        assert key.appointment_type_id is None
        assert key.timezone is None
        assert key.created_bucket is None
