"""Integration tests for the aggregate executor, against real rows."""

import datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest
from model_bakery import baker

from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    EventAttendance,
    ResourceAllocation,
)
from common.organization_context import organization_context
from organizations.models import Organization, OrganizationMembership
from public_api.aggregations import errors
from public_api.aggregations.executor import build_aggregate_queryset
from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    DimensionSpec,
    FilterBounds,
    HavingSpec,
    MetricSpec,
    OrderDirection,
    OrderSpec,
    WindowSpec,
)
from public_api.aggregations.types import TemporalGranularity
from users.models import User


UTC = ZoneInfo("UTC")
SAO_PAULO = ZoneInfo("America/Sao_Paulo")

#: Wide enough to cover every row the fixtures build.
BOUNDS = FilterBounds(
    field_path="start_time",
    start=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
    end=datetime.datetime(2026, 12, 31, tzinfo=datetime.UTC),
)


@pytest.fixture
def organization():
    return baker.make(Organization, name="Aggregations Org")


@pytest.fixture
def calendars(organization):
    # Distinct external ids: (external_id, provider, organization) is unique.
    return [
        Calendar.objects.create(organization=organization, name="Alpha", external_id="cal-alpha"),
        Calendar.objects.create(organization=organization, name="Beta", external_id="cal-beta"),
    ]


def _event(
    organization: Organization,
    calendar: Calendar,
    *,
    title: str,
    start: datetime.datetime,
    minutes: int,
    is_bundle_primary: bool = False,
) -> CalendarEvent:
    return CalendarEvent.objects.create(
        organization=organization,
        calendar=calendar,
        title=title,
        description=f"{title} notes",
        external_id=f"ext-{title}-{start:%Y%m%d%H%M}",
        start_time_tz_unaware=start,
        end_time_tz_unaware=start + datetime.timedelta(minutes=minutes),
        timezone="UTC",
        is_bundle_primary=is_bundle_primary,
    )


@pytest.fixture
def events(organization, calendars):
    """Five events: three on Alpha, two on Beta, spread over two days."""
    alpha, beta = calendars
    return [
        _event(
            organization,
            alpha,
            title="a1",
            start=datetime.datetime(2026, 3, 2, 9, 0),
            minutes=30,
            is_bundle_primary=True,
        ),
        _event(
            organization,
            alpha,
            title="a2",
            start=datetime.datetime(2026, 3, 2, 11, 0),
            minutes=60,
        ),
        _event(
            organization,
            alpha,
            title="a3",
            start=datetime.datetime(2026, 3, 3, 9, 0),
            minutes=90,
            is_bundle_primary=True,
        ),
        _event(
            organization,
            beta,
            title="b1",
            start=datetime.datetime(2026, 3, 2, 14, 0),
            minutes=15,
        ),
        _event(
            organization,
            beta,
            title="b2",
            start=datetime.datetime(2026, 3, 3, 14, 0),
            minutes=45,
        ),
    ]


def _plan(**overrides: Any) -> AggregateQueryPlan:
    kwargs: dict[str, Any] = {
        "entity": AggregatableEntity.CALENDAR_EVENT,
        "dimensions": (DimensionSpec.of("calendar_id"),),
        "metrics": (MetricSpec.row_count(),),
        "filter_bounds": BOUNDS,
    }
    kwargs.update(overrides)
    return AggregateQueryPlan(**kwargs)


def _base(organization: Organization):
    """The organization-scoped, date-bounded base queryset the resolver passes in."""
    return CalendarEvent.objects.filter(start_time__gte=BOUNDS.start, start_time__lte=BOUNDS.end)


