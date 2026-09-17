"""Temporal bucketing end to end, from a ``groupBy`` entry to real rows.

This is the load-bearing test of the phase. "Events per day" is only a
well-formed question once somebody says *whose* day, because a row stores a
naive wall clock plus its own timezone column and the project's ``TIME_ZONE`` is
UTC. The plan's guiding decision is that the caller names the zone; these tests
are what make that true rather than claimed.

The shape every test here uses: put events either side of a local midnight and
assert that the *same rows* bucket differently under two zones. Only a query
that genuinely applies the caller's zone can satisfy both halves -- an
implementation that quietly used the server's clock passes neither, and one
that used each row's own ``timezone`` column passes the first but not the
second.

Bucketing goes through ``dimensions_from_group_by`` rather than hand-built
specs, so the enum and input layer is exercised on the way in.
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
    dimensions_from_group_by,
)
from public_api.aggregations.executor import build_aggregate_queryset, execute_plan
from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateQueryPlan,
    MetricSpec,
)
from public_api.aggregations.types import TemporalGranularity


pytestmark = pytest.mark.django_db

SAO_PAULO = zoneinfo.ZoneInfo("America/Sao_Paulo")
NEW_YORK = zoneinfo.ZoneInfo("America/New_York")

# Sao Paulo is UTC-3 year round -- Brazil abolished DST in 2019 -- which makes
# it the clean case for "a fixed offset moves the day boundary". New York is
# where a DST transition can actually be asserted; see the DST test below.
SAO_PAULO_NAME = "America/Sao_Paulo"
NEW_YORK_NAME = "America/New_York"
UTC_NAME = "UTC"


def make_event(
    calendar: Calendar, *, title: str, at: datetime.datetime, minutes: int = 30
) -> CalendarEvent:
    """One event starting at ``at``, read as a UTC instant.

    ``timezone="UTC"`` with a naive value means the generated ``start_time``
    column is exactly ``at`` in UTC, so every expectation below can be written
    as a UTC instant and converted by hand.
    """
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
    return Organization.objects.create(name="Bucketing Org")


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    with organization_context(organization):
        return Calendar.objects.create(
            name="Dr. A", organization=organization, external_id=f"{organization.pk}-a"
        )


def bucket_plan(
    field: CalendarEventTemporalGroupByField,
    granularity: TemporalGranularity,
    timezone_name: str,
    **overrides: Any,
) -> AggregateQueryPlan:
    """A plan grouping events by one bucketed temporal dimension."""
    dimensions = dimensions_from_group_by(
        AggregatableEntity.CALENDAR_EVENT,
        [
            CalendarEventGroupByInput(
                temporal=CalendarEventTemporalGroupBy(field=field, granularity=granularity)
            )
        ],
        timezone_name,
    )
    kwargs: dict[str, Any] = {
        "entity": AggregatableEntity.CALENDAR_EVENT,
        "dimensions": dimensions,
        "metrics": (MetricSpec.row_count(),),
    }
    kwargs.update(overrides)
    return AggregateQueryPlan(**kwargs)


def buckets(plan: AggregateQueryPlan, alias: str, tz: zoneinfo.ZoneInfo) -> list[tuple]:
    """Run ``plan`` and read its rows as ``(local date, count)`` pairs."""
    rows = execute_plan(plan, CalendarEvent.objects.all())
    return [(row[alias].astimezone(tz).date(), row["count"]) for row in rows]


class TestADayBucketFollowsTheCallersMidnight:
    """The phase's central claim, asserted from both sides."""

    def test_events_either_side_of_sao_paulo_midnight_split_into_two_days(
        self, organization, calendar
    ):
        with organization_context(organization):
            # 2026-03-05 01:00 UTC is 2026-03-04 22:00 in Sao Paulo.
            make_event(
                calendar,
                title="BeforeMidnight",
                at=datetime.datetime(2026, 3, 5, 1, 0),
            )
            # 2026-03-05 04:00 UTC is 2026-03-05 01:00 in Sao Paulo.
            make_event(
                calendar,
                title="AfterMidnight",
                at=datetime.datetime(2026, 3, 5, 4, 0),
            )

            local = buckets(
                bucket_plan(
                    CalendarEventTemporalGroupByField.START_TIME,
                    TemporalGranularity.DAY,
                    SAO_PAULO_NAME,
                ),
                "start_time_day",
                SAO_PAULO,
            )

        assert local == [
            (datetime.date(2026, 3, 4), 1),
            (datetime.date(2026, 3, 5), 1),
        ]

    def test_the_same_events_bucketed_in_utc_land_in_one_day(self, organization, calendar):
        with organization_context(organization):
            make_event(calendar, title="BeforeMidnight", at=datetime.datetime(2026, 3, 5, 1, 0))
            make_event(calendar, title="AfterMidnight", at=datetime.datetime(2026, 3, 5, 4, 0))

            utc = buckets(
                bucket_plan(
                    CalendarEventTemporalGroupByField.START_TIME,
                    TemporalGranularity.DAY,
                    UTC_NAME,
                ),
                "start_time_day",
                datetime.UTC,
            )

        # Both instants are on 2026-03-05 in UTC. This is the answer a query
        # that ignored the caller's zone would give in both tests.
        assert utc == [(datetime.date(2026, 3, 5), 2)]

    def test_one_result_set_uses_one_clock_whatever_each_row_stores(self, organization, calendar):
        """Rows in different local timezones still bucket on the caller's.

        Each row's ``timezone`` column decides what instant its wall clock
        *is* -- ``convert_naive_utc_to_timezone`` reads the naive value in that
        zone, and ``start_time`` is the generated result. What it does not
        decide is which bucket that instant lands in: that is the caller's zone,
        for every row in the result set.
        """
        with organization_context(organization):
            # Wall clock 01:00 read in UTC -> 2026-03-05 01:00 UTC -> the 4th,
            # 22:00 in Sao Paulo.
            first = make_event(calendar, title="RowInUtc", at=datetime.datetime(2026, 3, 5, 1, 0))
            # Wall clock 15:00 read in Tokyo (UTC+9) -> 2026-03-05 06:00 UTC ->
            # the 5th, 03:00 in Sao Paulo.
            second = make_event(
                calendar, title="RowInTokyo", at=datetime.datetime(2026, 3, 5, 15, 0)
            )
            CalendarEvent.objects.filter(pk=second.pk).update(timezone="Asia/Tokyo")
            assert CalendarEvent.objects.get(pk=first.pk).timezone == "UTC"
            assert CalendarEvent.objects.get(pk=second.pk).start_time == datetime.datetime(
                2026, 3, 5, 6, 0, tzinfo=datetime.UTC
            )

            local = buckets(
                bucket_plan(
                    CalendarEventTemporalGroupByField.START_TIME,
                    TemporalGranularity.DAY,
                    SAO_PAULO_NAME,
                ),
                "start_time_day",
                SAO_PAULO,
            )

        # Two rows whose own zones differ, split on Sao Paulo's midnight -- the
        # one clock the caller named.
        assert local == [
            (datetime.date(2026, 3, 4), 1),
            (datetime.date(2026, 3, 5), 1),
        ]


