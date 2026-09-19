"""The executor against real rows.

Everything here runs one grouped query and reads the numbers back out of it.
The assertions that matter are the boring ones: one row per distinct key, the
arithmetic right, a multi-relation count that does not double-count, a
``GROUP BY`` in the SQL, and exactly one query.
"""

import datetime
from zoneinfo import ZoneInfo

from django.db import connection
from django.db.models import Count as DjangoCount
from django.test.utils import CaptureQueriesContext

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarProvider
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
)
from common.organization_context import organization_context
from organizations.models import Organization
from public_api.aggregations.errors import (
    AggregateLimitError,
    InvalidAggregatePlanError,
    UnknownAggregateFieldError,
)
from public_api.aggregations.executor import (
    build_aggregate_queryset,
    execute_aggregate_plan,
)
from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    DimensionSpec,
    HavingSpec,
    MetricSpec,
    OrderSpec,
    WindowSpec,
)
from public_api.aggregations.registry import build_dimension, build_metric
from public_api.aggregations.types import TemporalGranularity


BASE = datetime.datetime(2026, 3, 2, 9, 0)


def _make_event(
    organization: Organization,
    calendar: Calendar,
    *,
    external_id: str,
    title: str,
    minutes: int,
    day_offset: int = 0,
    is_bundle_primary: bool = False,
) -> CalendarEvent:
    start = BASE + datetime.timedelta(days=day_offset)
    return baker.make(
        CalendarEvent,
        organization=organization,
        calendar=calendar,
        external_id=external_id,
        title=title,
        description=f"{title} description",
        timezone="UTC",
        start_time_tz_unaware=start,
        end_time_tz_unaware=start + datetime.timedelta(minutes=minutes),
        is_bundle_primary=is_bundle_primary,
    )


@pytest.fixture
def organization() -> Organization:
    return baker.make(Organization, name="Aggregation Org")


@pytest.fixture
def calendar_a(organization) -> Calendar:
    return baker.make(
        Calendar,
        organization=organization,
        name="Calendar A",
        external_id="cal-a",
        provider=CalendarProvider.INTERNAL,
    )


@pytest.fixture
def calendar_b(organization) -> Calendar:
    return baker.make(
        Calendar,
        organization=organization,
        name="Calendar B",
        external_id="cal-b",
        provider=CalendarProvider.INTERNAL,
    )


@pytest.fixture
def events(organization, calendar_a, calendar_b) -> None:
    """Three events on A (30 / 60 / 90 minutes) and one on B (45 minutes)."""
    _make_event(organization, calendar_a, external_id="a-1", title="Alpha", minutes=30)
    _make_event(
        organization,
        calendar_a,
        external_id="a-2",
        title="Bravo",
        minutes=60,
        day_offset=1,
        is_bundle_primary=True,
    )
    _make_event(
        organization, calendar_a, external_id="a-3", title="Charlie", minutes=90, day_offset=2
    )
    _make_event(organization, calendar_b, external_id="b-1", title="Delta", minutes=45)


def _event_plan(*metrics: MetricSpec, **plan_kwargs) -> AggregateQueryPlan:
    return AggregateQueryPlan(
        entity=AggregatableEntity.CALENDAR_EVENT,
        dimensions=(build_dimension(AggregatableEntity.CALENDAR_EVENT, "calendar_id"),),
        metrics=metrics,
        **plan_kwargs,
    )


def _rows_by_calendar(rows: list[dict]) -> dict[int, dict]:
    return {row["calendar_id"]: row for row in rows}


