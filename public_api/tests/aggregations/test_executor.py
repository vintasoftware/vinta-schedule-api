"""The executor against real rows in a real database.

Every assertion here is on numbers Postgres produced. Two properties get the
most attention because they are the ones that fail quietly:

* **one query**, with a ``GROUP BY`` in it -- a Python fallback that looped
  rows and summed them would satisfy every value assertion in this file, so
  the query count and the emitted SQL are asserted alongside the numbers.
* **no join fan-out** -- two counts over two different relations in one
  ``annotate()`` multiply each other. The rows are built so a fan-out would
  report 4 where the answer is 2, rather than being off by an amount that
  reads like a plausible total.
"""

import datetime
import uuid
from zoneinfo import ZoneInfo

from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AppointmentType,
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    EventExternalAttendance,
    ExternalAttendee,
    ResourceAllocation,
)
from common.organization_context import organization_context
from organizations.models import Organization
from public_api.aggregations import (
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    AliasCollisionError,
    ComparisonOp,
    DimensionSpec,
    HavingComparison,
    HavingSpec,
    InvalidPlanError,
    MetricSpec,
    OrderDirection,
    OrderSpec,
    QuerysetModelMismatchError,
    TemporalGranularity,
    UnknownMetricFieldError,
    UnsupportedOperationError,
    WindowFunctionKind,
    WindowSpec,
    build_aggregate_queryset,
)


pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_org(label: str) -> Organization:
    return Organization.objects.create(
        name=f"{label} {uuid.uuid4().hex[:8]}", should_sync_rooms=False
    )


def _make_calendar(org: Organization, label: str, **overrides) -> Calendar:
    return Calendar.objects.create(
        organization=org,
        name=label,
        external_id=f"{label}-{uuid.uuid4().hex[:8]}",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        **overrides,
    )


def _make_event(
    org: Organization,
    calendar: Calendar,
    *,
    title: str,
    start: datetime.datetime,
    minutes: int,
    is_bundle_primary: bool = False,
) -> CalendarEvent:
    return CalendarEvent.objects.create(
        organization=org,
        calendar=calendar,
        title=title,
        description=f"description of {title}",
        external_id=f"ev-{uuid.uuid4().hex[:12]}",
        start_time_tz_unaware=start,
        end_time_tz_unaware=start + datetime.timedelta(minutes=minutes),
        timezone="UTC",
        is_bundle_primary=is_bundle_primary,
    )


def _make_blocked_time(org: Organization, calendar: Calendar, start: datetime.datetime):
    return BlockedTime.objects.create(
        organization=org,
        calendar=calendar,
        reason="maintenance",
        external_id=f"bt-{uuid.uuid4().hex[:12]}",
        start_time_tz_unaware=start,
        end_time_tz_unaware=start + datetime.timedelta(minutes=30),
        timezone="UTC",
    )


def _make_available_time(org: Organization, calendar: Calendar, start: datetime.datetime):
    return AvailableTime.objects.create(
        organization=org,
        calendar=calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=start + datetime.timedelta(minutes=60),
        timezone="UTC",
    )


@pytest.fixture
def org():
    return _make_org("Aggregation Org")


@pytest.fixture
def other_org():
    return _make_org("Other Org")


COUNT_METRIC = MetricSpec(alias="count", field_path="id", op=AggregateOp.COUNT)
BY_CALENDAR = DimensionSpec(alias="calendar_id", field_path="calendar_fk_id")

BASE_START = datetime.datetime(2026, 10, 1, 9, 0)


def _event_plan(**overrides) -> AggregateQueryPlan:
    defaults = {
        "entity": AggregatableEntity.CALENDAR_EVENT,
        "dimensions": (BY_CALENDAR,),
        "metrics": (COUNT_METRIC,),
    }
    return AggregateQueryPlan(**{**defaults, **overrides})


def _rows(plan: AggregateQueryPlan, queryset) -> list[dict]:
    return list(build_aggregate_queryset(plan, queryset))


# ---------------------------------------------------------------------------
# Grouping and numeric aggregates
# ---------------------------------------------------------------------------


