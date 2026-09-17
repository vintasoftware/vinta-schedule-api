"""What a window may and may not ask for, checked without touching the database.

The two rules the phase names are here, and both exist because the alternative
is a plausible wrong number rather than an error:

* **A window needs an ordering.** A running total accumulates in *some* order;
  with none specified the database picks one, and the same query returns
  different numbers on different runs with nothing in the response to say so.
* **`partitionBy` may name only dimensions the query grouped by.** A partition
  addresses columns the grouped rows carry. Naming anything else would widen
  the `GROUP BY`, which changes the numbers the window is computed over while
  still returning a result that looks like an answer.
"""

import zoneinfo

import pytest

from public_api.aggregations.errors import (
    WINDOW_ORDERING_REQUIRED_MESSAGE,
    MisplacedFrameBoundError,
    UngroupedPartitionKeyError,
    WindowFrameOffsetError,
    WindowFrameOrderError,
    WindowOrderingRequiredError,
    WindowSourceMissingError,
)
from public_api.aggregations.ordering import ORDER_KEY_ENUM_BY_ENTITY
from public_api.aggregations.plan import (
    AggregatableEntity,
    DimensionSpec,
    OrderSpec,
    WindowFunction,
    WindowMetricSpec,
    WindowSpec,
)
from public_api.aggregations.types import TemporalGranularity
from public_api.aggregations.windows import (
    WINDOW_INPUT_TYPE_BY_ENTITY,
    WINDOW_METRIC_DEFINITIONS_BY_ENTITY,
    WINDOW_METRICS_TYPE_BY_ENTITY,
    WINDOW_ORDER_INPUT_TYPE_BY_ENTITY,
    WindowBound,
    WindowFrameInput,
    WindowFrameType,
    window_from_input,
)


EVENT = AggregatableEntity.CALENDAR_EVENT

CALENDAR_DIMENSION = DimensionSpec(alias="calendar_id", field_path="calendar_id")
DAY_DIMENSION = DimensionSpec(
    alias="start_time_day",
    field_path="start_time",
    granularity=TemporalGranularity.DAY,
    tzinfo=zoneinfo.ZoneInfo("UTC"),
)


def _order(**kwargs):
    return WINDOW_ORDER_INPUT_TYPE_BY_ENTITY[EVENT](**kwargs)


def _window(**kwargs):
    kwargs.setdefault("partition_by", None)
    kwargs.setdefault("frame", None)
    return WINDOW_INPUT_TYPE_BY_ENTITY[EVENT](**kwargs)


def _keys():
    return ORDER_KEY_ENUM_BY_ENTITY[EVENT]


class TestOrderingIsRequired:
    def test_a_window_spec_with_no_ordering_is_refused(self):
        with pytest.raises(WindowOrderingRequiredError) as exc_info:
            WindowSpec(order_by=())
        assert exc_info.value.message == WINDOW_ORDERING_REQUIRED_MESSAGE

    def test_an_empty_order_by_list_through_the_input_is_refused(self):
        """`orderBy: []` satisfies `[X!]!` but still names no ordering."""
        with pytest.raises(WindowOrderingRequiredError):
            window_from_input(
                EVENT,
                _window(order_by=[]),
                ("running_count",),
                (DAY_DIMENSION,),
            )

    def test_a_window_with_an_ordering_is_accepted(self):
        spec, _metrics = window_from_input(
            EVENT,
            _window(order_by=[_order(key=_keys().START_TIME)]),
            ("running_count",),
            (DAY_DIMENSION,),
        )
        assert spec is not None
        assert [order.as_order_by() for order in spec.order_by] == ["start_time_day"]


class TestPartitionKeysMustBeGrouped:
    def test_partitioning_by_a_dimension_the_query_grouped_by_is_accepted(self):
        spec, _metrics = window_from_input(
            EVENT,
            _window(
                order_by=[_order(key=_keys().START_TIME)],
                partition_by=[_keys().CALENDAR_ID],
            ),
            ("running_count",),
            (CALENDAR_DIMENSION, DAY_DIMENSION),
        )
        assert spec is not None
        assert spec.partition_by == ("calendar_id",)

    def test_partitioning_by_a_dimension_absent_from_group_by_is_refused(self):
        with pytest.raises(UngroupedPartitionKeyError) as exc_info:
            window_from_input(
                EVENT,
                _window(
                    order_by=[_order(key=_keys().START_TIME)],
                    partition_by=[_keys().CALENDAR_ID],
                ),
                ("running_count",),
                (DAY_DIMENSION,),
            )
        assert "calendar_id" in exc_info.value.message
        assert "groupBy" in exc_info.value.message

    def test_a_bucketed_partition_key_resolves_to_its_granularity_alias(self):
        """The row key of a bucketed dimension carries its bucket size."""
        spec, _metrics = window_from_input(
            EVENT,
            _window(
                order_by=[_order(key=_keys().START_TIME)],
                partition_by=[_keys().START_TIME],
            ),
            ("running_count",),
            (DAY_DIMENSION,),
        )
        assert spec is not None
        assert spec.partition_by == ("start_time_day",)