@pytest.mark.django_db
class TestGroupingAndMetrics:
    """One row per distinct key, with the arithmetic done by Postgres."""

    def test_one_row_per_distinct_key_with_count_sum_avg_min_max(
        self, organization, calendars, events
    ):
        alpha, beta = calendars
        plan = _plan(
            metrics=(
                MetricSpec.row_count(),
                MetricSpec.of("duration_minutes", AggregateOp.SUM),
                MetricSpec.of("duration_minutes", AggregateOp.AVG),
                MetricSpec.of("duration_minutes", AggregateOp.MIN),
                MetricSpec.of("duration_minutes", AggregateOp.MAX),
            ),
        )

        with organization_context(organization):
            rows = list(build_aggregate_queryset(plan, _base(organization)))

        by_calendar = {row["dim_calendar_id"]: row for row in rows}

        assert len(rows) == 2
        assert by_calendar[alpha.id]["m_count"] == 3
        assert by_calendar[alpha.id]["m_duration_minutes_sum"] == pytest.approx(180)
        assert by_calendar[alpha.id]["m_duration_minutes_avg"] == pytest.approx(60)
        assert by_calendar[alpha.id]["m_duration_minutes_min"] == pytest.approx(30)
        assert by_calendar[alpha.id]["m_duration_minutes_max"] == pytest.approx(90)
        assert by_calendar[beta.id]["m_count"] == 2
        assert by_calendar[beta.id]["m_duration_minutes_sum"] == pytest.approx(60)
        assert by_calendar[beta.id]["m_duration_minutes_avg"] == pytest.approx(30)

    def test_string_metrics_concat_min_and_max(self, organization, calendars, events):
        alpha, _beta = calendars
        plan = _plan(
            metrics=(
                MetricSpec.of("title", AggregateOp.CONCAT, separator="|"),
                MetricSpec.of("title", AggregateOp.MIN),
                MetricSpec.of("title", AggregateOp.MAX),
            ),
        )

        with organization_context(organization):
            rows = list(build_aggregate_queryset(plan, _base(organization)))

        alpha_row = next(row for row in rows if row["dim_calendar_id"] == alpha.id)

        # Ordered by the aggregated value, so the concatenation is deterministic.
        assert alpha_row["m_title_concat"] == "a1|a2|a3"
        assert alpha_row["m_title_min"] == "a1"
        assert alpha_row["m_title_max"] == "a3"

    def test_distinct_concat_collapses_repeats(self, organization, calendars):
        alpha, _beta = calendars
        for index in range(3):
            _event(
                organization,
                alpha,
                title="same",
                start=datetime.datetime(2026, 4, 1, 9 + index, 0),
                minutes=30,
            )
        plan = _plan(
            metrics=(MetricSpec.of("title", AggregateOp.CONCAT, separator=",", distinct=True),),
        )

        with organization_context(organization):
            rows = list(build_aggregate_queryset(plan, _base(organization)))

        assert rows[0]["m_title_concat"] == "same"

    def test_datetime_metrics_min_and_max(self, organization, calendars, events):
        alpha, _beta = calendars
        plan = _plan(
            metrics=(
                MetricSpec.of("start_time", AggregateOp.MIN),
                MetricSpec.of("start_time", AggregateOp.MAX),
            ),
        )

        with organization_context(organization):
            rows = list(build_aggregate_queryset(plan, _base(organization)))

        alpha_row = next(row for row in rows if row["dim_calendar_id"] == alpha.id)

        assert alpha_row["m_start_time_min"] == datetime.datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
        assert alpha_row["m_start_time_max"] == datetime.datetime(2026, 3, 3, 9, 0, tzinfo=UTC)

    def test_boolean_metrics_split_the_rows(self, organization, calendars, events):
        alpha, beta = calendars
        plan = _plan(
            metrics=(
                MetricSpec.row_count(),
                MetricSpec.of("is_bundle_primary", AggregateOp.TRUE_COUNT),
                MetricSpec.of("is_bundle_primary", AggregateOp.FALSE_COUNT),
            ),
        )

        with organization_context(organization):
            rows = list(build_aggregate_queryset(plan, _base(organization)))

        by_calendar = {row["dim_calendar_id"]: row for row in rows}

        assert by_calendar[alpha.id]["m_is_bundle_primary_true_count"] == 2
        assert by_calendar[alpha.id]["m_is_bundle_primary_false_count"] == 1
        assert by_calendar[beta.id]["m_is_bundle_primary_true_count"] == 0
        assert by_calendar[beta.id]["m_is_bundle_primary_false_count"] == 2

    def test_grouping_on_two_dimensions_keys_on_the_pair(self, organization, calendars, events):
        alpha, beta = calendars
        plan = _plan(
            dimensions=(
                DimensionSpec.of("calendar_id"),
                DimensionSpec.of("start_time", granularity=TemporalGranularity.DAY, tzinfo=UTC),
            ),
        )

        with organization_context(organization):
            rows = list(build_aggregate_queryset(plan, _base(organization)))

        keyed = {
            (row["dim_calendar_id"], row["dim_start_time"].date()): row["m_count"] for row in rows
        }

        assert keyed == {
            (alpha.id, datetime.date(2026, 3, 2)): 2,
            (alpha.id, datetime.date(2026, 3, 3)): 1,
            (beta.id, datetime.date(2026, 3, 2)): 1,
            (beta.id, datetime.date(2026, 3, 3)): 1,
        }