@pytest.mark.django_db
class TestGroupedAggregates:
    def test_one_row_per_distinct_key_with_correct_numbers(
        self, organization, calendar_a, calendar_b, events
    ):
        plan = _event_plan(
            build_metric(
                AggregatableEntity.CALENDAR_EVENT, "count", AggregateOp.COUNT, alias="count"
            ),
            build_metric(
                AggregatableEntity.CALENDAR_EVENT,
                "duration_minutes",
                AggregateOp.SUM,
                alias="duration_sum",
            ),
            build_metric(
                AggregatableEntity.CALENDAR_EVENT,
                "duration_minutes",
                AggregateOp.AVG,
                alias="duration_avg",
            ),
            build_metric(
                AggregatableEntity.CALENDAR_EVENT,
                "duration_minutes",
                AggregateOp.MIN,
                alias="duration_min",
            ),
            build_metric(
                AggregatableEntity.CALENDAR_EVENT,
                "duration_minutes",
                AggregateOp.MAX,
                alias="duration_max",
            ),
        )

        with organization_context(organization):
            rows = execute_aggregate_plan(plan, CalendarEvent.objects.all())

        assert len(rows) == 2
        by_calendar = _rows_by_calendar(rows)
        assert by_calendar[calendar_a.id] == {
            "calendar_id": calendar_a.id,
            "count": 3,
            "duration_sum": 180.0,
            "duration_avg": 60.0,
            "duration_min": 30.0,
            "duration_max": 90.0,
        }
        assert by_calendar[calendar_b.id] == {
            "calendar_id": calendar_b.id,
            "count": 1,
            "duration_sum": 45.0,
            "duration_avg": 45.0,
            "duration_min": 45.0,
            "duration_max": 45.0,
        }

    def test_string_metrics(self, organization, calendar_a, calendar_b, events):
        plan = _event_plan(
            build_metric(
                AggregatableEntity.CALENDAR_EVENT, "title", AggregateOp.MIN, alias="title_min"
            ),
            build_metric(
                AggregatableEntity.CALENDAR_EVENT, "title", AggregateOp.MAX, alias="title_max"
            ),
            build_metric(
                AggregatableEntity.CALENDAR_EVENT,
                "title",
                AggregateOp.CONCAT,
                alias="title_concat",
                separator="; ",
            ),
        )

        with organization_context(organization):
            rows = execute_aggregate_plan(plan, CalendarEvent.objects.all())

        by_calendar = _rows_by_calendar(rows)
        assert by_calendar[calendar_a.id]["title_min"] == "Alpha"
        assert by_calendar[calendar_a.id]["title_max"] == "Charlie"
        assert by_calendar[calendar_a.id]["title_concat"] == "Alpha; Bravo; Charlie"
        assert by_calendar[calendar_b.id]["title_concat"] == "Delta"

    def test_concat_is_ordered_and_can_deduplicate(self, organization, calendar_a):
        _make_event(organization, calendar_a, external_id="d-2", title="Zulu", minutes=30)
        _make_event(
            organization, calendar_a, external_id="d-1", title="Alpha", minutes=30, day_offset=1
        )
        _make_event(
            organization, calendar_a, external_id="d-3", title="Alpha", minutes=30, day_offset=2
        )

        plan = _event_plan(
            build_metric(
                AggregatableEntity.CALENDAR_EVENT,
                "title",
                AggregateOp.CONCAT,
                alias="titles",
                separator=",",
            ),
            build_metric(
                AggregatableEntity.CALENDAR_EVENT,
                "title",
                AggregateOp.CONCAT,
                alias="unique_titles",
                separator=",",
                distinct=True,
            ),
        )

        with organization_context(organization):
            rows = execute_aggregate_plan(plan, CalendarEvent.objects.all())

        assert rows[0]["titles"] == "Alpha,Alpha,Zulu"
        assert rows[0]["unique_titles"] == "Alpha,Zulu"

    def test_temporal_and_boolean_metrics(self, organization, calendar_a, calendar_b, events):
        plan = _event_plan(
            build_metric(
                AggregatableEntity.CALENDAR_EVENT,
                "start_time",
                AggregateOp.MIN,
                alias="first_start",
            ),
            build_metric(
                AggregatableEntity.CALENDAR_EVENT,
                "start_time",
                AggregateOp.MAX,
                alias="last_start",
            ),
            build_metric(
                AggregatableEntity.CALENDAR_EVENT,
                "is_bundle_primary",
                AggregateOp.TRUE_COUNT,
                alias="primary_count",
            ),
            build_metric(
                AggregatableEntity.CALENDAR_EVENT,
                "is_bundle_primary",
                AggregateOp.FALSE_COUNT,
                alias="non_primary_count",
            ),
        )

        with organization_context(organization):
            rows = execute_aggregate_plan(plan, CalendarEvent.objects.all())

        row_a = _rows_by_calendar(rows)[calendar_a.id]
        assert row_a["first_start"] == BASE.replace(tzinfo=datetime.UTC)
        assert row_a["last_start"] == (BASE + datetime.timedelta(days=2)).replace(
            tzinfo=datetime.UTC
        )
        assert row_a["primary_count"] == 1
        assert row_a["non_primary_count"] == 2

    def test_grouping_by_two_dimensions(self, organization, calendar_a, calendar_b, events):
        plan = AggregateQueryPlan(
            entity=AggregatableEntity.CALENDAR_EVENT,
            dimensions=(
                build_dimension(AggregatableEntity.CALENDAR_EVENT, "calendar_id"),
                build_dimension(AggregatableEntity.CALENDAR_EVENT, "is_bundle_primary"),
            ),
            metrics=(
                build_metric(
                    AggregatableEntity.CALENDAR_EVENT, "count", AggregateOp.COUNT, alias="count"
                ),
            ),
        )

        with organization_context(organization):
            rows = execute_aggregate_plan(plan, CalendarEvent.objects.all())

        assert sorted(
            (row["calendar_id"], row["is_bundle_primary"], row["count"]) for row in rows
        ) == sorted(
            [
                (calendar_a.id, False, 2),
                (calendar_a.id, True, 1),
                (calendar_b.id, False, 1),
            ]
        )