class TestADstBoundary:
    """The acceptance criterion's named case.

    America/New_York springs forward at 2026-03-08 02:00 local (07:00 UTC), so
    that local day is 23 hours long. A bucket boundary computed with a fixed
    offset instead of a real zone would put the post-transition event in the
    wrong day.
    """

    def test_events_across_a_spring_forward_bucket_by_local_day(self, organization, calendar):
        with organization_context(organization):
            # 2026-03-08 04:00 UTC = 2026-03-07 23:00 EST (UTC-5).
            make_event(calendar, title="SaturdayNight", at=datetime.datetime(2026, 3, 8, 4, 0))
            # 2026-03-08 06:00 UTC = 2026-03-08 01:00 EST, before the jump.
            make_event(calendar, title="BeforeJump", at=datetime.datetime(2026, 3, 8, 6, 0))
            # 2026-03-08 08:00 UTC = 2026-03-08 04:00 EDT (UTC-4), after it.
            make_event(calendar, title="AfterJump", at=datetime.datetime(2026, 3, 8, 8, 0))
            # 2026-03-09 05:00 UTC = 2026-03-09 01:00 EDT, the next local day.
            make_event(calendar, title="NextDay", at=datetime.datetime(2026, 3, 9, 5, 0))

            local = buckets(
                bucket_plan(
                    CalendarEventTemporalGroupByField.START_TIME,
                    TemporalGranularity.DAY,
                    NEW_YORK_NAME,
                ),
                "start_time_day",
                NEW_YORK,
            )

        assert local == [
            (datetime.date(2026, 3, 7), 1),
            (datetime.date(2026, 3, 8), 2),
            (datetime.date(2026, 3, 9), 1),
        ]

    def test_the_short_local_day_starts_at_its_own_midnight(self, organization, calendar):
        """The 23-hour day still begins at 00:00 local, not 01:00."""
        with organization_context(organization):
            make_event(calendar, title="BeforeJump", at=datetime.datetime(2026, 3, 8, 6, 0))

            rows = execute_plan(
                bucket_plan(
                    CalendarEventTemporalGroupByField.START_TIME,
                    TemporalGranularity.DAY,
                    NEW_YORK_NAME,
                ),
                CalendarEvent.objects.all(),
            )

        bucket = rows[0]["start_time_day"].astimezone(NEW_YORK)
        assert (bucket.hour, bucket.minute) == (0, 0)
        assert bucket.date() == datetime.date(2026, 3, 8)
        # Midnight on the 8th in New York is 05:00 UTC -- still EST, the jump is
        # two hours later.
        assert bucket.astimezone(datetime.UTC) == datetime.datetime(
            2026, 3, 8, 5, 0, tzinfo=datetime.UTC
        )