@pytest.mark.django_db
class TestTemporalBucketing:
    """Buckets are cut in the caller's timezone, by Postgres."""

    def test_day_buckets_are_sparse(self, organization, calendars, events):
        plan = _plan(
            dimensions=(
                DimensionSpec.of("start_time", granularity=TemporalGranularity.DAY, tzinfo=UTC),
            ),
        )

        with organization_context(organization):
            rows = list(build_aggregate_queryset(plan, _base(organization)))

        # Two days hold rows; the days between the fixtures and the range's
        # bounds produce no row at all.
        assert [(row["dim_start_time"].date(), row["m_count"]) for row in rows] == [
            (datetime.date(2026, 3, 2), 3),
            (datetime.date(2026, 3, 3), 2),
        ]

    def test_the_caller_timezone_decides_which_day_a_row_falls_in(self, organization, calendars):
        alpha, _beta = calendars
        # 01:00 UTC on 4 March is 22:00 on 3 March in Sao Paulo (UTC-3).
        _event(
            organization,
            alpha,
            title="late",
            start=datetime.datetime(2026, 3, 4, 1, 0),
            minutes=30,
        )

        def buckets(tzinfo):
            plan = _plan(
                dimensions=(
                    DimensionSpec.of(
                        "start_time", granularity=TemporalGranularity.DAY, tzinfo=tzinfo
                    ),
                ),
            )
            with organization_context(organization):
                return [
                    row["dim_start_time"].date()
                    for row in build_aggregate_queryset(plan, _base(organization))
                ]

        assert buckets(UTC) == [datetime.date(2026, 3, 4)]
        assert buckets(SAO_PAULO) == [datetime.date(2026, 3, 3)]

    def test_month_buckets_collapse_every_day_in_the_month(self, organization, calendars, events):
        plan = _plan(
            dimensions=(
                DimensionSpec.of("start_time", granularity=TemporalGranularity.MONTH, tzinfo=UTC),
            ),
        )

        with organization_context(organization):
            rows = list(build_aggregate_queryset(plan, _base(organization)))

        assert len(rows) == 1
        assert rows[0]["dim_start_time"].date() == datetime.date(2026, 3, 1)
        assert rows[0]["m_count"] == 5


@pytest.mark.django_db
class TestRelationCountsDoNotFanOut:
    """Two counts over different relations stay independent."""

    @pytest.fixture
    def event_with_related_rows(self, organization, calendars):
        """One event with two attendances and two resource allocations.

        Joined naively, the two relations multiply: each count would report
        four instead of two.
        """
        alpha, beta = calendars
        event = _event(
            organization,
            alpha,
            title="fanout",
            start=datetime.datetime(2026, 5, 1, 9, 0),
            minutes=60,
        )
        for index in range(2):
            user = baker.make(User, email=f"attendee{index}@example.com")
            OrganizationMembership.objects.get_or_create(user=user, organization=organization)
            EventAttendance.objects.create(
                organization=organization, event=event, membership_user_id=user.id
            )
        for calendar in (alpha, beta):
            ResourceAllocation.objects.create(
                organization=organization, event=event, calendar=calendar
            )
        return event

    def test_two_relation_counts_in_one_query_do_not_multiply(
        self, organization, calendars, event_with_related_rows
    ):
        alpha, _beta = calendars
        plan = _plan(
            metrics=(
                MetricSpec.row_count(),
                MetricSpec.of("attendances", AggregateOp.RELATION_COUNT),
                MetricSpec.of("resource_allocations", AggregateOp.RELATION_COUNT),
            ),
        )

        with organization_context(organization):
            rows = list(build_aggregate_queryset(plan, _base(organization)))

        row = next(row for row in rows if row["dim_calendar_id"] == alpha.id)

        assert row["m_count"] == 1
        assert row["m_attendances_relation_count"] == 2
        assert row["m_resource_allocations_relation_count"] == 2

    def test_relation_counts_sum_across_the_rows_of_a_group(
        self, organization, calendars, event_with_related_rows
    ):
        alpha, _beta = calendars
        second = _event(
            organization,
            alpha,
            title="second",
            start=datetime.datetime(2026, 5, 2, 9, 0),
            minutes=30,
        )
        ResourceAllocation.objects.create(organization=organization, event=second, calendar=alpha)
        plan = _plan(
            metrics=(
                MetricSpec.row_count(),
                MetricSpec.of("resource_allocations", AggregateOp.RELATION_COUNT),
            ),
        )

        with organization_context(organization):
            rows = list(build_aggregate_queryset(plan, _base(organization)))

        row = next(row for row in rows if row["dim_calendar_id"] == alpha.id)

        assert row["m_count"] == 2
        assert row["m_resource_allocations_relation_count"] == 3

    def test_a_group_with_no_related_rows_counts_zero_not_null(
        self, organization, calendars, events
    ):
        plan = _plan(metrics=(MetricSpec.of("attendances", AggregateOp.RELATION_COUNT),))

        with organization_context(organization):
            rows = list(build_aggregate_queryset(plan, _base(organization)))

        assert {row["m_attendances_relation_count"] for row in rows} == {0}