@pytest.mark.django_db
class TestRelationCounts:
    def test_two_relations_do_not_double_count_each_other(self, organization, calendar_a):
        """Two events and two blocked times on one calendar means 2 and 2.

        Counted through the joins in a single ``annotate()`` this would be 4 and
        4: each event row pairs with each blocked-time row.
        """
        _make_event(organization, calendar_a, external_id="fan-1", title="One", minutes=30)
        _make_event(
            organization, calendar_a, external_id="fan-2", title="Two", minutes=30, day_offset=1
        )
        for index in range(2):
            baker.make(
                BlockedTime,
                organization=organization,
                calendar=calendar_a,
                external_id=f"block-{index}",
                timezone="UTC",
                start_time_tz_unaware=BASE + datetime.timedelta(days=index),
                end_time_tz_unaware=BASE + datetime.timedelta(days=index, minutes=30),
            )

        plan = AggregateQueryPlan(
            entity=AggregatableEntity.CALENDAR,
            dimensions=(build_dimension(AggregatableEntity.CALENDAR, "provider"),),
            metrics=(
                build_metric(
                    AggregatableEntity.CALENDAR, "count", AggregateOp.COUNT, alias="calendar_count"
                ),
                build_metric(
                    AggregatableEntity.CALENDAR, "events", AggregateOp.COUNT, alias="event_count"
                ),
                build_metric(
                    AggregatableEntity.CALENDAR,
                    "blocked_times",
                    AggregateOp.COUNT,
                    alias="blocked_time_count",
                ),
            ),
        )

        with organization_context(organization):
            rows = execute_aggregate_plan(plan, Calendar.objects.all())

        assert rows == [
            {
                "provider": CalendarProvider.INTERNAL.value,
                "calendar_count": 1,
                "event_count": 2,
                "blocked_time_count": 2,
            }
        ]

        # The control: the same two counts taken through the joins instead.
        # Without it this test would still pass over a fixture that happens not
        # to fan out, and would then be asserting nothing.
        with organization_context(organization):
            fanned_out = list(
                Calendar.objects.values("provider")
                .annotate(
                    event_count=DjangoCount("events__id"),
                    blocked_time_count=DjangoCount("blocked_times__id"),
                )
                .order_by("provider")
            )
        assert fanned_out == [
            {
                "provider": CalendarProvider.INTERNAL.value,
                "event_count": 4,
                "blocked_time_count": 4,
            }
        ]

    def test_relation_counts_sum_across_the_parents_in_a_group(
        self, organization, calendar_a, calendar_b
    ):
        _make_event(organization, calendar_a, external_id="sum-1", title="One", minutes=30)
        _make_event(
            organization, calendar_a, external_id="sum-2", title="Two", minutes=30, day_offset=1
        )
        _make_event(organization, calendar_b, external_id="sum-3", title="Three", minutes=30)

        plan = AggregateQueryPlan(
            entity=AggregatableEntity.CALENDAR,
            dimensions=(build_dimension(AggregatableEntity.CALENDAR, "provider"),),
            metrics=(
                build_metric(
                    AggregatableEntity.CALENDAR, "events", AggregateOp.COUNT, alias="event_count"
                ),
                build_metric(
                    AggregatableEntity.CALENDAR,
                    "available_times",
                    AggregateOp.COUNT,
                    alias="available_time_count",
                ),
            ),
        )

        with organization_context(organization):
            rows = execute_aggregate_plan(plan, Calendar.objects.all())

        assert rows == [
            {
                "provider": CalendarProvider.INTERNAL.value,
                "event_count": 3,
                "available_time_count": 0,
            }
        ]

    def test_relation_counts_are_organization_scoped(self, organization, calendar_a):
        """A second tenant's related rows contribute nothing to the subquery."""
        other_org = baker.make(Organization, name="Other Org")
        other_calendar = baker.make(
            Calendar,
            organization=other_org,
            name="Other Calendar",
            external_id="cal-other",
            provider=CalendarProvider.INTERNAL,
        )
        _make_event(organization, calendar_a, external_id="mine", title="Mine", minutes=30)
        _make_event(other_org, other_calendar, external_id="theirs", title="Theirs", minutes=30)
        baker.make(
            AvailableTime,
            organization=other_org,
            calendar=other_calendar,
            timezone="UTC",
            start_time_tz_unaware=BASE,
            end_time_tz_unaware=BASE + datetime.timedelta(hours=1),
        )

        plan = AggregateQueryPlan(
            entity=AggregatableEntity.CALENDAR,
            dimensions=(build_dimension(AggregatableEntity.CALENDAR, "provider"),),
            metrics=(
                build_metric(
                    AggregatableEntity.CALENDAR, "count", AggregateOp.COUNT, alias="calendar_count"
                ),
                build_metric(
                    AggregatableEntity.CALENDAR, "events", AggregateOp.COUNT, alias="event_count"
                ),
            ),
        )

        with organization_context(organization):
            rows = execute_aggregate_plan(plan, Calendar.objects.all())

        assert rows == [
            {
                "provider": CalendarProvider.INTERNAL.value,
                "calendar_count": 1,
                "event_count": 1,
            }
        ]


