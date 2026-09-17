"""What an ``AggregateQueryPlan`` guarantees about itself.

A plan is the seam between "what the caller asked for" and "what ran": the
audit hook records one without executing it and the executor executes one
without re-deriving it. That only works if a plan is immutable, compares by
value, and refuses to be built in a shape whose result would be ambiguous.

No database and no models here -- a plan is built and compared without either.
"""

import dataclasses
import datetime
import zoneinfo
from typing import Any

from django.db.models import Q

import pytest

from public_api.aggregations.errors import (
    AliasCollisionError,
    EmptyAggregatePlanError,
    LimitOutOfRangeError,
    OffsetNegativeError,
    UnknownOrderAliasError,
)
from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    DimensionSpec,
    FilterBounds,
    HavingSpec,
    MetricSpec,
    OrderSpec,
    WindowSpec,
)
from public_api.aggregations.types import TemporalGranularity


SAO_PAULO = zoneinfo.ZoneInfo("America/Sao_Paulo")


def build_plan(**overrides: Any) -> AggregateQueryPlan:
    """A representative plan: group events by calendar, count and sum minutes."""
    kwargs: dict[str, Any] = {
        "entity": AggregatableEntity.CALENDAR_EVENT,
        "dimensions": (DimensionSpec(alias="calendar_id", field_path="calendar_id"),),
        "metrics": (
            MetricSpec.row_count(),
            MetricSpec(
                alias="duration_minutes_sum",
                field_path="duration_minutes",
                op=AggregateOp.SUM,
            ),
        ),
    }
    kwargs.update(overrides)
    return AggregateQueryPlan(**kwargs)


class TestAPlanIsFrozen:
    def test_the_plan_cannot_be_mutated_after_construction(self):
        plan = build_plan()

        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.limit = 5

    def test_the_specs_cannot_be_mutated_after_construction(self):
        plan = build_plan()

        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.metrics[0].alias = "total"

        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.dimensions[0].field_path = "organization_id"

    def test_metric_options_are_read_only(self):
        metric = MetricSpec(
            alias="title_concat",
            field_path="title",
            op=AggregateOp.CONCAT,
            options={"separator": "; "},
        )

        with pytest.raises(TypeError):
            metric.options["separator"] = ","

    def test_filter_bound_predicates_are_read_only(self):
        bounds = FilterBounds(predicates={"calendar_id": 7})

        with pytest.raises(TypeError):
            bounds.predicates["calendar_id"] = 8

    def test_a_mutable_sequence_argument_is_frozen_into_a_tuple(self):
        plan = build_plan(
            dimensions=[DimensionSpec(alias="calendar_id", field_path="calendar_id")],
        )

        assert isinstance(plan.dimensions, tuple)
        assert isinstance(plan.metrics, tuple)
        assert isinstance(plan.order_by, tuple)


class TestPlanConstructionIsDeterministic:
    def test_two_plans_built_the_same_way_are_equal(self):
        assert build_plan() == build_plan()

    def test_a_list_and_a_tuple_of_the_same_specs_produce_equal_plans(self):
        dimensions = [DimensionSpec(alias="calendar_id", field_path="calendar_id")]

        assert build_plan(dimensions=dimensions) == build_plan(dimensions=tuple(dimensions))

    def test_a_different_request_produces_a_different_plan(self):
        assert build_plan() != build_plan(limit=10)

    def test_the_audit_description_is_stable_and_holds_no_values(self):
        plan = build_plan(
            dimensions=(
                DimensionSpec(
                    alias="start_time_day",
                    field_path="start_time",
                    granularity=TemporalGranularity.DAY,
                    tzinfo=SAO_PAULO,
                ),
            ),
            metrics=(
                MetricSpec.row_count(),
                MetricSpec(
                    alias="title_concat",
                    field_path="title",
                    op=AggregateOp.CONCAT,
                    options={"separator": "; ", "distinct": True},
                ),
            ),
            filter_bounds=FilterBounds(
                start=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
                end=datetime.datetime(2026, 1, 31, tzinfo=datetime.UTC),
                predicates={"calendar_id": 42},
            ),
            limit=25,
        )

        assert plan.as_audit_dict() == {
            "entity": "calendar_event",
            "dimensions": [
                {
                    "alias": "start_time_day",
                    "field_path": "start_time",
                    "granularity": "DAY",
                    "timezone": "America/Sao_Paulo",
                }
            ],
            "metrics": [
                {
                    "alias": "count",
                    "field_path": "id",
                    "op": "count",
                    "options": {"distinct": False},
                },
                {
                    "alias": "title_concat",
                    "field_path": "title",
                    "op": "concat",
                    "options": {"distinct": True, "separator": "; "},
                },
            ],
            "filter_bounds": {
                "start": "2026-01-01T00:00:00+00:00",
                "end": "2026-01-31T00:00:00+00:00",
                "predicates": {"calendar_id": 42},
            },
            "has_having": False,
            "order_by": [],
            "window": None,
            "limit": 25,
            "offset": 0,
        }

        # Built twice from the same inputs, described identically.
        assert plan.as_audit_dict() == plan.as_audit_dict()


