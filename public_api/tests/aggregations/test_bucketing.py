"""Temporal bucketing, against real rows and a real Postgres.

The load-bearing property is that the bucket boundary is the *caller's* local
midnight and not the server's. Every test here is built so an implementation
that truncated in UTC, or in Django's ``TIME_ZONE``, or against a fixed
offset, would give a different answer -- events are placed either side of a
boundary that only exists in the named timezone, and the DST case moves the
offset underneath the bucket while the query runs.

Row values are written as UTC instants: a row stores a naive local wall clock
plus its own ``timezone`` column, and the generated ``start_time`` is the
instant recovered from the pair. Writing every row with ``timezone="UTC"``
makes ``start_time_tz_unaware`` read as the UTC instant directly, which keeps
the arithmetic in these tests about the *bucketing* timezone rather than
about the rows'.
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
    CalendarEventAggregateFilterInput,
    CalendarEventGroupByInput,
    CalendarEventTemporalGroupByField,
    CalendarEventTemporalGroupByInput,
    DimensionSpec,
    MetricSpec,
    NonTemporalGranularityError,
    TemporalGranularity,
    build_aggregate_queryset,
    resolve_dimensions,
)


pytestmark = pytest.mark.django_db


SAO_PAULO = ZoneInfo("America/Sao_Paulo")
NEW_YORK = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

COUNT_METRIC = MetricSpec(alias="count", field_path="id", op=AggregateOp.COUNT)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def org():
    return Organization.objects.create(
        name=f"Bucketing Org {uuid.uuid4().hex[:8]}", should_sync_rooms=False
    )


def _make_calendar(org: Organization, label: str) -> Calendar:
    return Calendar.objects.create(
        organization=org,
        name=label,
        external_id=f"{label}-{uuid.uuid4().hex[:8]}",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
    )


def _make_event(org: Organization, calendar: Calendar, utc_start: datetime.datetime, minutes=30):
    """An event whose ``start_time`` is exactly ``utc_start`` as a UTC instant."""
    return CalendarEvent.objects.create(
        organization=org,
        calendar=calendar,
        title="Visit",
        description="",
        external_id=f"ev-{uuid.uuid4().hex[:12]}",
        start_time_tz_unaware=utc_start,
        end_time_tz_unaware=utc_start + datetime.timedelta(minutes=minutes),
        timezone="UTC",
    )


def _bucket_plan(granularity: TemporalGranularity, tzinfo: ZoneInfo) -> AggregateQueryPlan:
    return AggregateQueryPlan(
        entity=AggregatableEntity.CALENDAR_EVENT,
        dimensions=(
            DimensionSpec(
                alias="start_time_bucket",
                field_path="start_time",
                granularity=granularity,
                tzinfo=tzinfo,
            ),
        ),
        metrics=(COUNT_METRIC,),
    )


def _rows(plan: AggregateQueryPlan, queryset) -> list[dict]:
    return list(build_aggregate_queryset(plan, queryset))


def _local_midnight(year: int, month: int, day: int, tzinfo: ZoneInfo) -> datetime.datetime:
    return datetime.datetime(year, month, day, tzinfo=tzinfo)


# ---------------------------------------------------------------------------
# The boundary is the caller's, not the server's
# ---------------------------------------------------------------------------


class TestDayBucketsFollowTheNamedTimezone:
    """Two events an hour apart, straddling midnight in Sao Paulo only.

    Sao Paulo is UTC-3 all year, so 2026-10-01T01:00Z is 2026-09-30 22:00
    locally and 2026-10-01T05:00Z is 2026-10-01 02:00. In UTC both fall on
    2026-10-01.
    """

    @pytest.fixture
    def events(self, org):
        with organization_context(org):
            calendar = _make_calendar(org, "Only")
            _make_event(org, calendar, datetime.datetime(2026, 10, 1, 1, 0))
            _make_event(org, calendar, datetime.datetime(2026, 10, 1, 5, 0))
            yield

    def test_they_land_in_different_days_in_sao_paulo(self, org, events):
        with organization_context(org):
            rows = _rows(
                _bucket_plan(TemporalGranularity.DAY, SAO_PAULO), CalendarEvent.objects.all()
            )

        assert rows == [
            {"start_time_bucket": _local_midnight(2026, 9, 30, SAO_PAULO), "count": 1},
            {"start_time_bucket": _local_midnight(2026, 10, 1, SAO_PAULO), "count": 1},
        ]

    def test_the_same_events_land_in_one_day_in_utc(self, org, events):
        with organization_context(org):
            rows = _rows(_bucket_plan(TemporalGranularity.DAY, UTC), CalendarEvent.objects.all())

        assert rows == [{"start_time_bucket": _local_midnight(2026, 10, 1, UTC), "count": 2}]

    def test_the_bucket_comes_back_in_the_requested_timezone(self, org, events):
        with organization_context(org):
            rows = _rows(
                _bucket_plan(TemporalGranularity.DAY, SAO_PAULO), CalendarEvent.objects.all()
            )

        for row in rows:
            assert row["start_time_bucket"].tzinfo == SAO_PAULO
            # Midnight *there*, whatever that is as an instant.
            assert row["start_time_bucket"].hour == 0


class TestDayBucketsAcrossADstTransition:
    """US DST begins 2026-03-08: local clocks jump 02:00 EST -> 03:00 EDT.

    The local day 2026-03-08 is 23 hours long, so the offset that decides
    which bucket a row belongs to changes *inside* the range being grouped.
    A fixed-offset implementation puts the last event an hour early and in
    the wrong bucket.
    """

    @pytest.fixture
    def events(self, org):
        with organization_context(org):
            calendar = _make_calendar(org, "Only")
            # 01:30 EST -- before the jump.
            _make_event(org, calendar, datetime.datetime(2026, 3, 8, 6, 30))
            # 03:30 EDT -- after it.
            _make_event(org, calendar, datetime.datetime(2026, 3, 8, 7, 30))
            # 23:59 EDT -- last minute of the short local day.
            _make_event(org, calendar, datetime.datetime(2026, 3, 9, 3, 59))
            # 00:00 EDT the next day. At EST this would read 23:00 on the 8th
            # and join the bucket above; only the real offset separates them.
            _make_event(org, calendar, datetime.datetime(2026, 3, 9, 4, 0))
            yield

    def test_the_short_local_day_holds_three_events_and_the_next_holds_one(self, org, events):
        with organization_context(org):
            rows = _rows(
                _bucket_plan(TemporalGranularity.DAY, NEW_YORK), CalendarEvent.objects.all()
            )

        assert rows == [
            {"start_time_bucket": _local_midnight(2026, 3, 8, NEW_YORK), "count": 3},
            {"start_time_bucket": _local_midnight(2026, 3, 9, NEW_YORK), "count": 1},
        ]

    def test_the_same_events_split_differently_in_utc(self, org, events):
        """Proof the split above is the timezone's doing and not the data's."""
        with organization_context(org):
            rows = _rows(_bucket_plan(TemporalGranularity.DAY, UTC), CalendarEvent.objects.all())

        assert rows == [
            {"start_time_bucket": _local_midnight(2026, 3, 8, UTC), "count": 2},
            {"start_time_bucket": _local_midnight(2026, 3, 9, UTC), "count": 2},
        ]


class TestWeekAndMonthBuckets:
    def test_week_buckets_start_on_monday(self, org):
        """2026-09-28 and 2026-10-05 are Mondays; the events sit midweek."""
        with organization_context(org):
            calendar = _make_calendar(org, "Only")
            # Wednesday 2026-09-30 and Thursday 2026-10-01, both in the week
            # beginning Monday 2026-09-28.
            _make_event(org, calendar, datetime.datetime(2026, 9, 30, 12, 0))
            _make_event(org, calendar, datetime.datetime(2026, 10, 1, 12, 0))
            # Thursday 2026-10-08, the following week.
            _make_event(org, calendar, datetime.datetime(2026, 10, 8, 12, 0))

            rows = _rows(_bucket_plan(TemporalGranularity.WEEK, UTC), CalendarEvent.objects.all())

        assert rows == [
            {"start_time_bucket": _local_midnight(2026, 9, 28, UTC), "count": 2},
            {"start_time_bucket": _local_midnight(2026, 10, 5, UTC), "count": 1},
        ]

    def test_a_sunday_night_event_belongs_to_the_week_that_just_ended(self, org):
        """Sunday 2026-10-04 is the last day of the week beginning 2026-09-28."""
        with organization_context(org):
            calendar = _make_calendar(org, "Only")
            _make_event(org, calendar, datetime.datetime(2026, 10, 4, 23, 0))
            _make_event(org, calendar, datetime.datetime(2026, 10, 5, 1, 0))

            rows = _rows(_bucket_plan(TemporalGranularity.WEEK, UTC), CalendarEvent.objects.all())

        assert rows == [
            {"start_time_bucket": _local_midnight(2026, 9, 28, UTC), "count": 1},
            {"start_time_bucket": _local_midnight(2026, 10, 5, UTC), "count": 1},
        ]

    def test_month_buckets_follow_the_named_timezone_too(self, org):
        """2026-10-01T01:00Z is still September in Sao Paulo."""
        with organization_context(org):
            calendar = _make_calendar(org, "Only")
            _make_event(org, calendar, datetime.datetime(2026, 10, 1, 1, 0))
            _make_event(org, calendar, datetime.datetime(2026, 10, 15, 12, 0))

            sao_paulo_rows = _rows(
                _bucket_plan(TemporalGranularity.MONTH, SAO_PAULO), CalendarEvent.objects.all()
            )
            utc_rows = _rows(
                _bucket_plan(TemporalGranularity.MONTH, UTC), CalendarEvent.objects.all()
            )

        assert sao_paulo_rows == [
            {"start_time_bucket": _local_midnight(2026, 9, 1, SAO_PAULO), "count": 1},
            {"start_time_bucket": _local_midnight(2026, 10, 1, SAO_PAULO), "count": 1},
        ]
        assert utc_rows == [{"start_time_bucket": _local_midnight(2026, 10, 1, UTC), "count": 2}]


# ---------------------------------------------------------------------------
# Sparseness
# ---------------------------------------------------------------------------


class TestBucketsAreSparse:
    def test_a_day_with_no_events_produces_no_row(self, org):
        """Not a zero -- no row at all. Zero-filling is the client's job."""
        with organization_context(org):
            calendar = _make_calendar(org, "Only")
            _make_event(org, calendar, datetime.datetime(2026, 10, 1, 12, 0))
            # Nothing on 2026-10-02.
            _make_event(org, calendar, datetime.datetime(2026, 10, 3, 12, 0))

            queryset = CalendarEventAggregateFilterInput(
                start_datetime=datetime.datetime(2026, 10, 1, tzinfo=UTC),
                end_datetime=datetime.datetime(2026, 10, 5, tzinfo=UTC),
            ).apply(None, org)
            rows = _rows(_bucket_plan(TemporalGranularity.DAY, UTC), queryset)

        assert [row["start_time_bucket"] for row in rows] == [
            _local_midnight(2026, 10, 1, UTC),
            _local_midnight(2026, 10, 3, UTC),
        ]

    def test_a_range_with_no_rows_at_all_returns_nothing(self, org):
        with organization_context(org):
            calendar = _make_calendar(org, "Only")
            _make_event(org, calendar, datetime.datetime(2026, 10, 1, 12, 0))

            queryset = CalendarEventAggregateFilterInput(
                start_datetime=datetime.datetime(2026, 11, 1, tzinfo=UTC),
                end_datetime=datetime.datetime(2026, 11, 5, tzinfo=UTC),
            ).apply(None, org)
            rows = _rows(_bucket_plan(TemporalGranularity.DAY, UTC), queryset)

        assert rows == []