class TestWeekAndMonthBoundaries:
    def test_a_week_bucket_breaks_on_monday(self, organization, calendar):
        with organization_context(organization):
            # 2026-03-05 is a Thursday, the 8th a Sunday, the 9th a Monday.
            make_event(calendar, title="Thu", at=datetime.datetime(2026, 3, 5, 15, 0))
            make_event(calendar, title="Sun", at=datetime.datetime(2026, 3, 8, 15, 0))
            make_event(calendar, title="Mon", at=datetime.datetime(2026, 3, 9, 15, 0))

            weeks = buckets(
                bucket_plan(
                    CalendarEventTemporalGroupByField.START_TIME,
                    TemporalGranularity.WEEK,
                    SAO_PAULO_NAME,
                ),
                "start_time_week",
                SAO_PAULO,
            )

        # Sunday belongs to the week that began Monday the 2nd, matching
        # Django's TruncWeek.
        assert weeks == [
            (datetime.date(2026, 3, 2), 2),
            (datetime.date(2026, 3, 9), 1),
        ]

    def test_a_week_boundary_moves_with_the_callers_zone(self, organization, calendar):
        with organization_context(organization):
            # 2026-03-09 02:00 UTC is Sunday the 8th, 23:00 in Sao Paulo: the
            # previous week locally, the new one in UTC.
            make_event(calendar, title="SundayNight", at=datetime.datetime(2026, 3, 9, 2, 0))

            local = buckets(
                bucket_plan(
                    CalendarEventTemporalGroupByField.START_TIME,
                    TemporalGranularity.WEEK,
                    SAO_PAULO_NAME,
                ),
                "start_time_week",
                SAO_PAULO,
            )
            utc = buckets(
                bucket_plan(
                    CalendarEventTemporalGroupByField.START_TIME,
                    TemporalGranularity.WEEK,
                    UTC_NAME,
                ),
                "start_time_week",
                datetime.UTC,
            )

        assert local == [(datetime.date(2026, 3, 2), 1)]
        assert utc == [(datetime.date(2026, 3, 9), 1)]

    def test_a_month_bucket_groups_a_calendar_month(self, organization, calendar):
        with organization_context(organization):
            make_event(calendar, title="EarlyMarch", at=datetime.datetime(2026, 3, 2, 15, 0))
            make_event(calendar, title="LateMarch", at=datetime.datetime(2026, 3, 30, 15, 0))
            make_event(calendar, title="April", at=datetime.datetime(2026, 4, 2, 15, 0))

            months = buckets(
                bucket_plan(
                    CalendarEventTemporalGroupByField.START_TIME,
                    TemporalGranularity.MONTH,
                    SAO_PAULO_NAME,
                ),
                "start_time_month",
                SAO_PAULO,
            )

        assert months == [
            (datetime.date(2026, 3, 1), 2),
            (datetime.date(2026, 4, 1), 1),
        ]

    def test_a_month_boundary_moves_with_the_callers_zone(self, organization, calendar):
        with organization_context(organization):
            # 2026-04-01 01:00 UTC is 2026-03-31 22:00 in Sao Paulo.
            make_event(calendar, title="TurnOfMonth", at=datetime.datetime(2026, 4, 1, 1, 0))

            local = buckets(
                bucket_plan(
                    CalendarEventTemporalGroupByField.START_TIME,
                    TemporalGranularity.MONTH,
                    SAO_PAULO_NAME,
                ),
                "start_time_month",
                SAO_PAULO,
            )
            utc = buckets(
                bucket_plan(
                    CalendarEventTemporalGroupByField.START_TIME,
                    TemporalGranularity.MONTH,
                    UTC_NAME,
                ),
                "start_time_month",
                datetime.UTC,
            )

        assert local == [(datetime.date(2026, 3, 1), 1)]
        assert utc == [(datetime.date(2026, 4, 1), 1)]