class TestAliasesAreUnique:
    """One alias, one row key. A clash would overwrite rather than merge."""

    def test_two_metrics_claiming_one_alias_are_rejected(self):
        with pytest.raises(AliasCollisionError):
            build_plan(
                metrics=(
                    MetricSpec(alias="total", field_path="duration_minutes", op=AggregateOp.SUM),
                    MetricSpec(alias="total", field_path="duration_minutes", op=AggregateOp.AVG),
                )
            )

    def test_two_dimensions_claiming_one_alias_are_rejected(self):
        with pytest.raises(AliasCollisionError):
            build_plan(
                dimensions=(
                    DimensionSpec(alias="bucket", field_path="start_time"),
                    DimensionSpec(alias="bucket", field_path="end_time"),
                )
            )

    def test_a_dimension_and_a_metric_claiming_one_alias_are_rejected(self):
        with pytest.raises(AliasCollisionError) as excinfo:
            build_plan(
                dimensions=(DimensionSpec(alias="count", field_path="calendar_id"),),
                metrics=(MetricSpec.row_count(),),
            )

        assert "count" in str(excinfo.value)

    def test_aliases_lists_every_row_key_the_plan_produces(self):
        plan = build_plan()

        assert plan.dimension_aliases == ("calendar_id",)
        assert plan.metric_aliases == ("count", "duration_minutes_sum")
        assert plan.aliases == ("calendar_id", "count", "duration_minutes_sum")


class TestAPlanMustAskForSomething:
    def test_a_plan_with_no_dimensions_is_rejected(self):
        # `.values()` with no arguments groups by every column, which dumps rows
        # rather than aggregating them.
        with pytest.raises(EmptyAggregatePlanError):
            build_plan(dimensions=())

    def test_a_plan_with_no_metrics_is_rejected(self):
        with pytest.raises(EmptyAggregatePlanError):
            build_plan(metrics=())


class TestBoundsAreCheckedAtConstruction:
    @pytest.mark.parametrize("limit", [0, -1, 101, 1000])
    def test_a_limit_outside_one_to_a_hundred_is_rejected(self, limit):
        with pytest.raises(LimitOutOfRangeError) as excinfo:
            build_plan(limit=limit)

        assert str(excinfo.value.message) == "Limit must be between 1 and 100"

    @pytest.mark.parametrize("limit", [1, 50, 100])
    def test_a_limit_inside_the_range_is_accepted(self, limit):
        assert build_plan(limit=limit).limit == limit

    def test_a_negative_offset_is_rejected(self):
        with pytest.raises(OffsetNegativeError) as excinfo:
            build_plan(offset=-1)

        assert str(excinfo.value.message) == "Offset must be non-negative"


class TestOrderingNamesSomethingThePlanProduces:
    def test_ordering_by_a_metric_alias_is_accepted(self):
        plan = build_plan(order_by=(OrderSpec(alias="count", descending=True),))

        assert plan.order_by[0].as_order_by() == "-count"

    def test_ordering_by_a_dimension_alias_is_accepted(self):
        plan = build_plan(order_by=(OrderSpec(alias="calendar_id"),))

        assert plan.order_by[0].as_order_by() == "calendar_id"

    def test_ordering_by_an_alias_the_plan_does_not_produce_is_rejected(self):
        # Django would resolve the unknown name against the model, adding a
        # column to the GROUP BY and silently changing the grouping.
        with pytest.raises(UnknownOrderAliasError):
            build_plan(order_by=(OrderSpec(alias="organization_id"),))


class TestOptionalClausesAreCarriedButNotInterpreted:
    def test_a_having_clause_is_carried_as_a_q_object(self):
        having = HavingSpec(predicate=Q(count__gt=2))
        plan = build_plan(having=having)

        assert plan.having is having
        assert plan.as_audit_dict()["has_having"] is True

    def test_a_window_is_described_for_the_audit_trail(self):
        plan = build_plan(
            window=WindowSpec(
                partition_by=("calendar_id",),
                order_by=(OrderSpec(alias="calendar_id"),),
            )
        )

        assert plan.as_audit_dict()["window"] == {
            "partition_by": ["calendar_id"],
            "order_by": ["calendar_id"],
            "frame_type": "ROWS",
            "frame_start": "UNBOUNDED_PRECEDING",
            "frame_end": "CURRENT_ROW",
        }


class TestRowCountHelper:
    def test_the_row_count_helper_counts_the_primary_key(self):
        metric = MetricSpec.row_count()

        assert metric.alias == "count"
        assert metric.field_path == "id"
        assert metric.op is AggregateOp.COUNT
        assert metric.options == {"distinct": False}

    def test_the_row_count_helper_takes_an_alias_and_a_distinct_flag(self):
        metric = MetricSpec.row_count(alias="events", distinct=True)

        assert metric.alias == "events"
        assert metric.options == {"distinct": True}
