"""The executor against real rows in a real database.

Three properties are load-bearing and none of them can be checked without
executing SQL: that one row comes back per distinct group key with the right
numbers in it, that counting two relations at once does not multiply them by
each other, and that the whole thing is one grouped query rather than a Python
loop that looks like one.

Every queryset here starts from the model's organization-scoped manager inside
an ``organization_context`` block, which is the only way the executor is ever
meant to be called.
"""

import datetime
import zoneinfo
from typing import Any

from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest

from calendar_integration.models import BlockedTime, Calendar, CalendarEvent
from common.organization_context import organization_context
from organizations.models import Organization
from public_api.aggregations.errors import (
    EntityQuerysetMismatchError,
    UnknownAggregateFieldError,
    UnsupportedAggregateOperationError,
    WindowNotSupportedError,
)
from public_api.aggregations.executor import build_aggregate_queryset, execute_plan
from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    DimensionSpec,
    MetricSpec,
    OrderSpec,
    WindowSpec,
)
from public_api.aggregations.types import TemporalGranularity


pytestmark = pytest.mark.django_db

SAO_PAULO = zoneinfo.ZoneInfo("America/Sao_Paulo")


def make_event(
    calendar: Calendar,
    *,
    title: str,
    start_hour: int,
    duration_minutes: int,
    day: int = 5,
    description: str = "",
    is_bundle_primary: bool = False,
) -> CalendarEvent:
    """One event on ``calendar``, spanning ``duration_minutes`` from ``start_hour``."""
    start = datetime.datetime(2026, 3, day, start_hour, 0)
    return CalendarEvent.objects.create(
        organization=calendar.organization,
        calendar_fk=calendar,
        title=title,
        description=description,
        external_id=f"{calendar.pk}-{title}-{day}-{start_hour}",
        is_bundle_primary=is_bundle_primary,
        start_time_tz_unaware=start,
        end_time_tz_unaware=start + datetime.timedelta(minutes=duration_minutes),
        timezone="UTC",
    )


def make_blocked_time(calendar: Calendar, *, reason: str, start_hour: int) -> BlockedTime:
    """One blocked hour on ``calendar``.

    ``external_id`` is explicit because ``(calendar, external_id)`` is unique and
    the blank default collides on the second block of the same calendar.
    """
    start = datetime.datetime(2026, 3, 5, start_hour, 0)
    return BlockedTime.objects.create(
        organization=calendar.organization,
        calendar_fk=calendar,
        reason=reason,
        external_id=f"{calendar.pk}-{reason}-{start_hour}",
        start_time_tz_unaware=start,
        end_time_tz_unaware=start + datetime.timedelta(hours=1),
        timezone="UTC",
    )


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.create(name="Aggregating Org")


@pytest.fixture
def other_organization() -> Organization:
    return Organization.objects.create(name="Other Org")


def make_calendar(organization: Organization, name: str) -> Calendar:
    """One calendar. ``external_id`` is explicit because ``(external_id, provider,
    organization)`` is unique and the blank default collides on the second one."""
    return Calendar.objects.create(
        name=name,
        organization=organization,
        external_id=f"{organization.pk}-{name}",
    )


@pytest.fixture
def calendars(organization: Organization) -> tuple[Calendar, Calendar]:
    with organization_context(organization):
        first = make_calendar(organization, "Dr. A")
        second = make_calendar(organization, "Dr. B")
    return first, second


def events_by_calendar_plan(**overrides: Any) -> AggregateQueryPlan:
    """Group events by calendar, counting and rolling up their length."""
    kwargs: dict[str, Any] = {
        "entity": AggregatableEntity.CALENDAR_EVENT,
        "dimensions": (DimensionSpec(alias="calendar_id", field_path="calendar_id"),),
        "metrics": (
            MetricSpec.row_count(),
            MetricSpec("duration_minutes_sum", "duration_minutes", AggregateOp.SUM),
            MetricSpec("duration_minutes_avg", "duration_minutes", AggregateOp.AVG),
            MetricSpec("duration_minutes_min", "duration_minutes", AggregateOp.MIN),
            MetricSpec("duration_minutes_max", "duration_minutes", AggregateOp.MAX),
        ),
    }
    kwargs.update(overrides)
    return AggregateQueryPlan(**kwargs)