@pytest.mark.django_db
class TestGeneratedSql:
    def test_the_aggregate_is_one_grouped_query(self, organization, calendar_a, calendar_b, events):
        plan = _event_plan(
            build_metric(
                AggregatableEntity.CALENDAR_EVENT, "count", AggregateOp.COUNT, alias="count"
            ),
            build_metric(
                AggregatableEntity.CALENDAR_EVENT,
                "duration_minutes",
                AggregateOp.SUM,
                alias="duration_sum",
            ),
        )

        with organization_context(organization):
            with CaptureQueriesContext(connection) as captured:
                rows = execute_aggregate_plan(plan, CalendarEvent.objects.all())

        assert len(rows) == 2
        assert len(captured.captured_queries) == 1
        assert "GROUP BY" in captured.captured_queries[0]["sql"].upper()

    def test_relation_counts_stay_in_the_same_single_query(self, organization, calendar_a):
        _make_event(organization, calendar_a, external_id="one-query", title="One", minutes=30)
        plan = AggregateQueryPlan(
            entity=AggregatableEntity.CALENDAR,
            dimensions=(build_dimension(AggregatableEntity.CALENDAR, "provider"),),
            metrics=(
                build_metric(
                    AggregatableEntity.CALENDAR, "events", AggregateOp.COUNT, alias="event_count"
                ),
                build_metric(
                    AggregatableEntity.CALENDAR,
                    "blocked_times",
                    AggregateOp.COUNT,
                    alias="blocked_time_count",
                ),
            ),
        )

        with organization_context(organization):
            with CaptureQueriesContext(connection) as captured:
                execute_aggregate_plan(plan, Calendar.objects.all())

        assert len(captured.captured_queries) == 1


