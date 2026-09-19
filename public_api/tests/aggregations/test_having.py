"""Building HAVING trees and order-by specs, without touching a database.

The two share one notion of "a metric you may name" — ``referenceable_metrics``
— so most of what is worth asserting here is that the hand-written enums and
inputs still agree with it, and that the composition rules hold.
"""

import enum
from zoneinfo import ZoneInfo

import pytest

from public_api.aggregations.dimensions import (
    CalendarEventGroupByInput,
    CalendarEventScalarGroupByField,
    CalendarEventTemporalGroupBy,
    CalendarEventTemporalGroupByField,
    resolve_group_by_inputs,
)
from public_api.aggregations.errors import InvalidAggregatePlanError
from public_api.aggregations.having import (
    HAVING_INPUT_TYPES,
    MAX_HAVING_DEPTH,
    CalendarEventHavingInput,
    CalendarHavingInput,
    CalendarPoolHavingInput,
    FloatComparison,
    IntComparison,
    NumericAggregateComparison,
    flatten_conditions,
    having_is_empty,
    resolve_having,
)
from public_api.aggregations.ordering import (
    METRIC_REF_ENUMS,
    ORDER_INPUT_TYPES,
    ORDERABLE_KEY_ENUMS,
    CalendarEventAggregateOrderInput,
    CalendarEventMetricRef,
    CalendarEventOrderableKey,
    OrderDirection,
    resolve_order_by,
)
from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateOp,
    ComparisonOperator,
)
from public_api.aggregations.registry import (
    COUNT_ALIAS,
    FieldKind,
    get_registration,
    metric_alias,
    referenceable_metrics,
    relation_count_alias,
)
from public_api.aggregations.rows import relation_count_field_name
from public_api.aggregations.types import TemporalGranularity


ALL_ENTITIES = tuple(AggregatableEntity)
EVENT = AggregatableEntity.CALENDAR_EVENT
UTC = ZoneInfo("UTC")


def _grouped_by_calendar():
    return resolve_group_by_inputs(
        EVENT,
        [CalendarEventGroupByInput(scalar=CalendarEventScalarGroupByField.CALENDAR_ID)],
        UTC,
    )


def _grouped_by_day():
    return resolve_group_by_inputs(
        EVENT,
        [
            CalendarEventGroupByInput(
                temporal=CalendarEventTemporalGroupBy(
                    field=CalendarEventTemporalGroupByField.START_TIME,
                    granularity=TemporalGranularity.DAY,
                )
            )
        ],
        UTC,
    )


class TestReferenceableMetrics:
    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_the_set_is_count_plus_numeric_operations_plus_relation_counts(self, entity):
        registration = get_registration(entity)
        expected = {"count"}
        for field_name, registered in registration.aggregatable.items():
            if registered.kind is FieldKind.NUMERIC:
                expected |= {f"{field_name}_{op.value.lower()}" for op in registered.operations}
        expected |= {f"{name}_count" for name in registration.relation_counts}

        assert set(referenceable_metrics(entity)) == expected

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_no_string_or_temporal_aggregate_is_referenceable(self, entity):
        """Comparisons compare numbers, so the referenceable set is numbers."""
        registration = get_registration(entity)
        references = referenceable_metrics(entity)

        for field_name, registered in registration.aggregatable.items():
            if registered.kind is FieldKind.NUMERIC:
                continue
            for op in registered.operations:
                assert f"{field_name}_{op.value.lower()}" not in references

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_every_reference_carries_the_canonical_alias(self, entity):
        for name, reference in referenceable_metrics(entity).items():
            if name == "count":
                assert reference.alias == COUNT_ALIAS
            elif reference.op is AggregateOp.COUNT:
                assert reference.alias == relation_count_alias(reference.field_name)
            else:
                assert reference.alias == metric_alias(reference.field_name, reference.op)