class TestOneRowPerGroupWithCorrectAggregates:
    def test_grouping_by_calendar_rolls_each_calendar_up_once(self, organization, calendars):
        first, second = calendars
        with organization_context(organization):
            make_event(first, title="A1", start_hour=9, duration_minutes=30)
            make_event(first, title="A2", start_hour=11, duration_minutes=90)
            make_event(second, title="B1", start_hour=14, duration_minutes=60)

            rows = execute_plan(events_by_calendar_plan(), CalendarEvent.objects.all())

        assert rows == [
            {
                "calendar_id": first.pk,
                "count": 2,
                "duration_minutes_sum": 120.0,
                "duration_minutes_avg": 60.0,
                "duration_minutes_min": 30.0,
                "duration_minutes_max": 90.0,
            },
            {
                "calendar_id": second.pk,
                "count": 1,
                "duration_minutes_sum": 60.0,
                "duration_minutes_avg": 60.0,
                "duration_minutes_min": 60.0,
                "duration_minutes_max": 60.0,
            },
        ]

    def test_a_scalar_dimension_whose_alias_is_its_own_column_groups_directly(
        self, organization, calendars
    ):
        first, _second = calendars
        with organization_context(organization):
            make_event(first, title="A1", start_hour=9, duration_minutes=30)
            make_event(first, title="A2", start_hour=10, duration_minutes=30)
            event = make_event(first, title="A3", start_hour=11, duration_minutes=30)
            CalendarEvent.objects.filter(pk=event.pk).update(timezone="America/Sao_Paulo")

            plan = AggregateQueryPlan(
                entity=AggregatableEntity.CALENDAR_EVENT,
                dimensions=(DimensionSpec(alias="timezone", field_path="timezone"),),
                metrics=(MetricSpec.row_count(),),
            )
            rows = execute_plan(plan, CalendarEvent.objects.all())

        assert rows == [
            {"timezone": "America/Sao_Paulo", "count": 1},
            {"timezone": "UTC", "count": 2},
        ]

    def test_string_and_boolean_aggregates_come_back_from_sql(self, organization, calendars):
        first, _second = calendars
        with organization_context(organization):
            make_event(first, title="Consult", start_hour=9, duration_minutes=30)
            make_event(
                first, title="Follow-up", start_hour=11, duration_minutes=30, is_bundle_primary=True
            )

            plan = events_by_calendar_plan(
                metrics=(
                    MetricSpec.row_count(),
                    MetricSpec(
                        "title_concat",
                        "title",
                        AggregateOp.CONCAT,
                        options={"separator": "; "},
                    ),
                    MetricSpec("title_min", "title", AggregateOp.MIN),
                    MetricSpec("title_max", "title", AggregateOp.MAX),
                    MetricSpec(
                        "is_bundle_primary_true_count",
                        "is_bundle_primary",
                        AggregateOp.TRUE_COUNT,
                    ),
                    MetricSpec(
                        "is_bundle_primary_false_count",
                        "is_bundle_primary",
                        AggregateOp.FALSE_COUNT,
                    ),
                )
            )
            rows = execute_plan(plan, CalendarEvent.objects.all())

        assert rows == [
            {
                "calendar_id": first.pk,
                "count": 2,
                "title_concat": "Consult; Follow-up",
                "title_min": "Consult",
                "title_max": "Follow-up",
                "is_bundle_primary_true_count": 1,
                "is_bundle_primary_false_count": 1,
            }
        ]

    def test_concat_is_ordered_so_repeated_runs_agree(self, organization, calendars):
        first, _second = calendars
        with organization_context(organization):
            # Inserted out of alphabetical order on purpose.
            make_event(first, title="Zeta", start_hour=9, duration_minutes=30)
            make_event(first, title="Alpha", start_hour=10, duration_minutes=30)
            make_event(first, title="Mu", start_hour=11, duration_minutes=30)

            plan = events_by_calendar_plan(
                metrics=(
                    MetricSpec(
                        "title_concat", "title", AggregateOp.CONCAT, options={"separator": ","}
                    ),
                )
            )
            rows = execute_plan(plan, CalendarEvent.objects.all())

        assert rows[0]["title_concat"] == "Alpha,Mu,Zeta"

    def test_concat_distinct_collapses_repeats(self, organization, calendars):
        first, _second = calendars
        with organization_context(organization):
            make_event(first, title="Consult", start_hour=9, duration_minutes=30)
            make_event(first, title="Consult", start_hour=10, duration_minutes=30)
            make_event(first, title="Review", start_hour=11, duration_minutes=30)

            plan = events_by_calendar_plan(
                metrics=(
                    MetricSpec(
                        "title_concat",
                        "title",
                        AggregateOp.CONCAT,
                        options={"separator": ", ", "distinct": True},
                    ),
                )
            )
            rows = execute_plan(plan, CalendarEvent.objects.all())

        assert rows[0]["title_concat"] == "Consult, Review"

    def test_datetime_aggregates_return_the_span_of_the_group(self, organization, calendars):
        first, _second = calendars
        with organization_context(organization):
            make_event(first, title="Early", start_hour=8, duration_minutes=30)
            make_event(first, title="Late", start_hour=17, duration_minutes=30)

            plan = events_by_calendar_plan(
                metrics=(
                    MetricSpec("start_time_min", "start_time", AggregateOp.MIN),
                    MetricSpec("end_time_max", "end_time", AggregateOp.MAX),
                )
            )
            rows = execute_plan(plan, CalendarEvent.objects.all())

        assert rows[0]["start_time_min"] == datetime.datetime(2026, 3, 5, 8, 0, tzinfo=datetime.UTC)
        assert rows[0]["end_time_max"] == datetime.datetime(2026, 3, 5, 17, 30, tzinfo=datetime.UTC)