@pytest.mark.django_db
class TestOrderingAndSlicing:
    def test_rows_are_ordered_by_the_group_key_by_default(
        self, organization, calendar_a, calendar_b, events
    ):
        plan = _event_plan(
            build_metric(
                AggregatableEntity.CALENDAR_EVENT, "count", AggregateOp.COUNT, alias="count"
            )
        )

        with organization_context(organization):
            rows = execute_aggregate_plan(plan, CalendarEvent.objects.all())

        assert [row["calendar_id"] for row in rows] == sorted([calendar_a.id, calendar_b.id])

    def test_explicit_ordering_by_a_metric(self, organization, calendar_a, calendar_b, events):
        plan = _event_plan(
            build_metric(
                AggregatableEntity.CALENDAR_EVENT, "count", AggregateOp.COUNT, alias="count"
            ),
            order_by=(OrderSpec(alias="count", descending=True),),
        )

        with organization_context(organization):
            rows = execute_aggregate_plan(plan, CalendarEvent.objects.all())

        assert [row["count"] for row in rows] == [3, 1]

    def test_limit_takes_the_top_n(self, organization, calendar_a, calendar_b, events):
        plan = _event_plan(
            build_metric(
                AggregatableEntity.CALENDAR_EVENT, "count", AggregateOp.COUNT, alias="count"
            ),
            order_by=(OrderSpec(alias="count", descending=True),),
            limit=1,
        )

        with organization_context(organization):
            rows = execute_aggregate_plan(plan, CalendarEvent.objects.all())

        assert rows == [{"calendar_id": calendar_a.id, "count": 3}]

    def test_offset_skips_groups(self, organization, calendar_a, calendar_b, events):
        plan = _event_plan(
            build_metric(
                AggregatableEntity.CALENDAR_EVENT, "count", AggregateOp.COUNT, alias="count"
            ),
            order_by=(OrderSpec(alias="count", descending=True),),
            offset=1,
        )

        with organization_context(organization):
            rows = execute_aggregate_plan(plan, CalendarEvent.objects.all())

        assert rows == [{"calendar_id": calendar_b.id, "count": 1}]

    def test_ordering_by_an_unknown_alias_raises(self, organization):
        plan = _event_plan(
            build_metric(
                AggregatableEntity.CALENDAR_EVENT, "count", AggregateOp.COUNT, alias="count"
            ),
            order_by=(OrderSpec(alias="nope"),),
        )

        with organization_context(organization):
            with pytest.raises(UnknownAggregateFieldError):
                build_aggregate_queryset(plan, CalendarEvent.objects.all())


