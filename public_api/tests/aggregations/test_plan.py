"""Tests for the frozen plan primitives."""

import dataclasses
import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from graphql import GraphQLError

from calendar_integration.models import CalendarEvent
from public_api.aggregations import errors
from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    ComparisonOp,
    DimensionSpec,
    FilterBounds,
    HavingSpec,
    MetricComparison,
    MetricSpec,
    OrderDirection,
    OrderSpec,
    WindowSpec,
    dimension_alias,
    metric_alias,
)
from public_api.aggregations.types import TemporalGranularity
from public_api.queries import _slice_qs


SAO_PAULO = ZoneInfo("America/Sao_Paulo")


def _bounds() -> FilterBounds:
    return FilterBounds(
        field_path="start_time",
        start=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        end=datetime.datetime(2026, 1, 31, tzinfo=datetime.UTC),
    )


def _plan(**overrides: Any) -> AggregateQueryPlan:
    kwargs: dict[str, Any] = {
        "entity": AggregatableEntity.CALENDAR_EVENT,
        "dimensions": (DimensionSpec.of("calendar_id"),),
        "metrics": (MetricSpec.row_count(),),
        "filter_bounds": _bounds(),
    }
    kwargs.update(overrides)
    return AggregateQueryPlan(**kwargs)


class TestAliasHelpers:
    """Aliases are deterministic and cannot collide by construction."""

    def test_dimension_and_metric_aliases_are_namespaced_apart(self):
        assert dimension_alias("start_time") == "dim_start_time"
        assert metric_alias("start_time", AggregateOp.MIN) == "m_start_time_min"
        assert dimension_alias("start_time") != metric_alias("start_time", AggregateOp.MIN)

    def test_two_operations_over_one_field_get_different_aliases(self):
        assert metric_alias("duration_minutes", AggregateOp.SUM) != metric_alias(
            "duration_minutes", AggregateOp.AVG
        )

    def test_aliases_never_equal_the_bare_field_name(self):
        # A bare ``start_time`` alias is what Django refuses as conflicting with
        # a column, which is the entire reason for the prefixes.
        assert dimension_alias("start_time") != "start_time"
        assert metric_alias("title", AggregateOp.CONCAT) != "title"


class TestSpecsAreFrozen:
    """Nothing about a resolved request can change after it is built."""

    def test_metric_spec_is_frozen(self):
        spec = MetricSpec.of("duration_minutes", AggregateOp.SUM)

        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.alias = "other"  # type: ignore[misc]

    def test_metric_options_cannot_be_mutated_after_construction(self):
        spec = MetricSpec.of("title", AggregateOp.CONCAT, separator="; ")

        assert spec.options["separator"] == "; "
        with pytest.raises(TypeError):
            spec.options["separator"] = ","  # type: ignore[index]

    def test_mutating_the_source_options_dict_does_not_change_the_spec(self):
        source = {"separator": "; "}
        spec = MetricSpec(alias="m_x", field_path="title", op=AggregateOp.CONCAT, options=source)

        source["separator"] = "changed"

        assert spec.options["separator"] == "; "

    def test_dimension_spec_is_frozen(self):
        spec = DimensionSpec.of("calendar_id")

        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.field_path = "other"  # type: ignore[misc]

    def test_plan_is_frozen(self):
        plan = _plan()

        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.limit = 1  # type: ignore[misc]


class TestPlanConstructionIsDeterministic:
    """The same request twice is the same plan."""

    def test_two_identically_built_plans_are_equal(self):
        assert _plan() == _plan()

    def test_two_identically_built_metrics_are_equal(self):
        assert MetricSpec.of("title", AggregateOp.CONCAT, separator=",") == MetricSpec.of(
            "title", AggregateOp.CONCAT, separator=","
        )

    def test_aliases_are_reported_in_plan_order_dimensions_first(self):
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
            ),
        )

        assert plan.dimension_aliases == ("dim_calendar_id", "dim_start_time")
        assert plan.metric_aliases == ("m_count", "m_duration_minutes_sum")
        assert plan.aliases == (
            "dim_calendar_id",
            "dim_start_time",
            "m_count",
            "m_duration_minutes_sum",
        )

    def test_with_limits_returns_a_new_plan_and_leaves_the_original_alone(self):
        plan = _plan()

        paged = plan.with_limits(limit=10, offset=20)

        assert (paged.limit, paged.offset) == (10, 20)
        assert (plan.limit, plan.offset) == (errors.MAX_LIMIT, 0)
        assert paged is not plan


