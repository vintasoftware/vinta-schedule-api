"""Unit coverage for the two new input shapes this phase adds: HAVING's typed
comparisons and 'and' / 'or' nesting, and ORDER BY's "exactly one of key or
metric" variant.

Nothing here touches a database -- every assertion is about how a GraphQL
input resolves into the plan's own types (``HavingSpec`` / ``OrderSpec`` /
``MetricSpec``), not about what SQL that produces. See
``test_having_execution.py`` and ``test_metric_ordering.py`` for that.
"""

import dataclasses

import pytest

from public_api.aggregations.errors import (
    EmptyHavingComparisonError,
    EmptyHavingInputError,
    OrderKeyNotGroupedError,
    OrderVariantError,
)
from public_api.aggregations.having import (
    HAVING_INPUT_BY_ENTITY,
    CalendarEventHavingInput,
    IntComparison,
    NumericAggregateComparison,
    resolve_having,
)
from public_api.aggregations.ordering import (
    AggregateOrderDirection,
    CalendarEventAggregateOrderInput,
    CalendarEventOrderableMetric,
    resolve_order_by,
)
from public_api.aggregations.plan import AggregatableEntity, AggregateOp, ComparisonOp
from public_api.aggregations.registry import FieldKind, get_registration


class TestResolveHaving:
    def test_none_resolves_to_no_filter_and_no_metrics(self):
        spec, metrics = resolve_having(None)
        assert spec is None
        assert metrics == ()

    def test_a_single_field_resolves_to_one_leaf_and_its_metric(self):
        having = CalendarEventHavingInput(count=IntComparison(gt=2))
        spec, metrics = resolve_having(having)

        assert spec is not None
        assert spec.comparison is not None
        assert spec.comparison.alias == "count"
        assert spec.comparison.comparison is ComparisonOp.GT
        assert spec.comparison.value == 2
        assert len(metrics) == 1
        assert metrics[0].alias == "count"
        assert metrics[0].field_path == "id"
        assert metrics[0].op is AggregateOp.COUNT

    def test_a_relation_count_field_uses_its_own_field_path_as_alias(self):
        having = CalendarEventHavingInput(attendance_count=IntComparison(gte=1))
        spec, metrics = resolve_having(having)

        assert spec is not None
        assert spec.comparison is not None
        assert spec.comparison.alias == "attendance_count"
        assert len(metrics) == 1
        assert metrics[0].field_path == "attendance_count"
        assert metrics[0].op is AggregateOp.COUNT

    def test_a_numeric_field_resolves_one_leaf_per_operation_set(self):
        from public_api.aggregations.having import FloatComparison

        having = CalendarEventHavingInput(
            duration_minutes=NumericAggregateComparison(
                sum=FloatComparison(gt=100.0), avg=FloatComparison(lt=30.0)
            )
        )
        spec, metrics = resolve_having(having)

        assert spec is not None
        assert spec.comparison is None
        assert len(spec.all_of) == 2
        assert set(spec.referenced_aliases()) == {
            "duration_minutes__sum",
            "duration_minutes__avg",
        }
        assert {metric.alias for metric in metrics} == {
            "duration_minutes__sum",
            "duration_minutes__avg",
        }
        assert all(metric.field_path == "duration_minutes" for metric in metrics)
        assert {metric.op for metric in metrics} == {AggregateOp.SUM, AggregateOp.AVG}

    def test_multiple_fields_on_one_node_combine_with_and(self):
        having = CalendarEventHavingInput(
            count=IntComparison(gt=2),
            attendance_count=IntComparison(gte=1),
        )
        spec, metrics = resolve_having(having)

        assert spec is not None
        assert spec.comparison is None
        assert len(spec.all_of) == 2
        assert set(spec.referenced_aliases()) == {"count", "attendance_count"}
        assert {metric.alias for metric in metrics} == {"count", "attendance_count"}

    def test_multiple_operators_on_one_comparison_combine_with_and(self):
        having = CalendarEventHavingInput(count=IntComparison(gt=2, lt=10))
        spec, _ = resolve_having(having)

        assert spec is not None
        assert spec.comparison is None
        assert len(spec.all_of) == 2
        ops = {leaf.comparison.comparison for leaf in spec.all_of}
        assert ops == {ComparisonOp.GT, ComparisonOp.LT}

    def test_and_nests_child_inputs(self):
        having = CalendarEventHavingInput(
            and_=[
                CalendarEventHavingInput(count=IntComparison(gt=2)),
                CalendarEventHavingInput(attendance_count=IntComparison(gte=1)),
            ]
        )
        spec, metrics = resolve_having(having)

        assert spec is not None
        assert set(spec.referenced_aliases()) == {"count", "attendance_count"}
        assert {metric.alias for metric in metrics} == {"count", "attendance_count"}

    def test_or_nests_child_inputs_as_any_of(self):
        having = CalendarEventHavingInput(
            or_=[
                CalendarEventHavingInput(count=IntComparison(gt=100)),
                CalendarEventHavingInput(attendance_count=IntComparison(eq=0)),
            ]
        )
        spec, metrics = resolve_having(having)

        assert spec is not None
        assert spec.comparison is None
        assert spec.all_of == ()
        assert len(spec.any_of) == 2
        assert set(spec.referenced_aliases()) == {"count", "attendance_count"}
        assert {metric.alias for metric in metrics} == {"count", "attendance_count"}

    def test_and_and_or_together_combine_with_and_at_the_top(self):
        having = CalendarEventHavingInput(
            count=IntComparison(gt=1),
            or_=[
                CalendarEventHavingInput(attendance_count=IntComparison(eq=0)),
                CalendarEventHavingInput(attendance_count=IntComparison(gt=5)),
            ],
        )
        spec, _ = resolve_having(having)

        assert spec is not None
        assert spec.comparison is None
        assert len(spec.all_of) == 2
        # One leg is the direct `count` comparison, the other is the `or` group.
        kinds = [leaf.comparison is not None for leaf in spec.all_of]
        assert kinds.count(True) == 1
        assert kinds.count(False) == 1

    def test_an_empty_comparison_is_rejected(self):
        with pytest.raises(EmptyHavingComparisonError):
            resolve_having(CalendarEventHavingInput(count=IntComparison()))

    def test_an_empty_numeric_comparison_is_rejected(self):
        with pytest.raises(EmptyHavingComparisonError):
            resolve_having(CalendarEventHavingInput(duration_minutes=NumericAggregateComparison()))

    def test_an_empty_input_node_is_rejected(self):
        with pytest.raises(EmptyHavingInputError):
            resolve_having(CalendarEventHavingInput())

    def test_an_empty_nested_and_child_is_rejected(self):
        with pytest.raises(EmptyHavingInputError):
            resolve_having(CalendarEventHavingInput(and_=[CalendarEventHavingInput()]))