class TestMultiRelationCountsDoNotDoubleCount:
    """The join-fan-out bug the plan names, reproduced and asserted against.

    Two events *and* two blocked times on one calendar is the shape that breaks
    a naive ``annotate(Count("events"), Count("blocked_times"))``: the join
    produces four rows and both counts come back as 4.
    """

    def test_two_relations_counted_at_once_keep_their_own_totals(self, organization, calendars):
        first, second = calendars
        with organization_context(organization):
            make_event(first, title="A1", start_hour=9, duration_minutes=30)
            make_event(first, title="A2", start_hour=11, duration_minutes=30)
            make_blocked_time(first, reason="Lunch", start_hour=12)
            make_blocked_time(first, reason="Admin", start_hour=13)
            make_event(second, title="B1", start_hour=9, duration_minutes=30)

            plan = AggregateQueryPlan(
                entity=AggregatableEntity.CALENDAR,
                dimensions=(DimensionSpec(alias="calendar_type", field_path="calendar_type"),),
                metrics=(
                    MetricSpec.row_count(),
                    MetricSpec("event_count", "event_count", AggregateOp.COUNT),
                    MetricSpec("blocked_time_count", "blocked_time_count", AggregateOp.COUNT),
                ),
            )
            rows = execute_plan(plan, Calendar.objects.all())

        # Three events and two blocked times across two calendars of one type.
        # Under join fan-out both counts would read 4 for the first calendar and
        # the totals would be 8 and 8.
        assert rows == [
            {
                "calendar_type": "personal",
                "count": 2,
                "event_count": 3,
                "blocked_time_count": 2,
            }
        ]

    def test_a_parent_with_no_related_rows_contributes_zero_not_null(self, organization, calendars):
        first, _second = calendars
        with organization_context(organization):
            make_event(first, title="A1", start_hour=9, duration_minutes=30)

            plan = AggregateQueryPlan(
                entity=AggregatableEntity.CALENDAR,
                dimensions=(DimensionSpec(alias="calendar_type", field_path="calendar_type"),),
                metrics=(
                    MetricSpec("event_count", "event_count", AggregateOp.COUNT),
                    MetricSpec("blocked_time_count", "blocked_time_count", AggregateOp.COUNT),
                ),
            )
            rows = execute_plan(plan, Calendar.objects.all())

        # The second calendar has neither; summing NULL would have made the whole
        # group NULL rather than smaller.
        assert rows == [{"calendar_type": "personal", "event_count": 1, "blocked_time_count": 0}]

    def test_a_relation_count_subquery_carries_the_organization_filter(
        self, organization, calendars
    ):
        with organization_context(organization):
            plan = AggregateQueryPlan(
                entity=AggregatableEntity.CALENDAR,
                dimensions=(DimensionSpec(alias="calendar_type", field_path="calendar_type"),),
                metrics=(MetricSpec("event_count", "event_count", AggregateOp.COUNT),),
            )
            sql = str(build_aggregate_queryset(plan, Calendar.objects.all()).query)

        # The subquery runs through the related model's scoped manager, so the
        # organization is in its WHERE clause rather than only in the outer one.
        assert sql.count(f'organization_id" = {organization.pk}') >= 2