class TestBucketsAreSparse:
    def test_a_day_with_no_events_produces_no_row_rather_than_a_zero(self, organization, calendar):
        with organization_context(organization):
            make_event(calendar, title="Day2", at=datetime.datetime(2026, 3, 2, 15, 0))
            make_event(calendar, title="Day5", at=datetime.datetime(2026, 3, 5, 15, 0))

            days = buckets(
                bucket_plan(
                    CalendarEventTemporalGroupByField.START_TIME,
                    TemporalGranularity.DAY,
                    SAO_PAULO_NAME,
                ),
                "start_time_day",
                SAO_PAULO,
            )

        # The 3rd and the 4th fall inside the range and have no events, so they
        # have no rows. Zero-filling the axis is the client's job.
        assert days == [
            (datetime.date(2026, 3, 2), 1),
            (datetime.date(2026, 3, 5), 1),
        ]
        assert len(days) == 2

    def test_no_matching_rows_at_all_returns_no_rows(self, organization, calendar):
        with organization_context(organization):
            rows = execute_plan(
                bucket_plan(
                    CalendarEventTemporalGroupByField.START_TIME,
                    TemporalGranularity.DAY,
                    SAO_PAULO_NAME,
                ),
                CalendarEvent.objects.all(),
            )

        assert rows == []