class TestGroupedAggregates:
    def test_one_row_per_distinct_key_with_correct_numeric_aggregates(self, org):
        with organization_context(org):
            first = _make_calendar(org, "First")
            second = _make_calendar(org, "Second")
            _make_event(org, first, title="A", start=BASE_START, minutes=30)
            _make_event(org, first, title="B", start=BASE_START, minutes=90)
            _make_event(org, second, title="C", start=BASE_START, minutes=45)

            plan = _event_plan(
                metrics=(
                    COUNT_METRIC,
                    MetricSpec(
                        alias="duration_sum", field_path="duration_minutes", op=AggregateOp.SUM
                    ),
                    MetricSpec(
                        alias="duration_avg", field_path="duration_minutes", op=AggregateOp.AVG
                    ),
                    MetricSpec(
                        alias="duration_min", field_path="duration_minutes", op=AggregateOp.MIN
                    ),
                    MetricSpec(
                        alias="duration_max", field_path="duration_minutes", op=AggregateOp.MAX
                    ),
                ),
            )
            rows = _rows(plan, CalendarEvent.objects.all())

        assert rows == [
            {
                "calendar_id": first.id,
                "count": 2,
                "duration_sum": 120.0,
                "duration_avg": 60.0,
                "duration_min": 30.0,
                "duration_max": 90.0,
            },
            {
                "calendar_id": second.id,
                "count": 1,
                "duration_sum": 45.0,
                "duration_avg": 45.0,
                "duration_min": 45.0,
                "duration_max": 45.0,
            },
        ]

    def test_grouping_by_two_dimensions_yields_only_non_empty_combinations(self, org):
        with organization_context(org):
            first = _make_calendar(org, "First")
            second = _make_calendar(org, "Second")
            _make_event(org, first, title="A", start=BASE_START, minutes=30)
            _make_event(org, first, title="B", start=BASE_START, minutes=30, is_bundle_primary=True)
            _make_event(org, second, title="C", start=BASE_START, minutes=30)

            plan = _event_plan(
                dimensions=(
                    BY_CALENDAR,
                    DimensionSpec(alias="is_bundle_primary", field_path="is_bundle_primary"),
                ),
            )
            rows = _rows(plan, CalendarEvent.objects.all())

        # The cross product would hold four combinations; only the three that
        # have rows come back. Buckets are sparse everywhere, not only in time.
        assert rows == [
            {"calendar_id": first.id, "is_bundle_primary": False, "count": 1},
            {"calendar_id": first.id, "is_bundle_primary": True, "count": 1},
            {"calendar_id": second.id, "is_bundle_primary": False, "count": 1},
        ]

    def test_string_aggregates_come_back_from_the_database(self, org):
        with organization_context(org):
            calendar = _make_calendar(org, "Only")
            _make_event(org, calendar, title="Bravo", start=BASE_START, minutes=30)
            _make_event(org, calendar, title="Alfa", start=BASE_START, minutes=30)
            _make_event(org, calendar, title="Charlie", start=BASE_START, minutes=30)

            plan = _event_plan(
                metrics=(
                    MetricSpec(alias="title_min", field_path="title", op=AggregateOp.MIN),
                    MetricSpec(alias="title_max", field_path="title", op=AggregateOp.MAX),
                    MetricSpec(
                        alias="title_concat",
                        field_path="title",
                        op=AggregateOp.CONCAT,
                        options={"separator": "; "},
                    ),
                ),
            )
            rows = _rows(plan, CalendarEvent.objects.all())

        assert len(rows) == 1
        assert rows[0]["title_min"] == "Alfa"
        assert rows[0]["title_max"] == "Charlie"
        # Asserted in full rather than sorted: the aggregate carries its own
        # ORDER BY, so the order is part of the contract and not an accident
        # of insertion order. The rows were inserted B, A, C.
        assert rows[0]["title_concat"] == "Alfa; Bravo; Charlie"

    def test_distinct_concat_collapses_repeats(self, org):
        with organization_context(org):
            calendar = _make_calendar(org, "Only")
            for title in ("Alfa", "Alfa", "Bravo"):
                _make_event(org, calendar, title=title, start=BASE_START, minutes=30)

            plan = _event_plan(
                metrics=(
                    MetricSpec(
                        alias="title_concat",
                        field_path="title",
                        op=AggregateOp.CONCAT,
                        options={"separator": "|", "distinct": True},
                    ),
                ),
            )
            rows = _rows(plan, CalendarEvent.objects.all())

        assert rows[0]["title_concat"] == "Alfa|Bravo"

    def test_concat_emits_an_order_by_so_the_result_is_reproducible(self, org):
        with organization_context(org):
            calendar = _make_calendar(org, "Only")
            for title in ("Charlie", "Alfa", "Bravo"):
                _make_event(org, calendar, title=title, start=BASE_START, minutes=30)

            plan = _event_plan(
                metrics=(
                    MetricSpec(alias="title_concat", field_path="title", op=AggregateOp.CONCAT),
                ),
            )
            queryset = build_aggregate_queryset(plan, CalendarEvent.objects.all())
            with CaptureQueriesContext(connection) as captured:
                rows = list(queryset)

        assert rows[0]["title_concat"] == "Alfa,Bravo,Charlie"
        # The ordering is Postgres', inside the aggregate -- a Python sort
        # after the fact would satisfy the assertion above but not this one.
        assert "STRING_AGG" in captured.captured_queries[0]["sql"].upper()
        assert "ORDER BY" in captured.captured_queries[0]["sql"].upper()

    def test_datetime_aggregates_come_back_as_instants(self, org):
        with organization_context(org):
            calendar = _make_calendar(org, "Only")
            _make_event(org, calendar, title="Early", start=BASE_START, minutes=30)
            _make_event(
                org,
                calendar,
                title="Late",
                start=BASE_START + datetime.timedelta(hours=5),
                minutes=30,
            )

            plan = _event_plan(
                metrics=(
                    MetricSpec(alias="start_min", field_path="start_time", op=AggregateOp.MIN),
                    MetricSpec(alias="start_max", field_path="start_time", op=AggregateOp.MAX),
                ),
            )
            rows = _rows(plan, CalendarEvent.objects.all())

        assert rows[0]["start_min"] == BASE_START.replace(tzinfo=datetime.UTC)
        assert rows[0]["start_max"] == (BASE_START + datetime.timedelta(hours=5)).replace(
            tzinfo=datetime.UTC
        )

    def test_boolean_aggregates_split_the_group(self, org):
        with organization_context(org):
            calendar = _make_calendar(org, "Only")
            _make_event(
                org, calendar, title="A", start=BASE_START, minutes=30, is_bundle_primary=True
            )
            _make_event(
                org, calendar, title="B", start=BASE_START, minutes=30, is_bundle_primary=True
            )
            _make_event(
                org, calendar, title="C", start=BASE_START, minutes=30, is_bundle_primary=False
            )

            plan = _event_plan(
                metrics=(
                    COUNT_METRIC,
                    MetricSpec(
                        alias="primary_true",
                        field_path="is_bundle_primary",
                        op=AggregateOp.TRUE_COUNT,
                    ),
                    MetricSpec(
                        alias="primary_false",
                        field_path="is_bundle_primary",
                        op=AggregateOp.FALSE_COUNT,
                    ),
                ),
            )
            rows = _rows(plan, CalendarEvent.objects.all())

        assert rows == [
            {"calendar_id": calendar.id, "count": 3, "primary_true": 2, "primary_false": 1}
        ]


