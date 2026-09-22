"""A plan is a value: frozen, comparable, and validated at construction.

The audit hook and the executor both read the same object, so a plan that
can be mutated after it is recorded would let the two disagree about what
ran.
"""

from zoneinfo import ZoneInfo

import pytest

from public_api.aggregations import (
    MAX_AGGREGATE_LIMIT,
    MIN_AGGREGATE_LIMIT,
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    AliasCollisionError,
    ComparisonOp,
    DimensionSpec,
    FilterBounds,
    HavingComparison,
    HavingSpec,
    InvalidPlanError,
    MetricSpec,
    OrderDirection,
    OrderSpec,
    TemporalGranularity,
    WindowSpec,
)


def _plan(**overrides):
    defaults = {
        "entity": AggregatableEntity.CALENDAR_EVENT,
        "dimensions": (DimensionSpec(alias="calendar_id", field_path="calendar_fk_id"),),
        "metrics": (MetricSpec(alias="count", field_path="id", op=AggregateOp.COUNT),),
    }
    return AggregateQueryPlan(**{**defaults, **overrides})


class TestMetricSpec:
    def test_options_default_to_empty_and_are_immutable(self):
        metric = MetricSpec(alias="count", field_path="id", op=AggregateOp.COUNT)
        assert dict(metric.options) == {}
        with pytest.raises(TypeError):
            metric.options["distinct"] = True  # type: ignore[index]

    def test_options_are_copied_so_the_caller_cannot_mutate_them_later(self):
        supplied = {"separator": "; "}
        metric = MetricSpec(
            alias="title_concat", field_path="title", op=AggregateOp.CONCAT, options=supplied
        )
        supplied["separator"] = "|"
        assert metric.options["separator"] == "; "

    def test_attributes_cannot_be_reassigned(self):
        metric = MetricSpec(alias="count", field_path="id", op=AggregateOp.COUNT)
        with pytest.raises(AttributeError):
            metric.alias = "other"  # type: ignore[misc]

    def test_an_empty_alias_is_rejected(self):
        with pytest.raises(InvalidPlanError):
            MetricSpec(alias="", field_path="id", op=AggregateOp.COUNT)


class TestDimensionSpec:
    def test_attributes_cannot_be_reassigned(self):
        dimension = DimensionSpec(alias="calendar_id", field_path="calendar_fk_id")
        with pytest.raises(AttributeError):
            dimension.field_path = "id"  # type: ignore[misc]

    def test_an_empty_alias_is_rejected(self):
        with pytest.raises(InvalidPlanError):
            DimensionSpec(alias="", field_path="calendar_fk_id")

    def test_a_granularity_without_a_timezone_is_rejected(self):
        with pytest.raises(InvalidPlanError):
            DimensionSpec(alias="day", field_path="start_time", granularity=TemporalGranularity.DAY)

    def test_a_timezone_without_a_granularity_is_rejected(self):
        with pytest.raises(InvalidPlanError):
            DimensionSpec(alias="day", field_path="start_time", tzinfo=ZoneInfo("UTC"))

    def test_a_granularity_with_a_timezone_is_accepted(self):
        dimension = DimensionSpec(
            alias="day",
            field_path="start_time",
            granularity=TemporalGranularity.DAY,
            tzinfo=ZoneInfo("UTC"),
        )
        assert dimension.granularity is TemporalGranularity.DAY


class TestFilterBounds:
    def test_predicates_hold_only_tuples_of_ids_and_are_immutable(self):
        bounds = FilterBounds(predicates={"calendar_fk_id": [3, 1, 2]})
        assert bounds.predicates == {"calendar_fk_id": (3, 1, 2)}
        with pytest.raises(TypeError):
            bounds.predicates["calendar_fk_id"] = ()  # type: ignore[index]

    def test_bounds_default_to_unset(self):
        bounds = FilterBounds()
        assert bounds.start is None
        assert bounds.end is None
        assert dict(bounds.predicates) == {}


