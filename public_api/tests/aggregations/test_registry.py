"""The registry is the one place that decides what a field may be asked for.

These tests pin that down as a property of the tables rather than of any
single entry, so a seventh entity or a new column inherits the guarantees
instead of needing its own test.
"""

import pytest

from public_api.aggregations import (
    AGGREGATE_TYPE_BY_KIND,
    OPS_BY_KIND,
    AggregatableEntity,
    AggregateOp,
    BooleanAggregate,
    DateTimeAggregate,
    FieldKind,
    NumericAggregate,
    StringAggregate,
    UnknownDimensionError,
    UnknownEntityError,
    UnknownMetricFieldError,
    UnsupportedOperationError,
    aggregate_type_for,
    get_registration,
    registered_entities,
    supported_ops,
)
from public_api.constants import PublicAPIResources


ALL_ENTITIES = list(AggregatableEntity)


class TestKindTables:
    def test_every_kind_maps_to_exactly_one_aggregate_type(self):
        assert AGGREGATE_TYPE_BY_KIND == {
            FieldKind.NUMERIC: NumericAggregate,
            FieldKind.STRING: StringAggregate,
            FieldKind.DATETIME: DateTimeAggregate,
            FieldKind.BOOLEAN: BooleanAggregate,
        }

    def test_each_kind_accepts_exactly_the_documented_operations(self):
        assert OPS_BY_KIND == {
            FieldKind.NUMERIC: frozenset(
                {AggregateOp.SUM, AggregateOp.AVG, AggregateOp.MIN, AggregateOp.MAX}
            ),
            FieldKind.STRING: frozenset({AggregateOp.CONCAT, AggregateOp.MIN, AggregateOp.MAX}),
            FieldKind.DATETIME: frozenset({AggregateOp.MIN, AggregateOp.MAX}),
            FieldKind.BOOLEAN: frozenset({AggregateOp.TRUE_COUNT, AggregateOp.FALSE_COUNT}),
        }

    def test_concat_belongs_to_strings_alone(self):
        kinds_accepting_concat = {
            kind for kind, ops in OPS_BY_KIND.items() if AggregateOp.CONCAT in ops
        }
        assert kinds_accepting_concat == {FieldKind.STRING}

    def test_sum_and_avg_belong_to_numbers_alone(self):
        for op in (AggregateOp.SUM, AggregateOp.AVG):
            accepting = {kind for kind, ops in OPS_BY_KIND.items() if op in ops}
            assert accepting == {FieldKind.NUMERIC}


class TestRegistrations:
    def test_all_six_entities_are_registered(self):
        assert set(registered_entities()) == set(ALL_ENTITIES)
        assert len(ALL_ENTITIES) == 6

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_entity_declares_a_real_public_api_resource(self, entity):
        registration = get_registration(entity)
        assert registration.resource in PublicAPIResources.values

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_entity_has_at_least_one_dimension_and_one_metric(self, entity):
        registration = get_registration(entity)
        assert registration.dimensions
        assert registration.metrics

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_every_aggregatable_field_maps_to_exactly_one_aggregate_type(self, entity):
        registration = get_registration(entity)
        for name, aggregatable in registration.metrics.items():
            assert aggregatable.kind in AGGREGATE_TYPE_BY_KIND, name
            assert aggregatable.aggregate_type is aggregate_type_for(aggregatable.kind)
            assert aggregatable.supported_ops == supported_ops(aggregatable.kind)

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_registry_keys_match_the_field_paths_they_hold(self, entity):
        registration = get_registration(entity)
        for name, dimension in registration.dimensions.items():
            assert name == dimension.field_path
        for name, aggregatable in registration.metrics.items():
            assert name == aggregatable.field_path

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_plain_column_metrics_name_a_real_model_field(self, entity):
        """A metric without an expression must be a column the ORM can find."""
        registration = get_registration(entity)
        model_field_names = {
            model_field.name for model_field in registration.model._meta.get_fields()
        }
        for name, aggregatable in registration.metrics.items():
            if aggregatable.expression_factory is None:
                assert name in model_field_names

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_dimensions_name_a_real_model_column(self, entity):
        registration = get_registration(entity)
        column_names = set()
        for model_field in registration.model._meta.get_fields():
            column_names.add(model_field.name)
            attname = getattr(model_field, "attname", None)
            if attname is not None:
                column_names.add(attname)
        for name in registration.dimensions:
            assert name in column_names

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_relation_counts_name_a_real_column_on_the_related_model(self, entity):
        registration = get_registration(entity)
        for name, relation in registration.relation_counts.items():
            attnames = {model_field.attname for model_field in relation.model._meta.concrete_fields}
            assert relation.relation_column in attnames, name

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_relation_count_names_never_collide_with_metric_names(self, entity):
        """The executor looks a metric's field path up in both tables."""
        registration = get_registration(entity)
        assert not set(registration.relation_counts) & set(registration.metrics)

    def test_calendar_event_title_is_a_string_and_never_a_number(self):
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)
        title = registration.metric("title")
        assert title.kind is FieldKind.STRING
        assert title.aggregate_type is StringAggregate
        assert AggregateOp.SUM not in title.supported_ops
        assert AggregateOp.AVG not in title.supported_ops

    def test_calendar_event_duration_is_a_number_and_never_concatenable(self):
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)
        duration = registration.metric("duration_minutes")
        assert duration.kind is FieldKind.NUMERIC
        assert duration.aggregate_type is NumericAggregate
        assert AggregateOp.CONCAT not in duration.supported_ops


class TestLookupsRaise:
    def test_unknown_entity_raises(self):
        with pytest.raises(UnknownEntityError):
            get_registration("calendar_event")  # a string, not the enum member

    def test_unknown_metric_field_path_raises_rather_than_returning_nothing(self):
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)
        with pytest.raises(UnknownMetricFieldError) as excinfo:
            registration.metric("titel")
        assert excinfo.value.field_path == "titel"

    def test_unknown_dimension_field_path_raises(self):
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)
        with pytest.raises(UnknownDimensionError) as excinfo:
            registration.dimension("nonexistent_column")
        assert excinfo.value.field_path == "nonexistent_column"

    def test_a_field_that_is_aggregatable_is_not_automatically_groupable(self):
        """``title`` is a value, not a key: aggregating it is allowed, grouping is not."""
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)
        assert registration.metric("title") is not None
        with pytest.raises(UnknownDimensionError):
            registration.dimension("title")

    def test_checked_metric_refuses_an_operation_the_kind_does_not_support(self):
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)
        with pytest.raises(UnsupportedOperationError) as excinfo:
            registration.checked_metric("title", AggregateOp.SUM)
        assert excinfo.value.op is AggregateOp.SUM
        assert excinfo.value.kind is FieldKind.STRING

    def test_checked_metric_returns_the_field_for_a_supported_operation(self):
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)
        assert registration.checked_metric("title", AggregateOp.MIN).kind is FieldKind.STRING


class TestRegistrationsAreImmutable:
    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_mappings_cannot_be_written_through(self, entity):
        registration = get_registration(entity)
        with pytest.raises(TypeError):
            registration.metrics["injected"] = None  # type: ignore[index]
        with pytest.raises(TypeError):
            registration.dimensions["injected"] = None  # type: ignore[index]
