"""The having and ordering inputs, checked without touching the database.

What these assert is the translation: a GraphQL input object in, a `Q` and a
list of `OrderSpec`s out, plus the metrics those clauses need annotated. The
metrics half is the one worth being careful about -- it is invisible in the
response, and getting it wrong turns a `HAVING` into a `WHERE`, which filters
rows before grouping and returns numbers that look like an answer.
"""

from typing import cast

from django.db.models import Q

import pytest

from public_api.aggregations.errors import (
    AMBIGUOUS_ORDER_INPUT_MESSAGE,
    AmbiguousOrderInputError,
    UngroupedOrderKeyError,
)
from public_api.aggregations.having import (
    HAVING_INPUT_TYPE_BY_ENTITY,
    FloatComparison,
    IntComparison,
    NumericAggregateComparison,
    having_from_input,
    having_input_type,
)
from public_api.aggregations.ordering import (
    METRIC_REF_ENUM_BY_ENTITY,
    ORDER_INPUT_TYPE_BY_ENTITY,
    ORDER_KEY_ENUM_BY_ENTITY,
    OrderDirection,
    order_from_input,
    order_input_type,
)
from public_api.aggregations.plan import AggregatableEntity, AggregateOp, DimensionSpec
from public_api.aggregations.registry import get_registration, metric_alias
from public_api.aggregations.types import TemporalGranularity


EVENT = AggregatableEntity.CALENDAR_EVENT


def _event_registration():
    return get_registration(EVENT)


def _having(**kwargs):
    return HAVING_INPUT_TYPE_BY_ENTITY[EVENT](**kwargs)


def _order(**kwargs):
    return ORDER_INPUT_TYPE_BY_ENTITY[EVENT](**kwargs)


def _lookups(predicate: Q) -> set[tuple[str, object]]:
    """Every leaf `(lookup, value)` in a `Q`, flattened across nesting."""
    found: set[tuple[str, object]] = set()
    for child in predicate.children:
        if isinstance(child, Q):
            found |= _lookups(child)
        else:
            lookup, value = cast("tuple[str, object]", child)
            found.add((lookup, value))
    return found


class TestHavingTranslation:
    def test_an_absent_having_produces_no_predicate(self):
        spec, metrics = having_from_input(_event_registration(), None)
        assert spec is None
        assert metrics == ()

    def test_a_having_that_names_nothing_produces_no_predicate(self):
        """`having: {}` is treated as absent, not as an empty predicate."""
        spec, metrics = having_from_input(_event_registration(), _having())
        assert spec is None
        assert metrics == ()

    def test_count_comparison_becomes_a_lookup_on_the_row_count_alias(self):
        spec, metrics = having_from_input(_event_registration(), _having(count=IntComparison(gt=2)))
        assert spec is not None
        assert _lookups(spec.predicate) == {("count__gt", 2)}
        assert [metric.alias for metric in metrics] == ["count"]
        assert metrics[0].op is AggregateOp.COUNT

    def test_several_operators_on_one_comparison_are_all_applied(self):
        """`{gte: 10, lt: 20}` reads as a range and means both."""
        spec, _metrics = having_from_input(
            _event_registration(), _having(count=IntComparison(gte=10, lt=20))
        )
        assert spec is not None
        assert _lookups(spec.predicate) == {("count__gte", 10), ("count__lt", 20)}

    def test_an_empty_comparison_contributes_nothing(self):
        spec, metrics = having_from_input(_event_registration(), _having(count=IntComparison()))
        assert spec is None
        assert metrics == ()

    def test_a_metric_comparison_names_the_metric_it_needs_annotated(self):
        """The half that is invisible in the response and wrong when missing."""
        spec, metrics = having_from_input(
            _event_registration(),
            _having(duration_minutes=NumericAggregateComparison(sum=FloatComparison(gte=60.0))),
        )
        assert spec is not None
        alias = metric_alias("duration_minutes", AggregateOp.SUM)
        assert _lookups(spec.predicate) == {(f"{alias}__gte", 60.0)}
        assert [metric.alias for metric in metrics] == [alias]
        assert metrics[0].field_path == "duration_minutes"
        assert metrics[0].op is AggregateOp.SUM

    def test_a_relation_count_comparison_is_a_count_metric(self):
        spec, metrics = having_from_input(
            _event_registration(), _having(attendance_count=IntComparison(gt=0))
        )
        assert spec is not None
        alias = metric_alias("attendance_count", AggregateOp.COUNT)
        assert _lookups(spec.predicate) == {(f"{alias}__gt", 0)}
        assert [metric.alias for metric in metrics] == [alias]

    def test_one_metric_wanted_twice_is_annotated_once(self):
        """A metric named by two conditions must not collide on its alias."""
        alias = metric_alias("duration_minutes", AggregateOp.SUM)
        spec, metrics = having_from_input(
            _event_registration(),
            _having(
                duration_minutes=NumericAggregateComparison(sum=FloatComparison(gte=60.0)),
                and_=[
                    _having(
                        duration_minutes=NumericAggregateComparison(sum=FloatComparison(lt=600.0))
                    )
                ],
            ),
        )
        assert spec is not None
        assert _lookups(spec.predicate) == {(f"{alias}__gte", 60.0), (f"{alias}__lt", 600.0)}
        assert [metric.alias for metric in metrics] == [alias]


