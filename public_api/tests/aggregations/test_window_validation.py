"""What a window may and may not ask for, refused before a query is built.

Two layers, and the split matters. ``resolve_window`` is what a GraphQL
document reaches, so its refusals are ``AggregationRequestError``s a resolver
may let travel back to the caller. ``WindowSpec`` / ``AggregateQueryPlan``
are what code reaches, so theirs are engine-level ``InvalidPlanError``s -- the
backstop for a plan built directly, which no GraphQL document can produce.
Both are tested, because a rule enforced in only one of the two is a rule the
other half of the engine does not have.

Nothing here touches the database: every refusal happens while the plan is
still being built.
"""

import pytest

from public_api.aggregations import (
    AggregatableEntity,
    AggregateOp,
    AggregateQueryPlan,
    DimensionSpec,
    InvalidPlanError,
    MetricSpec,
    OrderDirection,
    OrderSpec,
    TemporalGranularity,
    WindowBound,
    WindowFrameSpec,
    WindowFrameType,
    WindowFunctionKind,
    WindowSpec,
    window_alias,
)
from public_api.aggregations.dimensions import (
    CalendarEventGroupByInput,
    CalendarEventScalarGroupByField,
    CalendarEventTemporalGroupByField,
    CalendarEventTemporalGroupByInput,
    resolve_dimensions,
)
from public_api.aggregations.errors import (
    WindowFrameError,
    WindowOrderByRequiredError,
    WindowOrderKeyNotGroupedError,
    WindowOrderVariantError,
    WindowPartitionNotGroupedError,
)
from public_api.aggregations.ordering import (
    AggregateOrderDirection,
    CalendarEventOrderableMetric,
)
from public_api.aggregations.windows import (
    CalendarEventWindowInput,
    CalendarEventWindowOrderInput,
    WindowFrameInput,
    resolve_frame,
    resolve_window,
    selected_window_kinds,
)


ENTITY = AggregatableEntity.CALENDAR_EVENT

BY_CALENDAR = CalendarEventGroupByInput(field=CalendarEventScalarGroupByField.CALENDAR_ID)
BY_DAY = CalendarEventGroupByInput(
    temporal=CalendarEventTemporalGroupByInput(
        field=CalendarEventTemporalGroupByField.START_TIME,
        granularity=TemporalGranularity.DAY,
    )
)
BY_TIMEZONE = CalendarEventGroupByInput(field=CalendarEventScalarGroupByField.TIMEZONE)

ORDER_BY_DAY = CalendarEventWindowOrderInput(key=BY_DAY, direction=AggregateOrderDirection.ASC)


def _window(**overrides) -> CalendarEventWindowInput:
    defaults: dict = {
        "metric": CalendarEventOrderableMetric.COUNT,
        "partition_by": None,
        "order_by": [ORDER_BY_DAY],
        "frame": None,
    }
    return CalendarEventWindowInput(**{**defaults, **overrides})


#: The resolved ``groupBy`` most of these tests run against: one day bucket.
DAY_DIMENSIONS = resolve_dimensions([BY_DAY], "UTC")
CALENDAR_AND_DAY_DIMENSIONS = resolve_dimensions([BY_CALENDAR, BY_DAY], "UTC")


def _resolve(window: CalendarEventWindowInput | None, dimensions=DAY_DIMENSIONS):
    return resolve_window(
        ENTITY,
        window,
        "UTC",
        dimensions,
        (WindowFunctionKind.RUNNING_TOTAL,),
    )


# ---------------------------------------------------------------------------
# What a GraphQL caller is refused
# ---------------------------------------------------------------------------