class TestFrameValidation:
    def test_a_preceding_bound_without_an_offset_is_refused(self):
        with pytest.raises(WindowFrameOffsetError) as exc_info:
            WindowSpec(order_by=(OrderSpec(alias="day"),), frame_start="PRECEDING")
        assert "PRECEDING" in exc_info.value.message

    def test_a_following_bound_without_an_offset_is_refused(self):
        with pytest.raises(WindowFrameOffsetError):
            WindowSpec(order_by=(OrderSpec(alias="day"),), frame_end="FOLLOWING")

    def test_a_negative_offset_is_refused(self):
        """The bound carries the direction; the offset is a distance."""
        with pytest.raises(WindowFrameOffsetError):
            WindowSpec(
                order_by=(OrderSpec(alias="day"),),
                frame_start="PRECEDING",
                frame_start_offset=-2,
            )

    def test_the_unbounded_and_current_row_bounds_need_no_offset(self):
        spec = WindowSpec(order_by=(OrderSpec(alias="day"),))
        assert spec.frame_start == "UNBOUNDED_PRECEDING"
        assert spec.frame_end == "CURRENT_ROW"


class TestUnboundedBoundsMustBeOnTheirOwnSide:
    """An unbounded bound on the wrong side is read as the opposite edge.

    Django hands an unbounded bound to the backend as `None` and reads it
    *positionally*: `None` at the start is `UNBOUNDED PRECEDING`, at the end it
    is `UNBOUNDED FOLLOWING`. So `end: UNBOUNDED_PRECEDING` would compile to a
    frame spanning the whole partition, and `movingAvgCount` would come back as
    the whole-partition average, labelled a moving average, with no error.
    """

    def test_a_frame_cannot_end_at_unbounded_preceding(self):
        with pytest.raises(MisplacedFrameBoundError) as exc_info:
            WindowSpec(
                order_by=(OrderSpec(alias="day"),),
                frame_start="UNBOUNDED_PRECEDING",
                frame_end="UNBOUNDED_PRECEDING",
            )
        assert "UNBOUNDED_PRECEDING" in exc_info.value.message
        assert "end" in exc_info.value.message

    def test_a_frame_cannot_start_at_unbounded_following(self):
        with pytest.raises(MisplacedFrameBoundError) as exc_info:
            WindowSpec(
                order_by=(OrderSpec(alias="day"),),
                frame_start="UNBOUNDED_FOLLOWING",
                frame_end="UNBOUNDED_FOLLOWING",
            )
        assert "UNBOUNDED_FOLLOWING" in exc_info.value.message
        assert "start" in exc_info.value.message

    def test_unbounded_on_its_own_side_is_accepted(self):
        spec = WindowSpec(
            order_by=(OrderSpec(alias="day"),),
            frame_start="UNBOUNDED_PRECEDING",
            frame_end="UNBOUNDED_FOLLOWING",
        )
        assert spec.frame_start == "UNBOUNDED_PRECEDING"
        assert spec.frame_end == "UNBOUNDED_FOLLOWING"


class TestFrameBoundsMustBeInOrder:
    """A start that sorts after its end describes no rows.

    Django's backend does catch the both-integers case, but as a bare
    `ValueError` raised while compiling the SQL -- which reaches a partner as an
    internal error rather than as the validation failure it is.
    """

    def test_a_frame_starting_after_the_current_row_and_ending_on_it_is_refused(self):
        with pytest.raises(WindowFrameOrderError):
            WindowSpec(
                order_by=(OrderSpec(alias="day"),),
                frame_start="FOLLOWING",
                frame_start_offset=2,
                frame_end="CURRENT_ROW",
            )

    def test_a_frame_ending_before_the_current_row_it_starts_on_is_refused(self):
        with pytest.raises(WindowFrameOrderError):
            WindowSpec(
                order_by=(OrderSpec(alias="day"),),
                frame_start="CURRENT_ROW",
                frame_end="PRECEDING",
                frame_end_offset=2,
            )

    def test_two_preceding_bounds_in_the_wrong_order_are_refused(self):
        """`1 PRECEDING` to `2 PRECEDING` runs backwards."""
        with pytest.raises(WindowFrameOrderError):
            WindowSpec(
                order_by=(OrderSpec(alias="day"),),
                frame_start="PRECEDING",
                frame_start_offset=1,
                frame_end="PRECEDING",
                frame_end_offset=2,
            )

    @pytest.mark.parametrize(
        ("start", "start_offset", "end", "end_offset"),
        [
            ("PRECEDING", 2, "CURRENT_ROW", None),
            ("PRECEDING", 2, "PRECEDING", 1),
            ("PRECEDING", 1, "FOLLOWING", 1),
            ("CURRENT_ROW", None, "FOLLOWING", 3),
            ("UNBOUNDED_PRECEDING", None, "CURRENT_ROW", None),
            ("CURRENT_ROW", None, "UNBOUNDED_FOLLOWING", None),
        ],
    )
    def test_well_ordered_frames_are_accepted(self, start, start_offset, end, end_offset):
        spec = WindowSpec(
            order_by=(OrderSpec(alias="day"),),
            frame_start=start,
            frame_start_offset=start_offset,
            frame_end=end,
            frame_end_offset=end_offset,
        )
        assert spec.frame_start == start
        assert spec.frame_end == end

    def test_a_frame_spanning_the_same_row_twice_is_accepted(self):
        """`CURRENT ROW` to `CURRENT ROW` is a one-row frame, not a backwards one."""
        spec = WindowSpec(
            order_by=(OrderSpec(alias="day"),),
            frame_start="CURRENT_ROW",
            frame_end="CURRENT_ROW",
        )
        assert spec.frame_start == spec.frame_end == "CURRENT_ROW"

    def test_a_frame_is_carried_onto_the_spec(self):
        spec, _metrics = window_from_input(
            EVENT,
            _window(
                order_by=[_order(key=_keys().START_TIME)],
                frame=WindowFrameInput(
                    type=WindowFrameType.ROWS,
                    start=WindowBound.PRECEDING,
                    end=WindowBound.CURRENT_ROW,
                    start_offset=2,
                ),
            ),
            ("moving_avg_count",),
            (DAY_DIMENSION,),
        )
        assert spec is not None
        assert spec.frame_start == "PRECEDING"
        assert spec.frame_start_offset == 2
        assert spec.frame_end == "CURRENT_ROW"