class TestItIsOneGroupedQuery:
    def test_the_generated_sql_groups_in_the_database(self, organization, calendars):
        first, _second = calendars
        with organization_context(organization):
            make_event(first, title="A1", start_hour=9, duration_minutes=30)
            sql = str(
                build_aggregate_queryset(
                    events_by_calendar_plan(), CalendarEvent.objects.all()
                ).query
            )

        assert "GROUP BY" in sql
        assert "COUNT(" in sql
        assert "SUM(" in sql

    def test_executing_a_plan_takes_exactly_one_query(self, organization, calendars):
        first, second = calendars
        with organization_context(organization):
            make_event(first, title="A1", start_hour=9, duration_minutes=30)
            make_event(second, title="B1", start_hour=9, duration_minutes=30)

            with CaptureQueriesContext(connection) as captured:
                rows = execute_plan(events_by_calendar_plan(), CalendarEvent.objects.all())

        assert len(rows) == 2
        assert len(captured.captured_queries) == 1

    def test_a_plan_counting_two_relations_still_takes_one_query(self, organization, calendars):
        first, _second = calendars
        with organization_context(organization):
            make_event(first, title="A1", start_hour=9, duration_minutes=30)
            make_blocked_time(first, reason="Lunch", start_hour=12)

            plan = AggregateQueryPlan(
                entity=AggregatableEntity.CALENDAR,
                dimensions=(DimensionSpec(alias="calendar_type", field_path="calendar_type"),),
                metrics=(
                    MetricSpec("event_count", "event_count", AggregateOp.COUNT),
                    MetricSpec("blocked_time_count", "blocked_time_count", AggregateOp.COUNT),
                ),
            )
            with CaptureQueriesContext(connection) as captured:
                execute_plan(plan, Calendar.objects.all())

        assert len(captured.captured_queries) == 1