class TestWindowInputRefusals:
    def test_a_window_without_an_order_by_is_refused(self):
        """An unordered running total has no definition -- Postgres would pick
        an order and return numbers that move between runs."""
        with pytest.raises(WindowOrderByRequiredError):
            _resolve(_window(order_by=None))

    def test_an_empty_order_by_list_is_refused_the_same_way(self):
        with pytest.raises(WindowOrderByRequiredError):
            _resolve(_window(order_by=[]))

    def test_partition_by_a_dimension_the_query_did_not_group_by_is_refused(self):
        with pytest.raises(WindowPartitionNotGroupedError):
            _resolve(_window(partition_by=[BY_CALENDAR]))

    def test_partition_by_a_grouped_dimension_is_accepted(self):
        spec, _ = _resolve(
            _window(partition_by=[BY_CALENDAR]),
            dimensions=CALENDAR_AND_DAY_DIMENSIONS,
        )
        assert spec is not None
        assert spec.partition_by == ("calendar_id",)

    def test_a_temporal_partition_resolves_to_the_bucket_alias(self):
        """A temporal dimension is grouped under ``<field>_bucket``, not under
        its column name, so partitioning on it has to land on the same alias
        ``groupBy`` produced or there is no column to read."""
        spec, _ = _resolve(_window(partition_by=[BY_DAY]))
        assert spec is not None
        assert spec.partition_by == ("start_time_bucket",)

    def test_partitioning_by_the_same_field_at_another_granularity_is_refused(self):
        """The one that returns confident wrong numbers rather than an error.

        ``temporal_alias`` does not encode the granularity, so a MONTH bucket
        and a DAY bucket of the same field share the alias
        ``start_time_bucket``. Matched on the alias alone, a window asking to
        partition by month over a day-grouped result would be accepted and
        would partition by *day*: one row per partition, so every
        ``runningTotal`` equals that row's own count and every
        ``percentOfTotal`` is 100.
        """
        by_month = CalendarEventGroupByInput(
            temporal=CalendarEventTemporalGroupByInput(
                field=CalendarEventTemporalGroupByField.START_TIME,
                granularity=TemporalGranularity.MONTH,
            )
        )
        # The two do share an alias -- this is the trap, spelled out.
        assert resolve_dimensions([by_month], "UTC")[0].alias == DAY_DIMENSIONS[0].alias

        with pytest.raises(WindowPartitionNotGroupedError):
            _resolve(_window(partition_by=[by_month]))

    def test_ordering_a_window_by_another_granularity_of_a_grouped_field_is_refused(self):
        by_week = CalendarEventGroupByInput(
            temporal=CalendarEventTemporalGroupByInput(
                field=CalendarEventTemporalGroupByField.START_TIME,
                granularity=TemporalGranularity.WEEK,
            )
        )
        entry = CalendarEventWindowOrderInput(key=by_week)
        with pytest.raises(WindowOrderKeyNotGroupedError):
            _resolve(_window(order_by=[entry]))

    def test_the_same_dimension_named_twice_is_one_partition(self):
        spec, _ = _resolve(_window(partition_by=[BY_DAY, BY_DAY]))
        assert spec is not None
        assert spec.partition_by == ("start_time_bucket",)

    def test_an_order_entry_setting_both_variants_is_refused(self):
        entry = CalendarEventWindowOrderInput(key=BY_DAY, metric=CalendarEventOrderableMetric.COUNT)
        with pytest.raises(WindowOrderVariantError):
            _resolve(_window(order_by=[entry]))

    def test_an_order_entry_setting_neither_variant_is_refused(self):
        with pytest.raises(WindowOrderVariantError):
            _resolve(_window(order_by=[CalendarEventWindowOrderInput()]))

    def test_ordering_a_window_by_an_ungrouped_dimension_is_refused(self):
        entry = CalendarEventWindowOrderInput(key=BY_TIMEZONE)
        with pytest.raises(WindowOrderKeyNotGroupedError):
            _resolve(_window(order_by=[entry]))

    def test_an_absent_window_argument_resolves_to_nothing(self):
        assert _resolve(None) == (None, ())


# ---------------------------------------------------------------------------
# What a window contributes to the plan
# ---------------------------------------------------------------------------