class TestWindowMetricSpecs:
    def test_a_windowed_aggregate_with_no_source_is_refused(self):
        with pytest.raises(WindowSourceMissingError):
            WindowMetricSpec(alias="_w_running_count", function=WindowFunction.RUNNING_SUM)

    def test_rank_needs_no_source(self):
        spec = WindowMetricSpec(alias="_w_rank", function=WindowFunction.RANK)
        assert spec.source_alias == ""


class TestSelectionDrivesWhatIsComputed:
    def test_a_window_argument_with_no_sub_selection_computes_nothing(self):
        """No `OVER` clause is worth emitting for a column nobody asked for."""
        spec, metrics = window_from_input(
            EVENT, _window(order_by=[_order(key=_keys().START_TIME)]), (), (DAY_DIMENSION,)
        )
        assert spec is None
        assert metrics == ()

    def test_no_window_argument_produces_no_spec(self):
        spec, metrics = window_from_input(EVENT, None, ("running_count",), (DAY_DIMENSION,))
        assert spec is None
        assert metrics == ()

    def test_only_the_selected_window_metrics_are_built(self):
        spec, _metrics = window_from_input(
            EVENT,
            _window(order_by=[_order(key=_keys().START_TIME)]),
            ("running_count", "rank"),
            (DAY_DIMENSION,),
        )
        assert spec is not None
        assert [metric.alias for metric in spec.metrics] == ["_w_running_count", "_w_rank"]

    def test_a_window_over_an_unselected_metric_asks_for_it_to_be_annotated(self):
        """`runningDurationMinutes` needs the duration sum whether or not it shows."""
        spec, metrics = window_from_input(
            EVENT,
            _window(order_by=[_order(key=_keys().START_TIME)]),
            ("running_duration_minutes",),
            (DAY_DIMENSION,),
        )
        assert spec is not None
        assert spec.metrics[0].source_alias == "duration_minutes_sum"
        assert [metric.alias for metric in metrics] == ["duration_minutes_sum"]


class TestGeneratedWindowSurface:
    def test_every_entity_has_the_four_count_based_window_metrics(self):
        for entity in AggregatableEntity:
            names = {d.name for d in WINDOW_METRIC_DEFINITIONS_BY_ENTITY[entity]}
            assert {"running_count", "moving_avg_count", "rank", "percent_of_total"} <= names

    def test_numeric_fields_gain_a_running_total_and_a_moving_average(self):
        from public_api.aggregations.registry import AggregateKind, get_registration

        for entity in AggregatableEntity:
            names = {d.name for d in WINDOW_METRIC_DEFINITIONS_BY_ENTITY[entity]}
            for spec in get_registration(entity).aggregatable:
                if spec.kind is not AggregateKind.NUMERIC:
                    assert f"running_{spec.name}" not in names
                    continue
                assert f"running_{spec.name}" in names
                assert f"moving_avg_{spec.name}" in names

    def test_window_metric_aliases_cannot_collide_with_a_metric_alias(self):
        """Every window alias carries the reserved prefix."""
        for entity in AggregatableEntity:
            for definition in WINDOW_METRIC_DEFINITIONS_BY_ENTITY[entity]:
                assert definition.alias.startswith("_w_")

    def test_the_calendar_event_metrics_type_matches_the_plans_shape(self):
        import dataclasses

        fields = {f.name for f in dataclasses.fields(WINDOW_METRICS_TYPE_BY_ENTITY[EVENT])}
        assert fields == {
            "running_count",
            "moving_avg_count",
            "rank",
            "percent_of_total",
            "running_duration_minutes",
            "moving_avg_duration_minutes",
        }