class TestEnumsAndInputsMatchTheRegistry:
    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_the_metric_ref_enum_is_exactly_the_referenceable_set(self, entity):
        assert {member.value for member in METRIC_REF_ENUMS[entity]} == set(
            referenceable_metrics(entity)
        )

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_the_orderable_key_enum_is_exactly_the_groupable_set(self, entity):
        assert {member.value for member in ORDERABLE_KEY_ENUMS[entity]} == set(
            get_registration(entity).groupable
        )

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_enum_member_names_are_the_upper_cased_values(self, entity):
        enums: list[type[enum.Enum]] = [METRIC_REF_ENUMS[entity], ORDERABLE_KEY_ENUMS[entity]]

        for one in enums:
            for member in one:
                assert member.name == str(member.value).upper()

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_the_having_input_offers_exactly_the_referenceable_metrics(self, entity):
        """One comparison field per referenceable metric, grouped by field."""
        registration = get_registration(entity)
        annotations = dict(HAVING_INPUT_TYPES[entity].__annotations__)

        expected = {"count", "and_", "or_"}
        expected |= {
            field_name
            for field_name, registered in registration.aggregatable.items()
            if registered.kind is FieldKind.NUMERIC
        }
        expected |= {relation_count_field_name(name) for name in registration.relation_counts}
        assert set(annotations) == expected

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_counts_compare_as_integers_and_numeric_fields_as_aggregates(self, entity):
        registration = get_registration(entity)
        annotations = dict(HAVING_INPUT_TYPES[entity].__annotations__)

        assert annotations["count"] == IntComparison | None
        for name in registration.relation_counts:
            assert annotations[relation_count_field_name(name)] == IntComparison | None
        for field_name, registered in registration.aggregatable.items():
            if registered.kind is FieldKind.NUMERIC:
                assert annotations[field_name] == NumericAggregateComparison | None

    def test_every_entity_has_a_having_input_and_an_order_input(self):
        assert set(HAVING_INPUT_TYPES) == set(ALL_ENTITIES)
        assert set(ORDER_INPUT_TYPES) == set(ALL_ENTITIES)


class TestResolvingHaving:
    def test_an_absent_clause_resolves_to_nothing(self):
        spec, metrics = resolve_having(EVENT, None)

        assert spec is None
        assert metrics == ()

    def test_a_count_comparison_becomes_one_condition_on_the_count_alias(self):
        spec, metrics = resolve_having(EVENT, CalendarEventHavingInput(count=IntComparison(gt=2)))

        assert spec is not None
        assert len(spec.conditions) == 1
        condition = spec.conditions[0]
        assert condition.alias == COUNT_ALIAS
        assert condition.operator is ComparisonOperator.GT
        assert condition.value == 2.0
        assert [metric.alias for metric in metrics] == [COUNT_ALIAS]

    def test_several_comparisons_on_one_metric_are_all_kept(self):
        spec, _ = resolve_having(
            EVENT, CalendarEventHavingInput(count=IntComparison(gte=2, lte=10))
        )

        assert spec is not None
        assert {(one.operator, one.value) for one in spec.conditions} == {
            (ComparisonOperator.GTE, 2.0),
            (ComparisonOperator.LTE, 10.0),
        }

    def test_a_numeric_aggregate_comparison_names_the_operation_alias(self):
        spec, metrics = resolve_having(
            EVENT,
            CalendarEventHavingInput(
                duration_minutes=NumericAggregateComparison(sum=FloatComparison(gt=100.0))
            ),
        )

        assert spec is not None
        assert spec.conditions[0].alias == metric_alias("duration_minutes", AggregateOp.SUM)
        assert [metric.alias for metric in metrics] == [
            metric_alias("duration_minutes", AggregateOp.SUM)
        ]

    def test_a_relation_count_comparison_names_the_relation_alias(self):
        spec, metrics = resolve_having(
            AggregatableEntity.CALENDAR,
            CalendarHavingInput(events_count=IntComparison(gte=5)),
        )

        assert spec is not None
        assert spec.conditions[0].alias == relation_count_alias("events")
        assert [metric.alias for metric in metrics] == [relation_count_alias("events")]

    def test_a_clause_requires_a_metric_the_document_never_selected(self):
        """That is the point: you can threshold on a number you do not display."""
        _spec, metrics = resolve_having(
            AggregatableEntity.CALENDAR,
            CalendarHavingInput(blocked_times_count=IntComparison(gt=0)),
        )

        assert [metric.alias for metric in metrics] == [relation_count_alias("blocked_times")]
        assert metrics[0].op is AggregateOp.COUNT
        assert metrics[0].field_path == "blocked_times"

    def test_one_metric_named_twice_is_required_once(self):
        _spec, metrics = resolve_having(
            EVENT,
            CalendarEventHavingInput(
                count=IntComparison(gt=1),
                and_=[CalendarEventHavingInput(count=IntComparison(lt=9))],
            ),
        )

        assert [metric.alias for metric in metrics] == [COUNT_ALIAS]