class TestResolvedWindow:
    def test_the_metric_a_window_reads_is_added_to_the_plans_metrics(self):
        """Same contract as ``having`` and ``orderBy``: a window may read a
        metric the row selection never asked for, and it is annotated rather
        than missing."""
        spec, metrics = _resolve(_window(metric=CalendarEventOrderableMetric.DURATION_MINUTES_SUM))
        assert spec is not None
        assert spec.metric_alias == "duration_minutes__sum"
        assert metrics == (
            MetricSpec(
                alias="duration_minutes__sum",
                field_path="duration_minutes",
                op=AggregateOp.SUM,
            ),
        )

    def test_a_metric_the_window_orders_by_is_added_too(self):
        entry = CalendarEventWindowOrderInput(
            metric=CalendarEventOrderableMetric.DURATION_MINUTES_AVG,
            direction=AggregateOrderDirection.DESC,
        )
        spec, metrics = _resolve(_window(order_by=[entry]))
        assert spec is not None
        assert spec.order_by == (
            OrderSpec(alias="duration_minutes__avg", direction=OrderDirection.DESC),
        )
        assert {metric.alias for metric in metrics} == {"count", "duration_minutes__avg"}

    def test_a_metric_named_twice_is_requested_once(self):
        entry = CalendarEventWindowOrderInput(metric=CalendarEventOrderableMetric.COUNT)
        _, metrics = _resolve(_window(order_by=[entry]))
        assert [metric.alias for metric in metrics] == ["count"]

    def test_functions_follow_the_enums_order_not_the_documents(self):
        """Two documents asking for the same functions build the same plan, so
        they audit the same way regardless of selection order."""
        spec, _ = resolve_window(
            ENTITY,
            _window(),
            "UTC",
            DAY_DIMENSIONS,
            (WindowFunctionKind.PERCENT_OF_TOTAL, WindowFunctionKind.RUNNING_TOTAL),
        )
        assert spec is not None
        assert spec.functions == (
            WindowFunctionKind.RUNNING_TOTAL,
            WindowFunctionKind.PERCENT_OF_TOTAL,
        )

    def test_a_window_nobody_read_a_function_from_still_validates(self):
        """A bad ``partitionBy`` is a bad query whether or not its result would
        have been read, so validation does not depend on the selection."""
        spec, _ = resolve_window(ENTITY, _window(), "UTC", DAY_DIMENSIONS, ())
        assert spec is not None
        assert spec.functions == ()

        with pytest.raises(WindowPartitionNotGroupedError):
            resolve_window(ENTITY, _window(partition_by=[BY_CALENDAR]), "UTC", DAY_DIMENSIONS, ())


class TestSelectedWindowKinds:
    def test_a_sub_selection_maps_to_its_functions(self):
        assert selected_window_kinds(["movingAverage", "runningTotal"]) == (
            WindowFunctionKind.RUNNING_TOTAL,
            WindowFunctionKind.MOVING_AVERAGE,
        )

    def test_a_name_that_is_not_a_window_function_is_ignored(self):
        assert selected_window_kinds(["__typename"]) == ()

    def test_each_function_has_its_own_row_dict_alias(self):
        aliases = {window_alias(kind) for kind in WindowFunctionKind}
        assert len(aliases) == len(WindowFunctionKind)
        assert all(alias.startswith("_window_") for alias in aliases)


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


class TestFrames:
    def test_the_default_frame_is_a_running_total(self):
        assert WindowFrameSpec().bounds() == (None, 0)

    def test_a_preceding_offset_becomes_a_negative_bound(self):
        """Django's own encoding: negative counts backwards, positive forwards,
        zero is the current row and ``None`` is unbounded."""
        frame = WindowFrameSpec(
            start=WindowBound.PRECEDING,
            start_offset=2,
            end=WindowBound.CURRENT_ROW,
        )
        assert frame.bounds() == (-2, 0)

    def test_a_following_offset_becomes_a_positive_bound(self):
        frame = WindowFrameSpec(
            start=WindowBound.CURRENT_ROW,
            end=WindowBound.FOLLOWING,
            end_offset=3,
        )
        assert frame.bounds() == (0, 3)

    def test_unbounded_on_both_sides_is_the_whole_partition(self):
        frame = WindowFrameSpec(
            start=WindowBound.UNBOUNDED_PRECEDING,
            end=WindowBound.UNBOUNDED_FOLLOWING,
        )
        assert frame.bounds() == (None, None)

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({"start": WindowBound.PRECEDING}, id="preceding-without-offset"),
            pytest.param(
                {"start": WindowBound.PRECEDING, "start_offset": 0}, id="preceding-zero-offset"
            ),
            pytest.param(
                {"end": WindowBound.FOLLOWING, "end_offset": None}, id="following-without-offset"
            ),
            pytest.param({"start_offset": 2}, id="offset-on-a-fixed-bound"),
            pytest.param({"start": WindowBound.UNBOUNDED_FOLLOWING}, id="starting-after-every-row"),
            pytest.param({"end": WindowBound.UNBOUNDED_PRECEDING}, id="ending-before-every-row"),
            pytest.param(
                {
                    "start": WindowBound.FOLLOWING,
                    "start_offset": 2,
                    "end": WindowBound.PRECEDING,
                    "end_offset": 2,
                },
                id="start-after-end",
            ),
        ],
    )
    def test_a_frame_that_describes_no_rows_is_refused(self, kwargs):
        with pytest.raises(InvalidPlanError):
            WindowFrameSpec(**kwargs)

    def test_the_same_refusal_reaches_a_caller_as_a_request_error(self):
        """A frame built in code is an engine error; the same frame arriving in
        a document is the caller's mistake, and travels back as one."""
        with pytest.raises(WindowFrameError):
            resolve_frame(WindowFrameInput(start=WindowBound.PRECEDING))

    def test_a_range_frame_keeps_its_unit(self):
        spec = resolve_frame(WindowFrameInput(frame_type=WindowFrameType.RANGE))
        assert spec is not None
        assert spec.frame_type is WindowFrameType.RANGE
        assert spec.bounds() == (None, 0)

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param(
                {"start": WindowBound.PRECEDING, "start_offset": 1}, id="range-preceding-offset"
            ),
            pytest.param(
                {"end": WindowBound.FOLLOWING, "end_offset": 1}, id="range-following-offset"
            ),
        ],
    )
    def test_a_range_frame_with_an_offset_is_refused(self, kwargs):
        """Postgres accepts ``RANGE <n> PRECEDING`` only over exactly one
        ordering column an integer can offset -- not the bucketed timestamp a
        window here routinely orders by ("RANGE with offset PRECEDING/FOLLOWING
        is not supported for column type timestamp with time zone"), and not
        two ordering terms at all. Refused here rather than by the database, in
        this engine's words."""
        with pytest.raises(InvalidPlanError):
            WindowFrameSpec(frame_type=WindowFrameType.RANGE, **kwargs)

        with pytest.raises(WindowFrameError):
            resolve_frame(WindowFrameInput(frame_type=WindowFrameType.RANGE, **kwargs))

    def test_the_same_offsets_are_fine_in_a_rows_frame(self):
        spec = resolve_frame(
            WindowFrameInput(
                frame_type=WindowFrameType.ROWS,
                start=WindowBound.PRECEDING,
                start_offset=1,
            )
        )
        assert spec is not None
        assert spec.bounds() == (-1, 0)

    def test_no_frame_stays_no_frame(self):
        assert resolve_frame(None) is None