@pytest.mark.django_db
class TestExecutorValidation:
    @pytest.mark.parametrize("limit", [0, -1, 101])
    def test_limit_outside_the_permitted_range_raises(self, organization, limit):
        plan = _event_plan(
            build_metric(
                AggregatableEntity.CALENDAR_EVENT, "count", AggregateOp.COUNT, alias="count"
            ),
            limit=limit,
        )

        with organization_context(organization):
            with pytest.raises(AggregateLimitError) as excinfo:
                build_aggregate_queryset(plan, CalendarEvent.objects.all())

        assert str(excinfo.value) == "Limit must be between 1 and 100"

    def test_negative_offset_raises(self, organization):
        plan = _event_plan(
            build_metric(
                AggregatableEntity.CALENDAR_EVENT, "count", AggregateOp.COUNT, alias="count"
            ),
            offset=-1,
        )

        with organization_context(organization):
            with pytest.raises(AggregateLimitError) as excinfo:
                build_aggregate_queryset(plan, CalendarEvent.objects.all())

        assert str(excinfo.value) == "Offset must be non-negative"

    def test_a_queryset_for_another_model_is_refused(self, organization):
        plan = _event_plan(
            build_metric(
                AggregatableEntity.CALENDAR_EVENT, "count", AggregateOp.COUNT, alias="count"
            )
        )

        with organization_context(organization):
            with pytest.raises(InvalidAggregatePlanError):
                build_aggregate_queryset(plan, Calendar.objects.all())

    def test_a_dimension_the_entity_does_not_offer_is_refused(self, organization):
        plan = AggregateQueryPlan(
            entity=AggregatableEntity.CALENDAR_EVENT,
            dimensions=(DimensionSpec(alias="external_id", field_path="external_id"),),
            metrics=(),
        )

        with organization_context(organization):
            with pytest.raises(UnknownAggregateFieldError):
                build_aggregate_queryset(plan, CalendarEvent.objects.all())

    def test_a_metric_the_entity_does_not_offer_is_refused(self, organization):
        plan = _event_plan(
            MetricSpec(alias="external_id_max", field_path="external_id", op=AggregateOp.MAX)
        )

        with organization_context(organization):
            with pytest.raises(UnknownAggregateFieldError):
                build_aggregate_queryset(plan, CalendarEvent.objects.all())

    def test_a_computed_dimension_aliased_to_a_model_field_is_refused(self, organization):
        """Django raises a bare ``ValueError`` here; the engine must not leak one.

        ``build_dimension`` never produces this — a bucket gets a suffixed
        alias — but a hand-built plan can, and a 500 is the wrong answer.
        """
        plan = AggregateQueryPlan(
            entity=AggregatableEntity.CALENDAR_EVENT,
            dimensions=(
                DimensionSpec(
                    alias="title",
                    field_path="start_time",
                    granularity=TemporalGranularity.DAY,
                    tzinfo=ZoneInfo("UTC"),
                ),
            ),
            metrics=(),
        )

        with organization_context(organization):
            with pytest.raises(InvalidAggregatePlanError) as excinfo:
                build_aggregate_queryset(plan, CalendarEvent.objects.all())

        assert str(excinfo.value) == "A computed dimension may not be aliased to a model field name"

    def test_a_metric_aliased_to_a_model_field_is_refused(self, organization):
        plan = _event_plan(MetricSpec(alias="title", field_path="count", op=AggregateOp.COUNT))

        with organization_context(organization):
            with pytest.raises(InvalidAggregatePlanError):
                build_aggregate_queryset(plan, CalendarEvent.objects.all())

    def test_a_bucket_without_a_timezone_is_refused(self, organization):
        """Bucketing on the process timezone by accident is the failure to avoid."""
        plan = AggregateQueryPlan(
            entity=AggregatableEntity.CALENDAR_EVENT,
            dimensions=(
                DimensionSpec(
                    alias="start_time_day",
                    field_path="start_time",
                    granularity=TemporalGranularity.DAY,
                ),
            ),
            metrics=(),
        )

        with organization_context(organization):
            with pytest.raises(InvalidAggregatePlanError) as excinfo:
                build_aggregate_queryset(plan, CalendarEvent.objects.all())

        assert str(excinfo.value) == "A bucketed dimension must name the timezone it is bucketed in"


@pytest.mark.django_db
class TestFeaturesLaterPhasesBuild:
    """Plan features this phase does not execute must raise, not be dropped.

    Each of these changes the answer, so silently ignoring one would return a
    confidently wrong number. The phase that implements the feature deletes the
    matching case.
    """

    def test_a_having_clause_is_refused(self, organization):
        plan = _event_plan(
            build_metric(
                AggregatableEntity.CALENDAR_EVENT, "count", AggregateOp.COUNT, alias="count"
            ),
            having=HavingSpec(),
        )

        with organization_context(organization):
            with pytest.raises(NotImplementedError):
                build_aggregate_queryset(plan, CalendarEvent.objects.all())

    def test_a_window_spec_is_refused(self, organization):
        plan = _event_plan(
            build_metric(
                AggregatableEntity.CALENDAR_EVENT, "count", AggregateOp.COUNT, alias="count"
            ),
            window=WindowSpec(),
        )

        with organization_context(organization):
            with pytest.raises(NotImplementedError):
                build_aggregate_queryset(plan, CalendarEvent.objects.all())