@pytest.mark.django_db
class TestGeneratedSql:
    """One statement, with a GROUP BY in it."""

    def test_the_whole_aggregate_is_a_single_grouped_query(self, organization, calendars, events):
        plan = _plan(
            dimensions=(
                DimensionSpec.of("calendar_id"),
                DimensionSpec.of(
                    "start_time", granularity=TemporalGranularity.DAY, tzinfo=SAO_PAULO
                ),
            ),
            metrics=(
                MetricSpec.row_count(),
                MetricSpec.of("duration_minutes", AggregateOp.SUM),
                MetricSpec.of("title", AggregateOp.CONCAT),
                MetricSpec.of("is_bundle_primary", AggregateOp.TRUE_COUNT),
                MetricSpec.of("attendances", AggregateOp.RELATION_COUNT),
                MetricSpec.of("resource_allocations", AggregateOp.RELATION_COUNT),
            ),
        )

        with organization_context(organization), CaptureQueriesContext(connection) as captured:
            rows = list(build_aggregate_queryset(plan, _base(organization)))

        assert rows
        assert len(captured.captured_queries) == 1
        assert "GROUP BY" in captured.captured_queries[0]["sql"].upper()

    def test_the_organization_filter_survives_into_the_generated_sql(
        self, organization, calendars, events
    ):
        plan = _plan()

        with organization_context(organization):
            sql = str(build_aggregate_queryset(plan, _base(organization)).query)

        assert "organization_id" in sql
        assert "GROUP BY" in sql.upper()

    def test_rows_of_another_organization_are_never_aggregated_in(
        self, organization, calendars, events
    ):
        other = baker.make(Organization, name="Other Org")
        other_calendar = Calendar.objects.create(
            organization=other, name="Other", external_id="cal-other"
        )
        for index in range(4):
            _event(
                other,
                other_calendar,
                title=f"other{index}",
                start=datetime.datetime(2026, 3, 2, 9 + index, 0),
                minutes=120,
            )
        plan = _plan(metrics=(MetricSpec.row_count(),))

        with organization_context(organization):
            rows = list(build_aggregate_queryset(plan, _base(organization)))

        assert sum(row["m_count"] for row in rows) == 5


