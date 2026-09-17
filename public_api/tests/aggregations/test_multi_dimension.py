"""Grouping by more than one dimension at once.

``[START_TIME:DAY, CALENDAR_ID]`` is the shape a partner reaches for as soon as
"events per day" gets a second axis, and it is where two things have to hold
that a single-dimension query never tests: the result is the *non-empty* part of
the cross product rather than the whole grid, and mixing a bucketed dimension
with a scalar one stays one query.

Sparse is the load-bearing half. Two calendars over three days is a six-cell
grid; a query that filled it would return six rows, and a client rendering a
table would show zeros the database never said anything about.
"""

import datetime
import zoneinfo
from typing import Any

from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest

from calendar_integration.models import Calendar, CalendarEvent
from common.organization_context import organization_context
from organizations.models import Organization
from public_api.aggregations.dimensions import (
    CalendarEventGroupByInput,
    CalendarEventScalarGroupByField,
    CalendarEventTemporalGroupBy,
    CalendarEventTemporalGroupByField,
    build_group_key,
    dimensions_from_group_by,
)
from public_api.aggregations.executor import build_aggregate_queryset, execute_plan
from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    MetricSpec,
)
from public_api.aggregations.types import TemporalGranularity


pytestmark = pytest.mark.django_db

SAO_PAULO = zoneinfo.ZoneInfo("America/Sao_Paulo")
SAO_PAULO_NAME = "America/Sao_Paulo"

DAY_THEN_CALENDAR = [
    CalendarEventGroupByInput(
        temporal=CalendarEventTemporalGroupBy(
            field=CalendarEventTemporalGroupByField.START_TIME,
            granularity=TemporalGranularity.DAY,
        )
    ),
    CalendarEventGroupByInput(scalar=CalendarEventScalarGroupByField.CALENDAR_ID),
]


def make_event(
    calendar: Calendar, *, title: str, at: datetime.datetime, minutes: int = 30
) -> CalendarEvent:
    """One event starting at ``at``, read as a UTC instant."""
    return CalendarEvent.objects.create(
        organization=calendar.organization,
        calendar_fk=calendar,
        title=title,
        external_id=f"{calendar.pk}-{title}",
        start_time_tz_unaware=at,
        end_time_tz_unaware=at + datetime.timedelta(minutes=minutes),
        timezone="UTC",
    )


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.create(name="Multi-dimension Org")


@pytest.fixture
def calendars(organization: Organization) -> tuple[Calendar, Calendar]:
    with organization_context(organization):
        first = Calendar.objects.create(
            name="Dr. A", organization=organization, external_id=f"{organization.pk}-a"
        )
        second = Calendar.objects.create(
            name="Dr. B", organization=organization, external_id=f"{organization.pk}-b"
        )
    return first, second


def multi_plan(**overrides: Any) -> AggregateQueryPlan:
    """Group events by local day and then by calendar."""
    kwargs: dict[str, Any] = {
        "entity": AggregatableEntity.CALENDAR_EVENT,
        "dimensions": dimensions_from_group_by(
            AggregatableEntity.CALENDAR_EVENT, DAY_THEN_CALENDAR, SAO_PAULO_NAME
        ),
        "metrics": (MetricSpec.row_count(),),
    }
    kwargs.update(overrides)
    return AggregateQueryPlan(**kwargs)


def cells(rows: list[dict[str, Any]]) -> list[tuple]:
    """Rows as ``(local day, calendar id, count)`` triples."""
    return [
        (row["start_time_day"].astimezone(SAO_PAULO).date(), row["calendar_id"], row["count"])
        for row in rows
    ]


class TestTheCrossProductIsOnlyItsNonEmptyPart:
    def test_only_combinations_with_rows_come_back(self, organization, calendars):
        first, second = calendars
        with organization_context(organization):
            # A 2x3 grid with three of its six cells filled.
            make_event(first, title="A-Mar2", at=datetime.datetime(2026, 3, 2, 15, 0))
            make_event(first, title="A-Mar3", at=datetime.datetime(2026, 3, 3, 15, 0))
            make_event(second, title="B-Mar4", at=datetime.datetime(2026, 3, 4, 15, 0))

            rows = execute_plan(multi_plan(), CalendarEvent.objects.all())

        assert cells(rows) == [
            (datetime.date(2026, 3, 2), first.pk, 1),
            (datetime.date(2026, 3, 3), first.pk, 1),
            (datetime.date(2026, 3, 4), second.pk, 1),
        ]
        # Not six. The empty cells are the client's to draw.
        assert len(rows) == 3

    def test_one_day_shared_by_two_calendars_is_two_rows(self, organization, calendars):
        first, second = calendars
        with organization_context(organization):
            make_event(first, title="A-1", at=datetime.datetime(2026, 3, 2, 12, 0))
            make_event(first, title="A-2", at=datetime.datetime(2026, 3, 2, 16, 0))
            make_event(second, title="B-1", at=datetime.datetime(2026, 3, 2, 14, 0))

            rows = execute_plan(multi_plan(), CalendarEvent.objects.all())

        # Same bucket, different calendars: the second dimension splits it.
        assert cells(rows) == [
            (datetime.date(2026, 3, 2), first.pk, 2),
            (datetime.date(2026, 3, 2), second.pk, 1),
        ]

    def test_a_fully_populated_grid_returns_every_cell(self, organization, calendars):
        first, second = calendars
        with organization_context(organization):
            for day in (2, 3):
                make_event(first, title=f"A-{day}", at=datetime.datetime(2026, 3, day, 12, 0))
                make_event(second, title=f"B-{day}", at=datetime.datetime(2026, 3, day, 12, 0))

            rows = execute_plan(multi_plan(), CalendarEvent.objects.all())

        assert cells(rows) == [
            (datetime.date(2026, 3, 2), first.pk, 1),
            (datetime.date(2026, 3, 2), second.pk, 1),
            (datetime.date(2026, 3, 3), first.pk, 1),
            (datetime.date(2026, 3, 3), second.pk, 1),
        ]