class TestHavingInputFieldsMatchRegistry:
    """``resolve_having``'s whole resolution scheme rests on a convention
    nothing enforces at import time or at schema build: a ``*HavingInput``
    attribute name IS the registry field path it filters (see having.py's
    module docstring). A field that drifts from the registry -- renamed or
    removed as a relation count or numeric metric -- would not fail loudly;
    it would reach ``executor.py``'s ``_relation_count_metrics`` empty-handed
    and raise ``InvalidPlanError``, an engine-internal exception a caller
    sees only as a masked "Unexpected error". This pins the convention for
    all six entities instead.
    """

    @pytest.mark.parametrize("entity", list(AggregatableEntity))
    def test_every_having_field_is_a_registered_relation_count_or_numeric_metric(self, entity):
        registration = get_registration(entity)
        numeric_metric_names = {
            name
            for name, aggregatable in registration.metrics.items()
            if aggregatable.kind is FieldKind.NUMERIC
        }
        allowed = {"count", *registration.relation_counts, *numeric_metric_names}

        having_input_cls = HAVING_INPUT_BY_ENTITY[entity]
        field_names = {
            f.name for f in dataclasses.fields(having_input_cls) if f.name not in ("and_", "or_")
        }

        assert field_names <= allowed


class TestResolveOrderBy:
    def test_setting_both_key_and_metric_is_rejected(self):
        from public_api.aggregations.dimensions import CalendarEventGroupByInput
        from public_api.aggregations.dimensions import (
            CalendarEventScalarGroupByField as ScalarField,
        )

        # Both slots are independent nullable fields on the schema -- nothing
        # about the type itself stops a caller from setting both, which is
        # exactly why the resolver has to check.
        entry = CalendarEventAggregateOrderInput(
            key=CalendarEventGroupByInput(field=ScalarField.CALENDAR_ID),
            metric=CalendarEventOrderableMetric.COUNT,
        )

        with pytest.raises(OrderVariantError):
            resolve_order_by(AggregatableEntity.CALENDAR_EVENT, (entry,), "UTC", ("calendar_id",))

    def test_setting_neither_key_nor_metric_is_rejected(self):
        entry = CalendarEventAggregateOrderInput(key=None, metric=None)
        with pytest.raises(OrderVariantError):
            resolve_order_by(AggregatableEntity.CALENDAR_EVENT, (entry,), "UTC", ("calendar_id",))

    def test_ordering_by_a_metric_resolves_an_order_spec_and_its_metric(self):
        entry = CalendarEventAggregateOrderInput(
            metric=CalendarEventOrderableMetric.COUNT,
            direction=AggregateOrderDirection.DESC,
        )
        order_specs, metrics = resolve_order_by(
            AggregatableEntity.CALENDAR_EVENT, (entry,), "UTC", ("calendar_id",)
        )

        assert len(order_specs) == 1
        assert order_specs[0].alias == "count"
        assert len(metrics) == 1
        assert metrics[0].alias == "count"
        assert metrics[0].op is AggregateOp.COUNT

    def test_ordering_by_a_grouped_key_resolves_its_alias(self):
        from public_api.aggregations.dimensions import CalendarEventGroupByInput
        from public_api.aggregations.dimensions import (
            CalendarEventScalarGroupByField as ScalarField,
        )

        entry = CalendarEventAggregateOrderInput(
            key=CalendarEventGroupByInput(field=ScalarField.CALENDAR_ID),
        )
        order_specs, metrics = resolve_order_by(
            AggregatableEntity.CALENDAR_EVENT, (entry,), "UTC", ("calendar_id",)
        )

        assert order_specs == (order_specs[0],)
        assert order_specs[0].alias == "calendar_id"
        assert metrics == ()

    def test_ordering_by_a_key_not_in_group_by_is_rejected(self):
        from public_api.aggregations.dimensions import CalendarEventGroupByInput
        from public_api.aggregations.dimensions import (
            CalendarEventScalarGroupByField as ScalarField,
        )

        entry = CalendarEventAggregateOrderInput(
            key=CalendarEventGroupByInput(field=ScalarField.CALENDAR_ID),
        )
        with pytest.raises(OrderKeyNotGroupedError):
            resolve_order_by(
                AggregatableEntity.CALENDAR_EVENT, (entry,), "UTC", ("is_recurring_exception",)
            )