class TestTemporalBucketingUsesTheCallersClock:
    """The plan's second goal, and the place a wrong answer looks right.

    ``TIME_ZONE`` is UTC in this project, so bucketing without a caller-supplied
    zone would silently answer in UTC. These tests put events either side of a
    Sao Paulo midnight (UTC-3) and assert the same rows land in *different* days
    in Sao Paulo and in the *same* day in UTC -- which only one of the two
    answers can satisfy.
    """

    def day_plan(self, tz: zoneinfo.ZoneInfo) -> AggregateQueryPlan:
        return AggregateQueryPlan(
            entity=AggregatableEntity.CALENDAR_EVENT,
            dimensions=(
                DimensionSpec(
                    alias="start_time_day",
                    field_path="start_time",
                    granularity=TemporalGranularity.DAY,
                    tzinfo=tz,
                ),
            ),
            metrics=(MetricSpec.row_count(),),
        )

    def test_a_day_bucket_straddles_local_midnight_not_the_servers(self, organization, calendars):
        first, _second = calendars
        with organization_context(organization):
            # 01:00 UTC is 22:00 the previous day in Sao Paulo; 04:00 UTC is
            # 01:00 the same day. Same UTC date, different Sao Paulo dates.
            make_event(first, title="LateNight", start_hour=1, duration_minutes=30, day=5)
            make_event(first, title="EarlyHours", start_hour=4, duration_minutes=30, day=5)

            sao_paulo_rows = execute_plan(self.day_plan(SAO_PAULO), CalendarEvent.objects.all())
            utc_rows = execute_plan(self.day_plan(datetime.UTC), CalendarEvent.objects.all())

        assert [
            (row["start_time_day"].astimezone(SAO_PAULO).date(), row["count"])
            for row in sao_paulo_rows
        ] == [(datetime.date(2026, 3, 4), 1), (datetime.date(2026, 3, 5), 1)]

        # The very same rows, bucketed on the server's clock, collapse into one
        # day -- which is the answer a missing timezone would have returned.
        assert [
            (row["start_time_day"].astimezone(datetime.UTC).date(), row["count"])
            for row in utc_rows
        ] == [(datetime.date(2026, 3, 5), 2)]

    def test_a_week_bucket_splits_on_the_monday_boundary(self, organization, calendars):
        first, _second = calendars
        with organization_context(organization):
            # 2026-03-05 is a Thursday; 2026-03-09 is the following Monday.
            make_event(first, title="Thu", start_hour=12, duration_minutes=30, day=5)
            make_event(first, title="Sun", start_hour=12, duration_minutes=30, day=8)
            make_event(first, title="Mon", start_hour=12, duration_minutes=30, day=9)

            plan = AggregateQueryPlan(
                entity=AggregatableEntity.CALENDAR_EVENT,
                dimensions=(
                    DimensionSpec(
                        alias="start_time_week",
                        field_path="start_time",
                        granularity=TemporalGranularity.WEEK,
                        tzinfo=SAO_PAULO,
                    ),
                ),
                metrics=(MetricSpec.row_count(),),
            )
            rows = execute_plan(plan, CalendarEvent.objects.all())

        assert [
            (row["start_time_week"].astimezone(SAO_PAULO).date(), row["count"]) for row in rows
        ] == [(datetime.date(2026, 3, 2), 2), (datetime.date(2026, 3, 9), 1)]

    def test_a_month_bucket_groups_a_whole_month(self, organization, calendars):
        first, _second = calendars
        with organization_context(organization):
            make_event(first, title="Early", start_hour=12, duration_minutes=30, day=5)
            make_event(first, title="Late", start_hour=12, duration_minutes=30, day=26)

            plan = AggregateQueryPlan(
                entity=AggregatableEntity.CALENDAR_EVENT,
                dimensions=(
                    DimensionSpec(
                        alias="start_time_month",
                        field_path="start_time",
                        granularity=TemporalGranularity.MONTH,
                        tzinfo=SAO_PAULO,
                    ),
                ),
                metrics=(MetricSpec.row_count(),),
            )
            rows = execute_plan(plan, CalendarEvent.objects.all())

        assert [
            (row["start_time_month"].astimezone(SAO_PAULO).date(), row["count"]) for row in rows
        ] == [(datetime.date(2026, 3, 1), 2)]

    def test_a_bucketed_plan_is_still_one_query_that_groups_in_sql(self, organization, calendars):
        first, _second = calendars
        with organization_context(organization):
            make_event(first, title="A1", start_hour=1, duration_minutes=30)
            plan = self.day_plan(SAO_PAULO)
            sql = str(build_aggregate_queryset(plan, CalendarEvent.objects.all()).query)

            with CaptureQueriesContext(connection) as captured:
                execute_plan(plan, CalendarEvent.objects.all())

        assert "GROUP BY" in sql
        # The bucket boundary is computed by the database in the named zone.
        assert "America/Sao_Paulo" in sql
        assert len(captured.captured_queries) == 1

    def test_a_day_with_no_events_produces_no_bucket(self, organization, calendars):
        first, _second = calendars
        with organization_context(organization):
            make_event(first, title="Day5", start_hour=12, duration_minutes=30, day=5)
            make_event(first, title="Day7", start_hour=12, duration_minutes=30, day=7)

            rows = execute_plan(self.day_plan(SAO_PAULO), CalendarEvent.objects.all())

        # The 6th has no events, so it has no row. Buckets are sparse and the
        # zero is the client's to draw.
        assert [row["start_time_day"].astimezone(SAO_PAULO).date() for row in rows] == [
            datetime.date(2026, 3, 5),
            datetime.date(2026, 3, 7),
        ]