# ---------------------------------------------------------------------------
# The fan-out property
# ---------------------------------------------------------------------------


class TestRelationCounts:
    def test_two_relation_counts_do_not_multiply_each_other(self, org):
        """Two events and two blocked times per calendar: the answer is 2 and 2.

        A pair of ``Count()`` calls over the two relations in one
        ``annotate()`` joins both tables and reports 4 and 4.
        """
        with organization_context(org):
            first = _make_calendar(org, "First")
            second = _make_calendar(org, "Second")
            for calendar in (first, second):
                _make_event(org, calendar, title="A", start=BASE_START, minutes=30)
                _make_event(org, calendar, title="B", start=BASE_START, minutes=30)
                _make_blocked_time(org, calendar, BASE_START)
                _make_blocked_time(org, calendar, BASE_START + datetime.timedelta(hours=1))
            # A third relation, at a different cardinality, so a fan-out cannot
            # coincidentally produce the right number.
            _make_available_time(org, first, BASE_START)

            plan = AggregateQueryPlan(
                entity=AggregatableEntity.CALENDAR,
                dimensions=(DimensionSpec(alias="id", field_path="id"),),
                metrics=(
                    MetricSpec(alias="event_count", field_path="event_count", op=AggregateOp.COUNT),
                    MetricSpec(
                        alias="blocked_time_count",
                        field_path="blocked_time_count",
                        op=AggregateOp.COUNT,
                    ),
                    MetricSpec(
                        alias="available_time_count",
                        field_path="available_time_count",
                        op=AggregateOp.COUNT,
                    ),
                ),
            )
            rows = _rows(plan, Calendar.objects.all())

        assert rows == [
            {
                "id": first.id,
                "event_count": 2,
                "blocked_time_count": 2,
                "available_time_count": 1,
            },
            {
                "id": second.id,
                "event_count": 2,
                "blocked_time_count": 2,
                "available_time_count": 0,
            },
        ]

    def test_three_event_relation_counts_stay_independent(self, org):
        """Same property one level down, over three relations of ``CalendarEvent``."""
        with organization_context(org):
            calendar = _make_calendar(org, "Only")
            event = _make_event(org, calendar, title="A", start=BASE_START, minutes=30)
            for index in range(2):
                attendee = ExternalAttendee.objects.create(
                    organization=org, email=f"a{index}-{uuid.uuid4().hex[:6]}@example.com"
                )
                EventExternalAttendance.objects.create(
                    organization=org, event=event, external_attendee=attendee
                )
            for index in range(3):
                ResourceAllocation.objects.create(
                    organization=org,
                    event=event,
                    calendar=_make_calendar(org, f"Room {index}"),
                )

            plan = _event_plan(
                dimensions=(DimensionSpec(alias="id", field_path="id"),),
                metrics=(
                    MetricSpec(
                        alias="external_attendance_count",
                        field_path="external_attendance_count",
                        op=AggregateOp.COUNT,
                    ),
                    MetricSpec(
                        alias="resource_allocation_count",
                        field_path="resource_allocation_count",
                        op=AggregateOp.COUNT,
                    ),
                ),
            )
            rows = _rows(plan, CalendarEvent.objects.filter(id=event.id))

        assert rows == [
            {
                "id": event.id,
                "external_attendance_count": 2,
                "resource_allocation_count": 3,
            }
        ]

    def test_a_relation_count_over_a_non_key_dimension_totals_the_group(self, org):
        """Grouped by provider, a relation count is the group's total."""
        with organization_context(org):
            first = _make_calendar(org, "First")
            second = _make_calendar(org, "Second")
            _make_event(org, first, title="A", start=BASE_START, minutes=30)
            _make_event(org, first, title="B", start=BASE_START, minutes=30)
            _make_event(org, second, title="C", start=BASE_START, minutes=30)

            plan = AggregateQueryPlan(
                entity=AggregatableEntity.CALENDAR,
                dimensions=(DimensionSpec(alias="provider", field_path="provider"),),
                metrics=(
                    MetricSpec(alias="calendar_count", field_path="id", op=AggregateOp.COUNT),
                    MetricSpec(alias="event_count", field_path="event_count", op=AggregateOp.COUNT),
                ),
            )
            rows = _rows(plan, Calendar.objects.all())

        assert rows == [
            {
                "provider": CalendarProvider.INTERNAL,
                "calendar_count": 2,
                "event_count": 3,
            }
        ]