class TestHavingComposition:
    def test_and_nests_as_a_conjunction(self):
        spec, _metrics = having_from_input(
            _event_registration(),
            _having(
                and_=[
                    _having(count=IntComparison(gt=2)),
                    _having(count=IntComparison(lt=10)),
                ]
            ),
        )
        assert spec is not None
        assert _lookups(spec.predicate) == {("count__gt", 2), ("count__lt", 10)}
        # The `and` group is the only term, so it *is* the predicate rather
        # than being wrapped in a single-child conjunction.
        assert spec.predicate.connector == Q.AND
        assert len(spec.predicate.children) == 2

    def test_or_nests_as_a_disjunction(self):
        spec, _metrics = having_from_input(
            _event_registration(),
            _having(
                or_=[
                    _having(count=IntComparison(lt=2)),
                    _having(count=IntComparison(gt=10)),
                ]
            ),
        )
        assert spec is not None
        assert _lookups(spec.predicate) == {("count__lt", 2), ("count__gt", 10)}
        # An `or` group really is disjunctive -- the difference between "fewer
        # than two or more than ten" and a condition nothing can satisfy.
        assert spec.predicate.connector == Q.OR
        assert len(spec.predicate.children) == 2

    def test_and_and_or_compose_in_one_input(self):
        """Top-level terms AND the `and` group AND the `or` group."""
        spec, metrics = having_from_input(
            _event_registration(),
            _having(
                count=IntComparison(gte=1),
                and_=[_having(attendance_count=IntComparison(gt=0))],
                or_=[
                    _having(
                        duration_minutes=NumericAggregateComparison(sum=FloatComparison(gt=1.0))
                    ),
                    _having(count=IntComparison(gt=100)),
                ],
            ),
        )
        assert spec is not None
        assert _lookups(spec.predicate) == {
            ("count__gte", 1),
            (f"{metric_alias('attendance_count', AggregateOp.COUNT)}__gt", 0),
            (f"{metric_alias('duration_minutes', AggregateOp.SUM)}__gt", 1.0),
            ("count__gt", 100),
        }
        assert {metric.alias for metric in metrics} == {
            "count",
            metric_alias("attendance_count", AggregateOp.COUNT),
            metric_alias("duration_minutes", AggregateOp.SUM),
        }

    def test_nesting_deeper_than_one_level_still_collects_its_metrics(self):
        spec, metrics = having_from_input(
            _event_registration(),
            _having(or_=[_having(and_=[_having(count=IntComparison(gt=3))])]),
        )
        assert spec is not None
        assert _lookups(spec.predicate) == {("count__gt", 3)}
        assert [metric.alias for metric in metrics] == ["count"]


class TestHavingSurface:
    def test_every_entity_has_a_having_input(self):
        for entity in AggregatableEntity:
            assert having_input_type(entity) is HAVING_INPUT_TYPE_BY_ENTITY[entity]

    def test_the_having_input_offers_one_field_per_aggregatable(self):
        """Generated from the registry, so the filter side cannot drift."""
        import dataclasses

        for entity in AggregatableEntity:
            registration = get_registration(entity)
            field_names = {f.name for f in dataclasses.fields(HAVING_INPUT_TYPE_BY_ENTITY[entity])}
            assert {"count", "and_", "or_"} <= field_names
            for spec in registration.aggregatable:
                assert spec.name in field_names
            for relation in registration.relation_counts:
                assert relation.name in field_names