# ---------------------------------------------------------------------------
# The query itself
# ---------------------------------------------------------------------------


class TestGeneratedQuery:
    def test_bucketing_is_one_query_and_postgres_does_the_truncation(self, org):
        with organization_context(org):
            calendar = _make_calendar(org, "Only")
            _make_event(org, calendar, datetime.datetime(2026, 10, 1, 1, 0))
            _make_event(org, calendar, datetime.datetime(2026, 10, 1, 5, 0))

            queryset = build_aggregate_queryset(
                _bucket_plan(TemporalGranularity.DAY, SAO_PAULO), CalendarEvent.objects.all()
            )
            with CaptureQueriesContext(connection) as captured:
                rows = list(queryset)

        assert len(rows) == 2
        assert len(captured.captured_queries) == 1
        sql = captured.captured_queries[0]["sql"].upper()
        assert "DATE_TRUNC" in sql
        assert "AT TIME ZONE" in sql
        assert "GROUP BY" in sql

    def test_buckets_come_back_in_chronological_order_without_being_asked(self, org):
        """The group key is the default ordering, so a time series is in order."""
        with organization_context(org):
            calendar = _make_calendar(org, "Only")
            for day in (5, 1, 3, 2):
                _make_event(org, calendar, datetime.datetime(2026, 10, day, 12, 0))

            rows = _rows(_bucket_plan(TemporalGranularity.DAY, UTC), CalendarEvent.objects.all())

        assert [row["start_time_bucket"].day for row in rows] == [1, 2, 3, 5]

    def test_end_time_buckets_independently_of_start_time(self, org):
        """An event that ends after local midnight buckets on each column's own day."""
        with organization_context(org):
            calendar = _make_calendar(org, "Only")
            # 21:00 -> 21:30 in Sao Paulo on 2026-09-30, entirely within the day.
            _make_event(org, calendar, datetime.datetime(2026, 10, 1, 0, 0), minutes=30)
            # 23:45 -> 00:15, crossing local midnight.
            _make_event(org, calendar, datetime.datetime(2026, 10, 1, 2, 45), minutes=30)

            by_end = AggregateQueryPlan(
                entity=AggregatableEntity.CALENDAR_EVENT,
                dimensions=(
                    DimensionSpec(
                        alias="end_time_bucket",
                        field_path="end_time",
                        granularity=TemporalGranularity.DAY,
                        tzinfo=SAO_PAULO,
                    ),
                ),
                metrics=(COUNT_METRIC,),
            )
            start_rows = _rows(
                _bucket_plan(TemporalGranularity.DAY, SAO_PAULO), CalendarEvent.objects.all()
            )
            end_rows = _rows(by_end, CalendarEvent.objects.all())

        assert start_rows == [
            {"start_time_bucket": _local_midnight(2026, 9, 30, SAO_PAULO), "count": 2}
        ]
        assert end_rows == [
            {"end_time_bucket": _local_midnight(2026, 9, 30, SAO_PAULO), "count": 1},
            {"end_time_bucket": _local_midnight(2026, 10, 1, SAO_PAULO), "count": 1},
        ]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class TestRefusals:
    def test_a_granularity_on_a_non_temporal_dimension_is_refused(self, org):
        """Unreachable through the schema; still refused for a plan built in code."""
        plan = AggregateQueryPlan(
            entity=AggregatableEntity.CALENDAR_EVENT,
            dimensions=(
                DimensionSpec(
                    alias="calendar_bucket",
                    field_path="calendar_fk_id",
                    granularity=TemporalGranularity.DAY,
                    tzinfo=UTC,
                ),
            ),
            metrics=(COUNT_METRIC,),
        )
        with organization_context(org), pytest.raises(NonTemporalGranularityError):
            build_aggregate_queryset(plan, CalendarEvent.objects.all())

    def test_a_bucketed_dimension_is_never_passed_through_by_name(self, org):
        """Aliasing a bucket after its own column would drop the truncation.

        ``DimensionSpec`` allows it; the executor must still annotate rather
        than hand ``start_time`` to ``.values()`` positionally, which would
        group on the raw timestamp -- one group per row.
        """
        plan = AggregateQueryPlan(
            entity=AggregatableEntity.CALENDAR_EVENT,
            dimensions=(
                DimensionSpec(
                    alias="start_time",
                    field_path="start_time",
                    granularity=TemporalGranularity.DAY,
                    tzinfo=UTC,
                ),
            ),
            metrics=(COUNT_METRIC,),
        )
        with organization_context(org), pytest.raises(Exception) as excinfo:  # noqa: B017
            build_aggregate_queryset(plan, CalendarEvent.objects.all())
        # Refused as a shadowing alias rather than silently degraded.
        assert "start_time" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Through the group-by inputs