class TestAliasCollisionsAreRejected:
    """Two entries under one alias would silently overwrite each other."""

    def test_a_dimension_and_a_metric_sharing_an_alias_are_rejected(self):
        with pytest.raises(errors.AliasCollisionError, match="more than one"):
            _plan(
                dimensions=(DimensionSpec(alias="shared", field_path="calendar_id"),),
                metrics=(MetricSpec(alias="shared", field_path="", op=AggregateOp.COUNT),),
            )

    def test_two_metrics_sharing_an_alias_are_rejected(self):
        with pytest.raises(errors.AliasCollisionError):
            _plan(
                metrics=(
                    MetricSpec(alias="m_x", field_path="duration_minutes", op=AggregateOp.SUM),
                    MetricSpec(alias="m_x", field_path="duration_minutes", op=AggregateOp.AVG),
                )
            )

    def test_two_dimensions_sharing_an_alias_are_rejected(self):
        with pytest.raises(errors.AliasCollisionError):
            _plan(
                dimensions=(
                    DimensionSpec(alias="dim_x", field_path="calendar_id"),
                    DimensionSpec(alias="dim_x", field_path="appointment_type_id"),
                )
            )

    def test_canonically_aliased_entries_never_collide(self):
        plan = _plan(
            metrics=(
                MetricSpec.row_count(),
                MetricSpec.of("duration_minutes", AggregateOp.SUM),
                MetricSpec.of("duration_minutes", AggregateOp.AVG),
                MetricSpec.of("title", AggregateOp.MIN),
            )
        )

        assert len(set(plan.aliases)) == len(plan.aliases)


class TestPlanValidation:
    """A plan refuses to exist in a shape the executor could not honour."""

    def test_a_plan_with_no_dimension_is_refused(self):
        with pytest.raises(GraphQLError) as excinfo:
            _plan(dimensions=())

        assert str(excinfo.value) == errors.NO_DIMENSIONS

    @pytest.mark.parametrize("limit", [0, -1, errors.MAX_LIMIT + 1])
    def test_a_limit_outside_the_cap_is_refused_with_the_shared_wording(self, limit):
        with pytest.raises(GraphQLError) as excinfo:
            _plan(limit=limit)

        assert str(excinfo.value) == errors.LIMIT_OUT_OF_RANGE

    def test_a_negative_offset_is_refused_with_the_shared_wording(self):
        with pytest.raises(GraphQLError) as excinfo:
            _plan(offset=-1)

        assert str(excinfo.value) == errors.OFFSET_NEGATIVE

    def test_order_by_naming_an_unproduced_alias_is_refused(self):
        with pytest.raises(errors.InvalidPlanError, match="orderBy names"):
            _plan(order_by=(OrderSpec(alias="m_nothing"),))

    def test_order_by_naming_a_produced_alias_is_accepted(self):
        plan = _plan(order_by=(OrderSpec(alias="m_count", direction=OrderDirection.DESC),))

        assert plan.order_by[0].as_order_by() == "-m_count"

    def test_ascending_order_renders_without_a_minus(self):
        assert OrderSpec(alias="dim_calendar_id", direction=OrderDirection.ASC).as_order_by() == (
            "dim_calendar_id"
        )

    def test_having_referencing_an_unproduced_alias_is_refused(self):
        having = HavingSpec(
            comparisons=(MetricComparison(alias="m_missing", op=ComparisonOp.GT, value=1),)
        )

        with pytest.raises(errors.InvalidPlanError, match="m_missing"):
            _plan(having=having)

    def test_having_referencing_a_nested_unproduced_alias_is_refused(self):
        having = HavingSpec(
            and_=(
                HavingSpec(
                    comparisons=(MetricComparison(alias="m_count", op=ComparisonOp.GTE, value=2),)
                ),
                HavingSpec(
                    or_=(
                        HavingSpec(
                            comparisons=(
                                MetricComparison(alias="m_buried", op=ComparisonOp.LT, value=9),
                            )
                        ),
                    )
                ),
            )
        )

        with pytest.raises(errors.InvalidPlanError, match="m_buried"):
            _plan(having=having)

    def test_window_referencing_an_unproduced_alias_is_refused(self):
        with pytest.raises(errors.InvalidPlanError, match="dim_absent"):
            _plan(window=WindowSpec(partition_by=("dim_absent",)))

    def test_a_window_over_produced_aliases_is_accepted(self):
        plan = _plan(
            window=WindowSpec(
                partition_by=("dim_calendar_id",), order_by=(OrderSpec(alias="m_count"),)
            )
        )

        assert plan.window is not None