class TestOrderTranslation:
    DIMENSIONS = (DimensionSpec(alias="calendar_id", field_path="calendar_id"),)

    def test_no_order_by_produces_nothing(self):
        specs, metrics = order_from_input(EVENT, None, self.DIMENSIONS)
        assert specs == ()
        assert metrics == ()

        specs, metrics = order_from_input(EVENT, [], self.DIMENSIONS)
        assert specs == ()
        assert metrics == ()

    def test_ordering_by_a_metric_names_the_alias_and_the_metric(self):
        metric_ref = METRIC_REF_ENUM_BY_ENTITY[EVENT]
        specs, metrics = order_from_input(EVENT, [_order(metric=metric_ref.COUNT)], self.DIMENSIONS)
        assert [spec.alias for spec in specs] == ["count"]
        assert specs[0].descending is True
        assert [metric.alias for metric in metrics] == ["count"]

    def test_direction_ascending_is_honoured(self):
        metric_ref = METRIC_REF_ENUM_BY_ENTITY[EVENT]
        specs, _metrics = order_from_input(
            EVENT,
            [_order(metric=metric_ref.COUNT, direction=OrderDirection.ASC)],
            self.DIMENSIONS,
        )
        assert specs[0].descending is False
        assert specs[0].as_order_by() == "count"

    def test_ordering_by_a_key_resolves_the_alias_from_this_querys_dimensions(self):
        key_enum = ORDER_KEY_ENUM_BY_ENTITY[EVENT]
        specs, metrics = order_from_input(
            EVENT, [_order(key=key_enum.CALENDAR_ID)], self.DIMENSIONS
        )
        assert [spec.alias for spec in specs] == ["calendar_id"]
        assert metrics == ()

    def test_ordering_by_a_bucketed_key_uses_the_granularity_it_was_grouped_at(self):
        """A bucketed dimension's row key carries its bucket size."""
        key_enum = ORDER_KEY_ENUM_BY_ENTITY[EVENT]
        import zoneinfo

        dimensions = (
            DimensionSpec(
                alias="start_time_day",
                field_path="start_time",
                granularity=TemporalGranularity.DAY,
                tzinfo=zoneinfo.ZoneInfo("UTC"),
            ),
        )
        specs, _metrics = order_from_input(EVENT, [_order(key=key_enum.START_TIME)], dimensions)
        assert [spec.alias for spec in specs] == ["start_time_day"]

    def test_several_order_entries_keep_their_order(self):
        key_enum = ORDER_KEY_ENUM_BY_ENTITY[EVENT]
        metric_ref = METRIC_REF_ENUM_BY_ENTITY[EVENT]
        specs, _metrics = order_from_input(
            EVENT,
            [
                _order(metric=metric_ref.COUNT),
                _order(key=key_enum.CALENDAR_ID, direction=OrderDirection.ASC),
            ],
            self.DIMENSIONS,
        )
        assert [spec.as_order_by() for spec in specs] == ["-count", "calendar_id"]


class TestOrderValidation:
    DIMENSIONS = (DimensionSpec(alias="calendar_id", field_path="calendar_id"),)

    def test_an_entry_naming_both_key_and_metric_is_rejected(self):
        key_enum = ORDER_KEY_ENUM_BY_ENTITY[EVENT]
        metric_ref = METRIC_REF_ENUM_BY_ENTITY[EVENT]
        with pytest.raises(AmbiguousOrderInputError) as exc_info:
            order_from_input(
                EVENT,
                [_order(key=key_enum.CALENDAR_ID, metric=metric_ref.COUNT)],
                self.DIMENSIONS,
            )
        assert exc_info.value.message == AMBIGUOUS_ORDER_INPUT_MESSAGE

    def test_an_entry_naming_neither_is_rejected(self):
        with pytest.raises(AmbiguousOrderInputError):
            order_from_input(EVENT, [_order()], self.DIMENSIONS)

    def test_ordering_by_a_dimension_the_query_did_not_group_by_is_rejected(self):
        """It would add a column to the GROUP BY and split the numbers further."""
        key_enum = ORDER_KEY_ENUM_BY_ENTITY[EVENT]
        with pytest.raises(UngroupedOrderKeyError) as exc_info:
            order_from_input(EVENT, [_order(key=key_enum.TIMEZONE)], self.DIMENSIONS)
        assert "timezone" in exc_info.value.message


class TestOrderSurface:
    def test_every_entity_has_an_order_input(self):
        for entity in AggregatableEntity:
            assert order_input_type(entity) is ORDER_INPUT_TYPE_BY_ENTITY[entity]

    def test_the_order_key_enum_is_the_registrys_groupable_dimensions(self):
        for entity in AggregatableEntity:
            registration = get_registration(entity)
            declared = {member.value for member in ORDER_KEY_ENUM_BY_ENTITY[entity]}
            assert declared == {spec.name for spec in registration.groupable}

    def test_the_metric_ref_enum_values_are_row_aliases(self):
        """Resolving a reference is a lookup, not a reconstruction."""
        for entity in AggregatableEntity:
            registration = get_registration(entity)
            values = {member.value for member in METRIC_REF_ENUM_BY_ENTITY[entity]}
            assert "count" in values
            for spec in registration.aggregatable:
                for op in spec.ops:
                    alias = metric_alias(spec.name, op)
                    if op is AggregateOp.CONCAT:
                        assert alias not in values
                    else:
                        assert alias in values

    def test_concat_is_not_orderable(self):
        """Its row key depends on the separator it was requested with."""
        members = {member.name for member in METRIC_REF_ENUM_BY_ENTITY[EVENT]}
        assert "TITLE_CONCAT" not in members
        assert "TITLE_MIN" in members