# ---------------------------------------------------------------------------
# The query itself
# ---------------------------------------------------------------------------


class TestGeneratedQuery:
    def test_one_query_containing_a_group_by(self, org):
        with organization_context(org):
            calendar = _make_calendar(org, "Only")
            _make_event(org, calendar, title="A", start=BASE_START, minutes=30)
            _make_event(org, calendar, title="B", start=BASE_START, minutes=60)

            plan = _event_plan(
                metrics=(
                    COUNT_METRIC,
                    MetricSpec(
                        alias="duration_sum", field_path="duration_minutes", op=AggregateOp.SUM
                    ),
                ),
            )
            queryset = build_aggregate_queryset(plan, CalendarEvent.objects.all())
            with CaptureQueriesContext(connection) as captured:
                rows = list(queryset)

        assert rows == [{"calendar_id": calendar.id, "count": 2, "duration_sum": 90.0}]
        assert len(captured.captured_queries) == 1
        assert "GROUP BY" in captured.captured_queries[0]["sql"].upper()

    def test_relation_counts_still_cost_one_query(self, org):
        with organization_context(org):
            calendar = _make_calendar(org, "Only")
            _make_event(org, calendar, title="A", start=BASE_START, minutes=30)
            _make_blocked_time(org, calendar, BASE_START)

            plan = AggregateQueryPlan(
                entity=AggregatableEntity.CALENDAR,
                dimensions=(DimensionSpec(alias="id", field_path="id"),),
                metrics=(
                    MetricSpec(alias="event_count", field_path="event_count", op=AggregateOp.COUNT),
                    MetricSpec(
                        alias="blocked_time_count",
                        field_path="blocked_time_count",
                        op=AggregateOp.COUNT,
                    ),
                ),
            )
            queryset = build_aggregate_queryset(plan, Calendar.objects.all())
            with CaptureQueriesContext(connection) as captured:
                rows = list(queryset)

        assert rows == [{"id": calendar.id, "event_count": 1, "blocked_time_count": 1}]
        assert len(captured.captured_queries) == 1
        sql = captured.captured_queries[0]["sql"].upper()
        assert "GROUP BY" in sql
        # Summed subqueries, not joined relations -- the shape that keeps the
        # two counts from multiplying each other.
        assert "JOIN" not in sql

    def test_the_returned_queryset_is_lazy(self, org):
        with organization_context(org):
            _make_calendar(org, "Only")
            with CaptureQueriesContext(connection) as captured:
                build_aggregate_queryset(_event_plan(), CalendarEvent.objects.all())

        assert captured.captured_queries == []

    def test_groups_are_ordered_by_the_key_when_nothing_else_is_asked_for(self, org):
        with organization_context(org):
            calendars = [_make_calendar(org, f"Cal {index}") for index in range(4)]
            for calendar in calendars:
                _make_event(org, calendar, title="A", start=BASE_START, minutes=30)

            rows = _rows(_event_plan(), CalendarEvent.objects.all())

        assert [row["calendar_id"] for row in rows] == sorted(calendar.id for calendar in calendars)

    def test_ordering_by_a_metric_with_a_limit_takes_the_top_n(self, org):
        with organization_context(org):
            busiest = _make_calendar(org, "Busiest")
            middle = _make_calendar(org, "Middle")
            quietest = _make_calendar(org, "Quietest")
            for calendar, event_count in ((busiest, 3), (middle, 2), (quietest, 1)):
                for index in range(event_count):
                    _make_event(org, calendar, title=f"E{index}", start=BASE_START, minutes=30)

            plan = _event_plan(
                order_by=(OrderSpec(alias="count", direction=OrderDirection.DESC),),
                limit=2,
            )
            rows = _rows(plan, CalendarEvent.objects.all())

        assert rows == [
            {"calendar_id": busiest.id, "count": 3},
            {"calendar_id": middle.id, "count": 2},
        ]

    def test_ties_break_on_the_group_key_so_paging_is_stable(self, org):
        with organization_context(org):
            calendars = [_make_calendar(org, f"Cal {index}") for index in range(5)]
            for calendar in calendars:
                _make_event(org, calendar, title="A", start=BASE_START, minutes=30)

            plan = _event_plan(order_by=(OrderSpec(alias="count", direction=OrderDirection.DESC),))
            first_page = _rows(plan, CalendarEvent.objects.all())
            second_page = _rows(plan, CalendarEvent.objects.all())

            paged = _rows(
                _event_plan(
                    order_by=(OrderSpec(alias="count", direction=OrderDirection.DESC),),
                    limit=2,
                    offset=2,
                ),
                CalendarEvent.objects.all(),
            )

        assert first_page == second_page
        assert paged == first_page[2:4]


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