# ---------------------------------------------------------------------------


class TestThroughTheGroupByInputs:
    def test_a_resolved_group_by_input_buckets_the_same_way(self, org):
        """The path a GraphQL caller will take, end to end against real rows."""
        with organization_context(org):
            calendar = _make_calendar(org, "Only")
            _make_event(org, calendar, datetime.datetime(2026, 10, 1, 1, 0))
            _make_event(org, calendar, datetime.datetime(2026, 10, 1, 5, 0))

            dimensions = resolve_dimensions(
                [
                    CalendarEventGroupByInput(
                        temporal=CalendarEventTemporalGroupByInput(
                            field=CalendarEventTemporalGroupByField.START_TIME,
                            granularity=TemporalGranularity.DAY,
                        )
                    )
                ],
                "America/Sao_Paulo",
            )
            plan = AggregateQueryPlan(
                entity=AggregatableEntity.CALENDAR_EVENT,
                dimensions=dimensions,
                metrics=(COUNT_METRIC,),
            )
            rows = _rows(plan, CalendarEvent.objects.all())

        assert rows == [
            {"start_time_bucket": _local_midnight(2026, 9, 30, SAO_PAULO), "count": 1},
            {"start_time_bucket": _local_midnight(2026, 10, 1, SAO_PAULO), "count": 1},
        ]
