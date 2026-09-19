"""An ``AggregateQueryPlan`` is what the audit trail records and what the
executor runs, so it has to be frozen and it has to be the same object twice
for the same request.
"""

import dataclasses
import datetime

import pytest

from public_api.aggregations.errors import InvalidAggregatePlanError
from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    DimensionSpec,
    FilterBounds,
    MetricSpec,
    OrderSpec,
)


def _dimension(alias: str = "calendar_id") -> DimensionSpec:
    return DimensionSpec(alias=alias, field_path="calendar_fk_id")


def _metric(alias: str = "count") -> MetricSpec:
    return MetricSpec(alias=alias, field_path="count", op=AggregateOp.COUNT)


class TestPlanIsFrozen:
    def test_plan_cannot_be_mutated(self):
        plan = AggregateQueryPlan(
            entity=AggregatableEntity.CALENDAR_EVENT,
            dimensions=(_dimension(),),
            metrics=(_metric(),),
        )

        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.limit = 5

    def test_metric_options_are_read_only(self):
        metric = MetricSpec(
            alias="title_concat",
            field_path="title",
            op=AggregateOp.CONCAT,
            options={"separator": "; "},
        )

        with pytest.raises(TypeError):
            metric.options["separator"] = ","  # type: ignore[index]

    def test_metric_options_copy_the_mapping_they_were_given(self):
        options = {"separator": "; "}
        metric = MetricSpec(
            alias="title_concat", field_path="title", op=AggregateOp.CONCAT, options=options
        )

        options["separator"] = ","

        assert metric.options == {"separator": "; "}

    def test_filter_bounds_predicates_are_read_only(self):
        bounds = FilterBounds(predicates={"calendar_id": 7})

        with pytest.raises(TypeError):
            bounds.predicates["calendar_id"] = 8  # type: ignore[index]


class TestPlanIsDeterministic:
    def test_two_plans_from_the_same_inputs_are_equal(self):
        start = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        end = datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC)

        def build() -> AggregateQueryPlan:
            return AggregateQueryPlan(
                entity=AggregatableEntity.CALENDAR_EVENT,
                dimensions=(_dimension(),),
                metrics=(
                    _metric(),
                    MetricSpec(
                        alias="title_concat",
                        field_path="title",
                        op=AggregateOp.CONCAT,
                        options={"separator": "; ", "distinct": False},
                    ),
                ),
                filter_bounds=FilterBounds(start=start, end=end, predicates={"calendar_id": 7}),
                order_by=(OrderSpec(alias="count"),),
                limit=10,
                offset=0,
            )

        assert build() == build()

    def test_alias_helpers_report_the_plan_in_order(self):
        plan = AggregateQueryPlan(
            entity=AggregatableEntity.CALENDAR_EVENT,
            dimensions=(_dimension("calendar_id"), _dimension("timezone")),
            metrics=(_metric("count"), _metric("other_count")),
        )

        assert plan.dimension_aliases == ("calendar_id", "timezone")
        assert plan.metric_aliases == ("count", "other_count")


class TestPlanValidation:
    def test_an_alias_shared_by_a_dimension_and_a_metric_is_rejected(self):
        with pytest.raises(InvalidAggregatePlanError) as excinfo:
            AggregateQueryPlan(
                entity=AggregatableEntity.CALENDAR_EVENT,
                dimensions=(_dimension("calendar_id"),),
                metrics=(_metric("calendar_id"),),
            )

        assert (
            str(excinfo.value) == "Aggregate aliases must be unique across dimensions and metrics"
        )

    def test_two_metrics_sharing_an_alias_are_rejected(self):
        with pytest.raises(InvalidAggregatePlanError):
            AggregateQueryPlan(
                entity=AggregatableEntity.CALENDAR_EVENT,
                dimensions=(_dimension(),),
                metrics=(_metric("total"), _metric("total")),
            )

    def test_two_dimensions_sharing_an_alias_are_rejected(self):
        with pytest.raises(InvalidAggregatePlanError):
            AggregateQueryPlan(
                entity=AggregatableEntity.CALENDAR_EVENT,
                dimensions=(_dimension("same"), _dimension("same")),
                metrics=(_metric(),),
            )

    def test_an_empty_alias_is_rejected(self):
        with pytest.raises(InvalidAggregatePlanError) as excinfo:
            AggregateQueryPlan(
                entity=AggregatableEntity.CALENDAR_EVENT,
                dimensions=(_dimension(""),),
                metrics=(_metric(),),
            )

        assert str(excinfo.value) == "Aggregate aliases must not be empty"

    def test_a_plan_with_no_dimensions_is_rejected(self):
        with pytest.raises(InvalidAggregatePlanError) as excinfo:
            AggregateQueryPlan(
                entity=AggregatableEntity.CALENDAR_EVENT,
                dimensions=(),
                metrics=(_metric(),),
            )

        assert str(excinfo.value) == "An aggregate query must group by at least one dimension"

    def test_a_plan_with_no_metrics_is_allowed(self):
        plan = AggregateQueryPlan(
            entity=AggregatableEntity.CALENDAR_EVENT,
            dimensions=(_dimension(),),
            metrics=(),
        )

        assert plan.metric_aliases == ()