class TestTenantScoping:
    def test_another_organizations_rows_contribute_nothing(self, org, other_org):
        with organization_context(other_org):
            outsider = _make_calendar(other_org, "Outsider")
            for index in range(7):
                _make_event(other_org, outsider, title=f"X{index}", start=BASE_START, minutes=120)

        with organization_context(org):
            mine = _make_calendar(org, "Mine")
            _make_event(org, mine, title="A", start=BASE_START, minutes=30)

            plan = _event_plan(
                metrics=(
                    COUNT_METRIC,
                    MetricSpec(
                        alias="duration_sum", field_path="duration_minutes", op=AggregateOp.SUM
                    ),
                ),
            )
            rows = _rows(plan, CalendarEvent.objects.all())

        assert rows == [{"calendar_id": mine.id, "count": 1, "duration_sum": 30.0}]

    def test_relation_count_subqueries_are_scoped_too(self, org, other_org):
        """The subquery goes through ``objects``, so it carries the bound org."""
        with organization_context(other_org):
            outsider = _make_calendar(other_org, "Outsider")
            _make_event(other_org, outsider, title="X", start=BASE_START, minutes=30)
            _make_event(other_org, outsider, title="Y", start=BASE_START, minutes=30)

        with organization_context(org):
            mine = _make_calendar(org, "Mine")
            _make_event(org, mine, title="A", start=BASE_START, minutes=30)

            plan = AggregateQueryPlan(
                entity=AggregatableEntity.CALENDAR,
                dimensions=(DimensionSpec(alias="id", field_path="id"),),
                metrics=(
                    MetricSpec(alias="event_count", field_path="event_count", op=AggregateOp.COUNT),
                ),
            )
            rows = _rows(plan, Calendar.objects.all())

        assert rows == [{"id": mine.id, "event_count": 1}]