@pytest.mark.django_db
class TestOrderingAndSlicing:
    """Paging over groups is stable, and capped."""

    def test_ordering_by_a_metric_descending(self, organization, calendars, events):
        alpha, beta = calendars
        plan = _plan(
            metrics=(MetricSpec.row_count(),),
            order_by=(OrderSpec(alias="m_count", direction=OrderDirection.DESC),),
        )

        with organization_context(organization):
            rows = list(build_aggregate_queryset(plan, _base(organization)))

        assert [row["dim_calendar_id"] for row in rows] == [alpha.id, beta.id]

    def test_ordering_defaults_to_the_group_key(self, organization, calendars, events):
        alpha, beta = calendars
        plan = _plan()

        with organization_context(organization):
            rows = list(build_aggregate_queryset(plan, _base(organization)))

        assert [row["dim_calendar_id"] for row in rows] == sorted([alpha.id, beta.id])

    def test_limit_and_offset_page_over_the_groups(self, organization, calendars, events):
        alpha, beta = calendars
        with organization_context(organization):
            first = list(build_aggregate_queryset(_plan(limit=1), _base(organization)))
            second = list(build_aggregate_queryset(_plan(limit=1, offset=1), _base(organization)))

        assert [row["dim_calendar_id"] for row in first] == [min(alpha.id, beta.id)]
        assert [row["dim_calendar_id"] for row in second] == [max(alpha.id, beta.id)]


@pytest.mark.django_db
class TestExecutorRefusals:
    """The executor refuses a plan it cannot honour exactly."""

    def test_a_base_queryset_over_another_model_is_refused(self, organization, calendars):
        plan = _plan()

        with organization_context(organization), pytest.raises(errors.InvalidPlanError):
            build_aggregate_queryset(plan, Calendar.objects.all())

    def test_a_plan_carrying_a_having_is_refused_rather_than_executed_without_it(
        self, organization, calendars
    ):
        plan = _plan(having=HavingSpec())

        with (
            organization_context(organization),
            pytest.raises(errors.UnsupportedPlanFeatureError, match="HAVING"),
        ):
            build_aggregate_queryset(plan, _base(organization))

    def test_a_plan_carrying_a_window_is_refused(self, organization, calendars):
        plan = _plan(window=WindowSpec(partition_by=("dim_calendar_id",)))

        with (
            organization_context(organization),
            pytest.raises(errors.UnsupportedPlanFeatureError, match="window"),
        ):
            build_aggregate_queryset(plan, _base(organization))

    def test_an_alias_shadowing_a_column_is_refused(self, organization, calendars):
        plan = _plan(dimensions=(DimensionSpec(alias="title", field_path="calendar_id"),))

        with (
            organization_context(organization),
            pytest.raises(errors.AliasCollisionError, match="shadows a column"),
        ):
            build_aggregate_queryset(plan, _base(organization))

    def test_an_unregistered_dimension_is_refused(self, organization, calendars):
        plan = _plan(dimensions=(DimensionSpec.of("external_id"),))

        with (
            organization_context(organization),
            pytest.raises(errors.UnknownFieldError),
        ):
            build_aggregate_queryset(plan, _base(organization))

    def test_an_operation_the_field_kind_does_not_expose_is_refused(self, organization, calendars):
        plan = _plan(metrics=(MetricSpec.of("title", AggregateOp.SUM),))

        with (
            organization_context(organization),
            pytest.raises(errors.UnsupportedOperationError),
        ):
            build_aggregate_queryset(plan, _base(organization))

    def test_a_granularity_on_a_non_temporal_dimension_is_refused(self, organization, calendars):
        plan = _plan(
            dimensions=(
                DimensionSpec.of("calendar_id", granularity=TemporalGranularity.DAY, tzinfo=UTC),
            ),
        )

        with (
            organization_context(organization),
            pytest.raises(errors.UnsupportedOperationError, match="takes no granularity"),
        ):
            build_aggregate_queryset(plan, _base(organization))


@pytest.mark.django_db
class TestOtherEntities:
    """The engine is entity-agnostic: a second entity needs no new code."""

    def test_calendars_group_by_a_stored_column(self, organization, calendars):
        plan = AggregateQueryPlan(
            entity=AggregatableEntity.CALENDAR,
            dimensions=(DimensionSpec.of("calendar_type"),),
            metrics=(
                MetricSpec.row_count(),
                MetricSpec.of("name", AggregateOp.MIN),
                MetricSpec.of("events", AggregateOp.RELATION_COUNT),
            ),
            filter_bounds=FilterBounds(
                field_path="created",
                start=datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC),
                end=datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC),
            ),
        )

        with organization_context(organization):
            rows = list(build_aggregate_queryset(plan, Calendar.objects.all()))

        assert len(rows) == 1
        assert rows[0]["m_count"] == 2
        assert rows[0]["m_name_min"] == "Alpha"
        assert rows[0]["m_events_relation_count"] == 0