class TestHavingSpec:
    def test_a_leaf_comparison_is_accepted(self):
        spec = HavingSpec(
            comparison=HavingComparison(alias="count", comparison=ComparisonOp.GT, value=2)
        )
        assert spec.referenced_aliases() == ("count",)

    def test_setting_nothing_is_rejected(self):
        with pytest.raises(InvalidPlanError):
            HavingSpec()

    def test_setting_comparison_and_all_of_together_is_rejected(self):
        leaf = HavingSpec(
            comparison=HavingComparison(alias="count", comparison=ComparisonOp.GT, value=2)
        )
        with pytest.raises(InvalidPlanError):
            HavingSpec(comparison=leaf.comparison, all_of=(leaf,))

    def test_setting_all_of_and_any_of_together_is_rejected(self):
        leaf = HavingSpec(
            comparison=HavingComparison(alias="count", comparison=ComparisonOp.GT, value=2)
        )
        with pytest.raises(InvalidPlanError):
            HavingSpec(all_of=(leaf,), any_of=(leaf,))

    def test_referenced_aliases_collects_every_leaf_in_the_tree(self):
        spec = HavingSpec(
            all_of=(
                HavingSpec(
                    comparison=HavingComparison(alias="count", comparison=ComparisonOp.GT, value=2)
                ),
                HavingSpec(
                    any_of=(
                        HavingSpec(
                            comparison=HavingComparison(
                                alias="duration_sum", comparison=ComparisonOp.LT, value=100
                            )
                        ),
                        HavingSpec(
                            comparison=HavingComparison(
                                alias="calendar_id", comparison=ComparisonOp.NE, value=1
                            )
                        ),
                    )
                ),
            )
        )
        assert set(spec.referenced_aliases()) == {"count", "duration_sum", "calendar_id"}


class TestPlanConstruction:
    def test_two_identically_built_plans_are_equal(self):
        assert _plan() == _plan()

    def test_plans_and_their_parts_are_hashable(self):
        """Values, so they can key a dict or land in a set.

        Every one of these holds a mapping internally, and the hash a frozen
        dataclass generates would try to hash the mapping.
        """
        metric = MetricSpec(
            alias="title_concat",
            field_path="title",
            op=AggregateOp.CONCAT,
            options={"separator": "; "},
        )
        bounds = FilterBounds(predicates={"calendar_fk_id": (1, 2)})
        plan = _plan(metrics=(metric,), filter_bounds=bounds)

        assert hash(metric) == hash(
            MetricSpec(
                alias="title_concat",
                field_path="title",
                op=AggregateOp.CONCAT,
                options={"separator": "; "},
            )
        )
        assert hash(bounds) == hash(FilterBounds(predicates={"calendar_fk_id": (1, 2)}))
        assert len({plan, _plan(metrics=(metric,), filter_bounds=bounds)}) == 1

    def test_a_plan_cannot_be_mutated(self):
        plan = _plan()
        with pytest.raises(AttributeError):
            plan.limit = 5  # type: ignore[misc]

    def test_defaults_are_the_documented_ones(self):
        plan = _plan()
        assert plan.limit == MAX_AGGREGATE_LIMIT
        assert plan.offset == 0
        assert plan.having is None
        assert plan.window is None
        assert plan.order_by == ()
        assert plan.filter_bounds == FilterBounds()

    def test_alias_helpers_preserve_request_order(self):
        plan = _plan(
            dimensions=(
                DimensionSpec(alias="calendar_id", field_path="calendar_fk_id"),
                DimensionSpec(alias="timezone", field_path="timezone"),
            ),
            metrics=(
                MetricSpec(alias="count", field_path="id", op=AggregateOp.COUNT),
                MetricSpec(alias="duration_sum", field_path="duration_minutes", op=AggregateOp.SUM),
            ),
        )
        assert plan.dimension_aliases == ("calendar_id", "timezone")
        assert plan.metric_aliases == ("count", "duration_sum")

    def test_window_slot_still_carries_its_placeholder_object(self):
        """Phase 6 owns ``WindowSpec``'s shape; until then it stays an empty
        marker, unlike ``HavingSpec``, which this phase gives real fields."""
        plan = _plan(window=WindowSpec())
        assert plan.window == WindowSpec()

    def test_a_having_clause_referencing_a_known_metric_is_accepted(self):
        plan = _plan(
            having=HavingSpec(
                comparison=HavingComparison(alias="count", comparison=ComparisonOp.GT, value=2)
            )
        )
        assert plan.having is not None
        assert plan.having.comparison is not None
        assert plan.having.comparison.alias == "count"