# ---------------------------------------------------------------------------
# Every other entity
# ---------------------------------------------------------------------------


class TestOtherEntities:
    def test_blocked_times_group_and_aggregate(self, org):
        with organization_context(org):
            calendar = _make_calendar(org, "Only")
            _make_blocked_time(org, calendar, BASE_START)
            _make_blocked_time(org, calendar, BASE_START + datetime.timedelta(hours=1))

            plan = AggregateQueryPlan(
                entity=AggregatableEntity.BLOCKED_TIME,
                dimensions=(BY_CALENDAR,),
                metrics=(
                    COUNT_METRIC,
                    MetricSpec(
                        alias="duration_sum", field_path="duration_minutes", op=AggregateOp.SUM
                    ),
                    MetricSpec(alias="reason_min", field_path="reason", op=AggregateOp.MIN),
                ),
            )
            rows = _rows(plan, BlockedTime.objects.all())

        assert rows == [
            {
                "calendar_id": calendar.id,
                "count": 2,
                "duration_sum": 60.0,
                "reason_min": "maintenance",
            }
        ]

    def test_available_times_group_and_aggregate(self, org):
        with organization_context(org):
            calendar = _make_calendar(org, "Only")
            _make_available_time(org, calendar, BASE_START)

            plan = AggregateQueryPlan(
                entity=AggregatableEntity.AVAILABLE_TIME,
                dimensions=(BY_CALENDAR,),
                metrics=(
                    COUNT_METRIC,
                    MetricSpec(
                        alias="duration_sum", field_path="duration_minutes", op=AggregateOp.SUM
                    ),
                ),
            )
            rows = _rows(plan, AvailableTime.objects.all())

        assert rows == [{"calendar_id": calendar.id, "count": 1, "duration_sum": 60.0}]

    def test_appointment_types_aggregate_their_duration_column(self, org):
        with organization_context(org):
            AppointmentType.objects.create(
                organization=org,
                name=f"Short {uuid.uuid4().hex[:6]}",
                accepts_public_scheduling=True,
                duration=datetime.timedelta(minutes=20),
            )
            AppointmentType.objects.create(
                organization=org,
                name=f"Long {uuid.uuid4().hex[:6]}",
                accepts_public_scheduling=True,
                duration=datetime.timedelta(minutes=40),
            )

            plan = AggregateQueryPlan(
                entity=AggregatableEntity.APPOINTMENT_TYPE,
                dimensions=(
                    DimensionSpec(
                        alias="accepts_public_scheduling", field_path="accepts_public_scheduling"
                    ),
                ),
                metrics=(
                    COUNT_METRIC,
                    MetricSpec(
                        alias="duration_avg", field_path="duration_minutes", op=AggregateOp.AVG
                    ),
                ),
            )
            rows = _rows(plan, AppointmentType.objects.all())

        assert rows == [{"accepts_public_scheduling": True, "count": 2, "duration_avg": 30.0}]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class TestRefusals:
    def test_a_queryset_over_the_wrong_model_is_refused(self, org):
        with organization_context(org), pytest.raises(QuerysetModelMismatchError):
            build_aggregate_queryset(_event_plan(), Calendar.objects.all())

    def test_a_metric_field_the_entity_does_not_expose_is_refused(self, org):
        plan = _event_plan(
            metrics=(MetricSpec(alias="nope", field_path="secret", op=AggregateOp.MIN),)
        )
        with organization_context(org), pytest.raises(UnknownMetricFieldError):
            build_aggregate_queryset(plan, CalendarEvent.objects.all())

    def test_an_operation_the_field_kind_rejects_is_refused(self, org):
        plan = _event_plan(
            metrics=(MetricSpec(alias="title_sum", field_path="title", op=AggregateOp.SUM),)
        )
        with organization_context(org), pytest.raises(UnsupportedOperationError):
            build_aggregate_queryset(plan, CalendarEvent.objects.all())

    def test_a_relation_count_asked_for_with_another_operation_is_refused(self, org):
        plan = AggregateQueryPlan(
            entity=AggregatableEntity.CALENDAR,
            dimensions=(DimensionSpec(alias="id", field_path="id"),),
            metrics=(MetricSpec(alias="event_sum", field_path="event_count", op=AggregateOp.SUM),),
        )
        with organization_context(org), pytest.raises(UnsupportedOperationError):
            build_aggregate_queryset(plan, Calendar.objects.all())

    def test_a_metric_alias_that_shadows_a_column_is_refused(self, org):
        """``MIN(title)`` cannot be called ``title``: Django rejects the annotation."""
        plan = _event_plan(
            metrics=(MetricSpec(alias="title", field_path="title", op=AggregateOp.MIN),)
        )
        with organization_context(org), pytest.raises(AliasCollisionError):
            build_aggregate_queryset(plan, CalendarEvent.objects.all())

    def test_a_dimension_alias_that_shadows_another_column_is_refused(self, org):
        plan = _event_plan(dimensions=(DimensionSpec(alias="title", field_path="calendar_fk_id"),))
        with organization_context(org), pytest.raises(AliasCollisionError):
            build_aggregate_queryset(plan, CalendarEvent.objects.all())

    def test_a_count_over_something_that_is_neither_id_nor_a_relation_is_refused(self, org):
        plan = _event_plan(
            metrics=(MetricSpec(alias="title_count", field_path="title", op=AggregateOp.COUNT),)
        )
        with organization_context(org), pytest.raises(InvalidPlanError):
            build_aggregate_queryset(plan, CalendarEvent.objects.all())