class TestBucketingIsDoneByTheDatabase:
    def test_the_bucket_is_computed_in_sql_in_the_named_zone(self, organization, calendar):
        with organization_context(organization):
            make_event(calendar, title="A", at=datetime.datetime(2026, 3, 5, 1, 0))
            sql = str(
                build_aggregate_queryset(
                    bucket_plan(
                        CalendarEventTemporalGroupByField.START_TIME,
                        TemporalGranularity.DAY,
                        SAO_PAULO_NAME,
                    ),
                    CalendarEvent.objects.all(),
                ).query
            )

        assert "GROUP BY" in sql
        assert "DATE_TRUNC" in sql.upper()
        assert "America/Sao_Paulo" in sql

    def test_a_bucketed_query_is_a_single_round_trip(self, organization, calendar):
        with organization_context(organization):
            for day in (2, 3, 4, 5, 6):
                make_event(calendar, title=f"Day{day}", at=datetime.datetime(2026, 3, day, 15, 0))

            with CaptureQueriesContext(connection) as captured:
                rows = execute_plan(
                    bucket_plan(
                        CalendarEventTemporalGroupByField.START_TIME,
                        TemporalGranularity.DAY,
                        SAO_PAULO_NAME,
                    ),
                    CalendarEvent.objects.all(),
                )

        assert len(rows) == 5
        assert len(captured.captured_queries) == 1

    def test_buckets_come_back_in_chronological_order(self, organization, calendar):
        with organization_context(organization):
            # Inserted out of order on purpose.
            for day in (6, 2, 4):
                make_event(calendar, title=f"Day{day}", at=datetime.datetime(2026, 3, day, 15, 0))

            days = buckets(
                bucket_plan(
                    CalendarEventTemporalGroupByField.START_TIME,
                    TemporalGranularity.DAY,
                    SAO_PAULO_NAME,
                ),
                "start_time_day",
                SAO_PAULO,
            )

        assert [day for day, _count in days] == [
            datetime.date(2026, 3, 2),
            datetime.date(2026, 3, 4),
            datetime.date(2026, 3, 6),
        ]


class TestEndTimeBucketsIndependentlyOfStartTime:
    def test_grouping_by_end_time_buckets_the_end_of_the_span(self, organization, calendar):
        with organization_context(organization):
            # Starts 2026-03-04 23:30 in Sao Paulo, ends 00:30 on the 5th.
            make_event(
                calendar,
                title="OverMidnight",
                at=datetime.datetime(2026, 3, 5, 2, 30),
                minutes=60,
            )

            by_start = buckets(
                bucket_plan(
                    CalendarEventTemporalGroupByField.START_TIME,
                    TemporalGranularity.DAY,
                    SAO_PAULO_NAME,
                ),
                "start_time_day",
                SAO_PAULO,
            )
            by_end = buckets(
                bucket_plan(
                    CalendarEventTemporalGroupByField.END_TIME,
                    TemporalGranularity.DAY,
                    SAO_PAULO_NAME,
                ),
                "end_time_day",
                SAO_PAULO,
            )

        assert by_start == [(datetime.date(2026, 3, 4), 1)]
        assert by_end == [(datetime.date(2026, 3, 5), 1)]


class TestScalarDimensionsAreNotBucketed:
    def test_a_scalar_dimension_passes_through_untruncated(self, organization, calendar):
        with organization_context(organization):
            make_event(calendar, title="A", at=datetime.datetime(2026, 3, 5, 1, 0))

            dimensions = dimensions_from_group_by(
                AggregatableEntity.CALENDAR_EVENT,
                [CalendarEventGroupByInput(scalar=CalendarEventScalarGroupByField.CALENDAR_ID)],
                SAO_PAULO_NAME,
            )
            plan = AggregateQueryPlan(
                entity=AggregatableEntity.CALENDAR_EVENT,
                dimensions=dimensions,
                metrics=(MetricSpec.row_count(),),
            )
            rows = execute_plan(plan, CalendarEvent.objects.all())
            sql = str(build_aggregate_queryset(plan, CalendarEvent.objects.all()).query)

        assert rows == [{"calendar_id": calendar.pk, "count": 1}]
        # No truncation, and the caller's zone never reaches the SQL.
        assert "DATE_TRUNC" not in sql.upper()
        assert "America/Sao_Paulo" not in sql