class TestHavingComposition:
    def test_and_nests_as_all_of(self):
        spec, _ = resolve_having(
            EVENT,
            CalendarEventHavingInput(
                count=IntComparison(gt=1),
                and_=[
                    CalendarEventHavingInput(count=IntComparison(lt=100)),
                    CalendarEventHavingInput(count=IntComparison(gte=2)),
                ],
            ),
        )

        assert spec is not None
        assert len(spec.conditions) == 1
        assert len(spec.all_of) == 2
        assert spec.any_of == ()
        assert len(flatten_conditions(spec)) == 3

    def test_or_nests_as_any_of(self):
        spec, _ = resolve_having(
            EVENT,
            CalendarEventHavingInput(
                or_=[
                    CalendarEventHavingInput(count=IntComparison(lt=2)),
                    CalendarEventHavingInput(count=IntComparison(gt=10)),
                ]
            ),
        )

        assert spec is not None
        assert spec.conditions == ()
        assert len(spec.any_of) == 2
        assert [one.conditions[0].operator for one in spec.any_of] == [
            ComparisonOperator.LT,
            ComparisonOperator.GT,
        ]

    def test_and_and_or_nest_together(self):
        spec, _ = resolve_having(
            EVENT,
            CalendarEventHavingInput(
                count=IntComparison(gte=1),
                and_=[CalendarEventHavingInput(count=IntComparison(lte=50))],
                or_=[
                    CalendarEventHavingInput(
                        duration_minutes=NumericAggregateComparison(sum=FloatComparison(gt=100.0))
                    ),
                    CalendarEventHavingInput(count=IntComparison(gt=3)),
                ],
            ),
        )

        assert spec is not None
        assert len(spec.conditions) == 1
        assert len(spec.all_of) == 1
        assert len(spec.any_of) == 2
        assert len(flatten_conditions(spec)) == 4

    def test_an_empty_clause_carries_no_condition(self):
        spec, metrics = resolve_having(EVENT, CalendarEventHavingInput())

        assert spec is not None
        assert having_is_empty(spec)
        assert metrics == ()

    def test_a_clause_whose_nesting_is_all_empty_is_still_empty(self):
        spec, _ = resolve_having(
            EVENT,
            CalendarEventHavingInput(
                and_=[CalendarEventHavingInput(or_=[CalendarEventHavingInput()])]
            ),
        )

        assert spec is not None
        assert having_is_empty(spec)

    def test_a_clause_with_one_condition_anywhere_is_not_empty(self):
        spec, _ = resolve_having(
            EVENT,
            CalendarEventHavingInput(and_=[CalendarEventHavingInput(count=IntComparison(gt=0))]),
        )

        assert spec is not None
        assert not having_is_empty(spec)

    def test_nesting_beyond_the_maximum_depth_is_refused(self):
        deepest = CalendarEventHavingInput(count=IntComparison(gt=0))
        for _ in range(MAX_HAVING_DEPTH):
            deepest = CalendarEventHavingInput(and_=[deepest])

        with pytest.raises(InvalidAggregatePlanError) as excinfo:
            resolve_having(EVENT, deepest)

        assert str(excinfo.value) == "A having clause may not nest more than 5 levels deep"

    def test_nesting_at_the_maximum_depth_is_allowed(self):
        deepest = CalendarEventHavingInput(count=IntComparison(gt=0))
        for _ in range(MAX_HAVING_DEPTH - 1):
            deepest = CalendarEventHavingInput(and_=[deepest])

        spec, _ = resolve_having(EVENT, deepest)

        assert spec is not None
        assert not having_is_empty(spec)