class TestFeaturesLaterPhasesOwn:
    """Refused loudly, never dropped silently -- see the executor's docstring."""

    def test_a_temporal_granularity_is_no_longer_refused(self, org):
        """Bucketing landed with the group-by dimensions phase.

        The behaviour it now has is covered by ``test_bucketing.py``; this
        only records that the refusal was removed rather than relaxed
        elsewhere.
        """
        plan = _event_plan(
            dimensions=(
                DimensionSpec(
                    alias="day",
                    field_path="start_time",
                    granularity=TemporalGranularity.DAY,
                    tzinfo=ZoneInfo("America/Sao_Paulo"),
                ),
            )
        )
        with organization_context(org):
            build_aggregate_queryset(plan, CalendarEvent.objects.all())

    def test_a_having_clause_is_no_longer_refused(self, org):
        """HAVING landed with this phase.

        The behaviour it now has is covered by ``test_having_execution.py``;
        this only records that the refusal was removed rather than relaxed
        elsewhere.
        """
        plan = _event_plan(
            having=HavingSpec(
                comparison=HavingComparison(alias="count", comparison=ComparisonOp.GT, value=0)
            )
        )
        with organization_context(org):
            build_aggregate_queryset(plan, CalendarEvent.objects.all())

    def test_a_window_clause_is_built_rather_than_refused(self, org):
        """The executor used to refuse a window outright; the window-function
        phase replaced that refusal with the ``OVER`` clause itself.

        The behaviour it now has is covered by ``test_window_execution.py``;
        this only records that the refusal was removed rather than relaxed
        elsewhere.
        """
        plan = _event_plan(
            window=WindowSpec(
                metric_alias="count",
                order_by=(OrderSpec(alias="count", direction=OrderDirection.ASC),),
                functions=(WindowFunctionKind.RUNNING_TOTAL,),
            )
        )
        with organization_context(org):
            queryset = build_aggregate_queryset(plan, CalendarEvent.objects.all())
            assert "OVER (" in str(queryset.query)