# ---------------------------------------------------------------------------
# The plan's own backstop
# ---------------------------------------------------------------------------


COUNT_METRIC = MetricSpec(alias="count", field_path="id", op=AggregateOp.COUNT)
BY_CALENDAR_DIMENSION = DimensionSpec(alias="calendar_id", field_path="calendar_fk_id")


def _plan(**overrides) -> AggregateQueryPlan:
    defaults = {
        "entity": AggregatableEntity.CALENDAR_EVENT,
        "dimensions": (BY_CALENDAR_DIMENSION,),
        "metrics": (COUNT_METRIC,),
    }
    return AggregateQueryPlan(**{**defaults, **overrides})


class TestPlanLevelWindowValidation:
    def test_a_window_spec_needs_an_order_by(self):
        with pytest.raises(InvalidPlanError):
            WindowSpec(metric_alias="count", order_by=())

    def test_a_window_spec_needs_a_metric_alias(self):
        with pytest.raises(InvalidPlanError):
            WindowSpec(metric_alias="", order_by=(OrderSpec(alias="count"),))

    def test_windowing_over_an_unknown_metric_is_refused(self):
        with pytest.raises(InvalidPlanError):
            _plan(
                window=WindowSpec(
                    metric_alias="duration_minutes__sum", order_by=(OrderSpec(alias="count"),)
                )
            )

    def test_partitioning_by_something_that_is_not_a_dimension_is_refused(self):
        """Not held to "any alias": a partition on a metric would mean one
        partition per distinct value of the number the window is reading
        across, and there is no group-by column for it either."""
        with pytest.raises(InvalidPlanError):
            _plan(
                window=WindowSpec(
                    metric_alias="count",
                    order_by=(OrderSpec(alias="count"),),
                    partition_by=("count",),
                )
            )

    def test_ordering_a_window_by_an_unknown_alias_is_refused(self):
        with pytest.raises(InvalidPlanError):
            _plan(window=WindowSpec(metric_alias="count", order_by=(OrderSpec(alias="nope"),)))

    def test_a_window_over_a_dimension_alias_is_still_ordered_by_one(self):
        """Ordering *is* allowed to name a dimension or a metric -- the
        asymmetry with ``partition_by`` is deliberate."""
        plan = _plan(
            window=WindowSpec(
                metric_alias="count",
                order_by=(OrderSpec(alias="calendar_id", direction=OrderDirection.ASC),),
                partition_by=("calendar_id",),
                functions=(WindowFunctionKind.RANK,),
            )
        )
        assert plan.window is not None
        assert plan.window.referenced_aliases() == ("count", "calendar_id", "calendar_id")