class TestDimensionSpec:
    """Bucketing needs a timezone, always."""

    def test_a_granularity_without_a_timezone_is_refused(self):
        with pytest.raises(errors.InvalidPlanError, match="no timezone"):
            DimensionSpec.of("start_time", granularity=TemporalGranularity.DAY)

    def test_a_granularity_with_a_timezone_is_accepted(self):
        spec = DimensionSpec.of(
            "start_time", granularity=TemporalGranularity.MONTH, tzinfo=SAO_PAULO
        )

        assert spec.granularity is TemporalGranularity.MONTH
        assert spec.tzinfo == SAO_PAULO

    def test_a_plain_dimension_needs_no_timezone(self):
        assert DimensionSpec.of("calendar_id").tzinfo is None


class TestFilterBounds:
    """The mandatory range is orientable and measurable."""

    def test_span_days_measures_the_range(self):
        assert _bounds().span_days == 30

    def test_an_inverted_range_is_refused(self):
        with pytest.raises(GraphQLError) as excinfo:
            FilterBounds(
                field_path="start_time",
                start=datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC),
                end=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
            )

        assert str(excinfo.value) == errors.INVALID_DATE_RANGE

    def test_an_empty_range_is_allowed(self):
        instant = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)

        assert FilterBounds(field_path="start_time", start=instant, end=instant).span_days == 0


class TestErrorMessages:
    """The wording is fixed here so no phase invents its own."""

    def test_date_range_message_names_the_maximum_and_nothing_else(self):
        message = str(errors.date_range_exceeded(90))

        assert message == "Date range exceeds the maximum of 90 days"

    def test_unknown_timezone_never_echoes_the_input(self):
        assert str(errors.unknown_timezone()) == "Unknown timezone"

    def test_time_budget_message_discloses_no_plan_detail(self):
        assert str(errors.time_budget_exceeded()) == "Aggregate query exceeded its time budget"

    @pytest.mark.parametrize(
        ("offset", "limit", "expected"),
        [
            (0, 0, errors.LIMIT_OUT_OF_RANGE),
            (0, errors.MAX_LIMIT + 1, errors.LIMIT_OUT_OF_RANGE),
            (-1, 10, errors.OFFSET_NEGATIVE),
        ],
    )
    def test_slice_refusals_are_worded_identically_to_the_list_fields(
        self, offset, limit, expected
    ):
        # An aggregate field and a list field refuse the same slice with the
        # same sentence. ``_slice_qs`` validates before it slices, so no query
        # is built or run here.
        queryset = CalendarEvent.original_manager.all()

        with pytest.raises(GraphQLError) as excinfo:
            _slice_qs(queryset, offset, limit)

        assert str(excinfo.value) == expected