class TestPlanValidation:
    def test_a_dimension_and_a_metric_cannot_share_an_alias(self):
        with pytest.raises(AliasCollisionError) as excinfo:
            _plan(
                dimensions=(DimensionSpec(alias="total", field_path="calendar_fk_id"),),
                metrics=(MetricSpec(alias="total", field_path="id", op=AggregateOp.COUNT),),
            )
        assert "total" in str(excinfo.value)

    def test_two_metrics_cannot_share_an_alias(self):
        with pytest.raises(AliasCollisionError):
            _plan(
                metrics=(
                    MetricSpec(alias="agg", field_path="duration_minutes", op=AggregateOp.SUM),
                    MetricSpec(alias="agg", field_path="duration_minutes", op=AggregateOp.AVG),
                )
            )

    def test_two_dimensions_cannot_share_an_alias(self):
        with pytest.raises(AliasCollisionError):
            _plan(
                dimensions=(
                    DimensionSpec(alias="key", field_path="calendar_fk_id"),
                    DimensionSpec(alias="key", field_path="timezone"),
                )
            )

    def test_a_plan_with_no_dimension_is_rejected(self):
        with pytest.raises(InvalidPlanError):
            _plan(dimensions=())

    def test_a_plan_with_no_metric_is_rejected(self):
        """The mirror of the empty-dimensions rule, and the same failure.

        ``.values("calendar_fk_id")`` with no aggregate after it is not a
        ``GROUP BY``: it returns one row per source row with the key
        repeating, and nothing about the result says so.
        """
        with pytest.raises(InvalidPlanError):
            _plan(metrics=())

    def test_ordering_by_an_unknown_alias_is_rejected(self):
        with pytest.raises(InvalidPlanError) as excinfo:
            _plan(order_by=(OrderSpec(alias="nope"),))
        assert "nope" in str(excinfo.value)

    def test_ordering_by_a_dimension_or_a_metric_alias_is_accepted(self):
        plan = _plan(
            order_by=(
                OrderSpec(alias="count", direction=OrderDirection.DESC),
                OrderSpec(alias="calendar_id", direction=OrderDirection.ASC),
            )
        )
        assert [order.alias for order in plan.order_by] == ["count", "calendar_id"]

    def test_filtering_on_an_unknown_alias_is_rejected(self):
        with pytest.raises(InvalidPlanError) as excinfo:
            _plan(
                having=HavingSpec(
                    comparison=HavingComparison(alias="nope", comparison=ComparisonOp.GT, value=1)
                )
            )
        assert "nope" in str(excinfo.value)

    def test_filtering_on_a_known_alias_inside_an_and_or_tree_is_accepted(self):
        plan = _plan(
            having=HavingSpec(
                all_of=(
                    HavingSpec(
                        comparison=HavingComparison(
                            alias="count", comparison=ComparisonOp.GT, value=1
                        )
                    ),
                    HavingSpec(
                        any_of=(
                            HavingSpec(
                                comparison=HavingComparison(
                                    alias="calendar_id", comparison=ComparisonOp.EQ, value=5
                                )
                            ),
                        )
                    ),
                )
            )
        )
        assert plan.having is not None
        assert set(plan.having.referenced_aliases()) == {"count", "calendar_id"}

    @pytest.mark.parametrize("limit", [0, -1, MAX_AGGREGATE_LIMIT + 1])
    def test_a_limit_outside_the_band_is_rejected(self, limit):
        with pytest.raises(InvalidPlanError):
            _plan(limit=limit)

    @pytest.mark.parametrize("limit", [MIN_AGGREGATE_LIMIT, 50, MAX_AGGREGATE_LIMIT])
    def test_a_limit_inside_the_band_is_accepted(self, limit):
        assert _plan(limit=limit).limit == limit

    def test_a_negative_offset_is_rejected(self):
        with pytest.raises(InvalidPlanError):
            _plan(offset=-1)