class TestBucketsAreSparseAndOrderingIsDeterministic:
    def test_rows_come_back_ordered_by_the_group_key_by_default(self, organization, calendars):
        first, second = calendars
        with organization_context(organization):
            make_event(second, title="B1", start_hour=9, duration_minutes=30)
            make_event(first, title="A1", start_hour=9, duration_minutes=30)

            rows = execute_plan(events_by_calendar_plan(), CalendarEvent.objects.all())

        assert [row["calendar_id"] for row in rows] == sorted([first.pk, second.pk])

    def test_ordering_by_a_metric_puts_the_busiest_group_first(self, organization, calendars):
        first, second = calendars
        with organization_context(organization):
            make_event(first, title="A1", start_hour=9, duration_minutes=30)
            make_event(second, title="B1", start_hour=9, duration_minutes=30)
            make_event(second, title="B2", start_hour=10, duration_minutes=30)

            plan = events_by_calendar_plan(order_by=(OrderSpec(alias="count", descending=True),))
            rows = execute_plan(plan, CalendarEvent.objects.all())

        assert [row["calendar_id"] for row in rows] == [second.pk, first.pk]

    def test_limit_and_offset_page_the_groups(self, organization, calendars):
        first, second = calendars
        with organization_context(organization):
            make_event(first, title="A1", start_hour=9, duration_minutes=30)
            make_event(second, title="B1", start_hour=9, duration_minutes=30)

            page = execute_plan(
                events_by_calendar_plan(limit=1, offset=1), CalendarEvent.objects.all()
            )

        assert [row["calendar_id"] for row in page] == [max(first.pk, second.pk)]

    def test_a_group_with_no_rows_produces_no_row(self, organization, calendars):
        first, _second = calendars
        with organization_context(organization):
            make_event(first, title="A1", start_hour=9, duration_minutes=30)

            rows = execute_plan(events_by_calendar_plan(), CalendarEvent.objects.all())

        # The second calendar has no events, so it has no row -- buckets are
        # sparse, and a zero is the client's to draw.
        assert [row["calendar_id"] for row in rows] == [first.pk]


class TestTenancyComesFromTheCallersQueryset:
    def test_another_organizations_rows_do_not_reach_the_aggregate(
        self, organization, other_organization, calendars
    ):
        first, _second = calendars
        with organization_context(other_organization):
            intruder = make_calendar(other_organization, "Elsewhere")
            make_event(intruder, title="X1", start_hour=9, duration_minutes=600)
            make_event(intruder, title="X2", start_hour=20, duration_minutes=600)

        with organization_context(organization):
            make_event(first, title="A1", start_hour=9, duration_minutes=30)
            rows = execute_plan(events_by_calendar_plan(), CalendarEvent.objects.all())

        assert rows == [
            {
                "calendar_id": first.pk,
                "count": 1,
                "duration_minutes_sum": 30.0,
                "duration_minutes_avg": 30.0,
                "duration_minutes_min": 30.0,
                "duration_minutes_max": 30.0,
            }
        ]

    def test_a_queryset_over_the_wrong_model_is_refused(self, organization, calendars):
        with organization_context(organization), pytest.raises(EntityQuerysetMismatchError):
            build_aggregate_queryset(events_by_calendar_plan(), Calendar.objects.all())


class TestThePlanIsValidatedBeforeAnySql:
    def test_an_unknown_metric_field_raises(self, organization, calendars):
        plan = events_by_calendar_plan(
            metrics=(MetricSpec("ssn_min", "patient_ssn", AggregateOp.MIN),)
        )

        with organization_context(organization), pytest.raises(UnknownAggregateFieldError):
            build_aggregate_queryset(plan, CalendarEvent.objects.all())

    def test_summing_a_string_field_raises(self, organization, calendars):
        plan = events_by_calendar_plan(metrics=(MetricSpec("title_sum", "title", AggregateOp.SUM),))

        with organization_context(organization), pytest.raises(UnsupportedAggregateOperationError):
            build_aggregate_queryset(plan, CalendarEvent.objects.all())

    def test_a_window_is_refused_rather_than_silently_dropped(self, organization, calendars):
        plan = events_by_calendar_plan(
            window=WindowSpec(order_by=(OrderSpec(alias="calendar_id"),))
        )

        with organization_context(organization), pytest.raises(WindowNotSupportedError):
            build_aggregate_queryset(plan, CalendarEvent.objects.all())