class TestTheCallersZoneStillDecidesTheDayAxis:
    def test_a_second_dimension_does_not_disturb_the_bucket_boundary(self, organization, calendars):
        first, _second = calendars
        with organization_context(organization):
            # 2026-03-05 01:00 UTC is the 4th, 22:00 in Sao Paulo.
            make_event(first, title="LateNight", at=datetime.datetime(2026, 3, 5, 1, 0))
            make_event(first, title="NextMorning", at=datetime.datetime(2026, 3, 5, 14, 0))

            rows = execute_plan(multi_plan(), CalendarEvent.objects.all())

        assert cells(rows) == [
            (datetime.date(2026, 3, 4), first.pk, 1),
            (datetime.date(2026, 3, 5), first.pk, 1),
        ]


class TestItStaysOneQuery:
    def test_two_dimensions_and_several_metrics_are_one_round_trip(self, organization, calendars):
        first, second = calendars
        with organization_context(organization):
            make_event(first, title="A-1", at=datetime.datetime(2026, 3, 2, 12, 0), minutes=30)
            make_event(first, title="A-2", at=datetime.datetime(2026, 3, 3, 12, 0), minutes=90)
            make_event(second, title="B-1", at=datetime.datetime(2026, 3, 2, 12, 0), minutes=60)

            plan = multi_plan(
                metrics=(
                    MetricSpec.row_count(),
                    MetricSpec("duration_minutes_sum", "duration_minutes", AggregateOp.SUM),
                    MetricSpec("title_min", "title", AggregateOp.MIN),
                )
            )
            with CaptureQueriesContext(connection) as captured:
                rows = execute_plan(plan, CalendarEvent.objects.all())

        assert len(captured.captured_queries) == 1
        assert rows == [
            {
                "start_time_day": rows[0]["start_time_day"],
                "calendar_id": first.pk,
                "count": 1,
                "duration_minutes_sum": 30.0,
                "title_min": "A-1",
            },
            {
                "start_time_day": rows[1]["start_time_day"],
                "calendar_id": second.pk,
                "count": 1,
                "duration_minutes_sum": 60.0,
                "title_min": "B-1",
            },
            {
                "start_time_day": rows[2]["start_time_day"],
                "calendar_id": first.pk,
                "count": 1,
                "duration_minutes_sum": 90.0,
                "title_min": "A-2",
            },
        ]

    def test_both_dimensions_reach_the_group_by(self, organization, calendars):
        with organization_context(organization):
            sql = str(build_aggregate_queryset(multi_plan(), CalendarEvent.objects.all()).query)

        assert "GROUP BY" in sql
        assert "DATE_TRUNC" in sql.upper()
        assert "America/Sao_Paulo" in sql
        assert "calendar_fk_id" in sql


class TestOrderingIsStableAcrossBothDimensions:
    def test_rows_order_by_the_whole_group_key(self, organization, calendars):
        first, second = calendars
        with organization_context(organization):
            # Inserted so that neither dimension is already sorted.
            make_event(second, title="B-3", at=datetime.datetime(2026, 3, 3, 12, 0))
            make_event(first, title="A-3", at=datetime.datetime(2026, 3, 3, 12, 0))
            make_event(second, title="B-2", at=datetime.datetime(2026, 3, 2, 12, 0))
            make_event(first, title="A-2", at=datetime.datetime(2026, 3, 2, 12, 0))

            rows = execute_plan(multi_plan(), CalendarEvent.objects.all())

        assert cells(rows) == [
            (datetime.date(2026, 3, 2), first.pk, 1),
            (datetime.date(2026, 3, 2), second.pk, 1),
            (datetime.date(2026, 3, 3), first.pk, 1),
            (datetime.date(2026, 3, 3), second.pk, 1),
        ]

    def test_repeated_runs_agree(self, organization, calendars):
        first, second = calendars
        with organization_context(organization):
            for day in (2, 3, 4):
                make_event(first, title=f"A-{day}", at=datetime.datetime(2026, 3, day, 12, 0))
                make_event(second, title=f"B-{day}", at=datetime.datetime(2026, 3, day, 12, 0))

            first_run = cells(execute_plan(multi_plan(), CalendarEvent.objects.all()))
            second_run = cells(execute_plan(multi_plan(), CalendarEvent.objects.all()))

        assert first_run == second_run


class TestTheGroupKeyCarriesBothDimensions:
    def test_a_multi_dimension_row_populates_both_key_fields_and_no_others(
        self, organization, calendars
    ):
        first, _second = calendars
        with organization_context(organization):
            make_event(first, title="A-1", at=datetime.datetime(2026, 3, 2, 12, 0))

            plan = multi_plan()
            rows = execute_plan(plan, CalendarEvent.objects.all())
            key = build_group_key(AggregatableEntity.CALENDAR_EVENT, plan.dimensions, rows[0])

        assert key.calendar_id == first.pk
        assert key.start_time.astimezone(SAO_PAULO).date() == datetime.date(2026, 3, 2)
        assert key.end_time is None
        assert key.appointment_type_id is None
        assert key.timezone is None
        assert key.is_bundle_primary is None