class TestResolvingOrderBy:
    def test_an_absent_order_by_resolves_to_nothing(self):
        specs, metrics = resolve_order_by(EVENT, None, _grouped_by_calendar())

        assert specs == ()
        assert metrics == ()

    def test_an_empty_order_by_resolves_to_nothing(self):
        specs, metrics = resolve_order_by(EVENT, [], _grouped_by_calendar())

        assert specs == ()
        assert metrics == ()

    def test_setting_both_key_and_metric_is_rejected(self):
        with pytest.raises(InvalidAggregatePlanError) as excinfo:
            resolve_order_by(
                EVENT,
                [
                    CalendarEventAggregateOrderInput(
                        key=CalendarEventOrderableKey.CALENDAR_ID,
                        metric=CalendarEventMetricRef.COUNT,
                    )
                ],
                _grouped_by_calendar(),
            )

        assert str(excinfo.value) == "An order-by must set exactly one of key and metric"

    def test_setting_neither_key_nor_metric_is_rejected(self):
        with pytest.raises(InvalidAggregatePlanError) as excinfo:
            resolve_order_by(EVENT, [CalendarEventAggregateOrderInput()], _grouped_by_calendar())

        assert str(excinfo.value) == "An order-by must set exactly one of key and metric"

    def test_a_metric_resolves_to_its_alias_and_requires_it(self):
        specs, metrics = resolve_order_by(
            EVENT,
            [CalendarEventAggregateOrderInput(metric=CalendarEventMetricRef.COUNT)],
            _grouped_by_calendar(),
        )

        assert [spec.alias for spec in specs] == [COUNT_ALIAS]
        assert specs[0].descending is True
        assert [metric.alias for metric in metrics] == [COUNT_ALIAS]

    def test_direction_ascending_is_carried_through(self):
        specs, _ = resolve_order_by(
            EVENT,
            [
                CalendarEventAggregateOrderInput(
                    metric=CalendarEventMetricRef.COUNT, direction=OrderDirection.ASC
                )
            ],
            _grouped_by_calendar(),
        )

        assert specs[0].descending is False

    def test_a_key_resolves_to_the_dimension_alias(self):
        specs, metrics = resolve_order_by(
            EVENT,
            [CalendarEventAggregateOrderInput(key=CalendarEventOrderableKey.CALENDAR_ID)],
            _grouped_by_calendar(),
        )

        assert [spec.alias for spec in specs] == ["calendar_id"]
        assert metrics == ()

    def test_a_bucketed_key_resolves_to_the_bucket_alias(self):
        """``START_TIME`` at ``DAY`` is computed under ``start_time_day``."""
        specs, _ = resolve_order_by(
            EVENT,
            [CalendarEventAggregateOrderInput(key=CalendarEventOrderableKey.START_TIME)],
            _grouped_by_day(),
        )

        assert [spec.alias for spec in specs] == ["start_time_day"]

    def test_a_key_the_query_does_not_group_by_is_rejected(self):
        with pytest.raises(InvalidAggregatePlanError) as excinfo:
            resolve_order_by(
                EVENT,
                [CalendarEventAggregateOrderInput(key=CalendarEventOrderableKey.TIMEZONE)],
                _grouped_by_calendar(),
            )

        assert (
            str(excinfo.value)
            == "Cannot order by a dimension the query does not group by: timezone"
        )

    def test_several_orderings_keep_the_order_they_were_given(self):
        specs, _ = resolve_order_by(
            EVENT,
            [
                CalendarEventAggregateOrderInput(metric=CalendarEventMetricRef.COUNT),
                CalendarEventAggregateOrderInput(
                    key=CalendarEventOrderableKey.CALENDAR_ID,
                    direction=OrderDirection.ASC,
                ),
            ],
            _grouped_by_calendar(),
        )

        assert [(spec.alias, spec.descending) for spec in specs] == [
            (COUNT_ALIAS, True),
            ("calendar_id", False),
        ]

    def test_one_metric_ordered_twice_is_required_once(self):
        _specs, metrics = resolve_order_by(
            EVENT,
            [
                CalendarEventAggregateOrderInput(metric=CalendarEventMetricRef.COUNT),
                CalendarEventAggregateOrderInput(
                    metric=CalendarEventMetricRef.COUNT, direction=OrderDirection.ASC
                ),
            ],
            _grouped_by_calendar(),
        )

        assert [metric.alias for metric in metrics] == [COUNT_ALIAS]


class TestCalendarPoolIsNotSpecial:
    def test_its_having_input_still_offers_count_and_its_relation(self):
        spec, metrics = resolve_having(
            AggregatableEntity.CALENDAR_POOL,
            CalendarPoolHavingInput(memberships_count=IntComparison(gt=0)),
        )

        assert spec is not None
        assert spec.conditions[0].alias == relation_count_alias("memberships")
        assert len(metrics) == 1
