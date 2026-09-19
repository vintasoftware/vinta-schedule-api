"""The registry is the one place that decides what a field can do.

These tests hold that line: every aggregatable field on every entity maps to
exactly one aggregate type, a numeric field never offers ``concat``, and an
unknown field raises instead of returning no metric.
"""

import pytest

from public_api.aggregations.errors import (
    UnknownAggregateFieldError,
    UnsupportedAggregateOperationError,
)
from public_api.aggregations.plan import AggregatableEntity, AggregateOp
from public_api.aggregations.registry import (
    AGGREGATE_TYPE_FOR_KIND,
    OPERATIONS_FOR_KIND,
    REGISTRY,
    FieldKind,
    build_dimension,
    build_metric,
    get_registration,
)
from public_api.aggregations.types import (
    BooleanAggregate,
    DateTimeAggregate,
    NumericAggregate,
    StringAggregate,
    TemporalGranularity,
)
from public_api.constants import PublicAPIResources


ALL_ENTITIES = tuple(AggregatableEntity)


class TestRegistryCoverage:
    def test_every_entity_is_registered(self):
        assert set(REGISTRY) == set(ALL_ENTITIES)

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_registration_matches_its_entity(self, entity):
        registration = get_registration(entity)

        assert registration.entity is entity
        assert isinstance(registration.resource, PublicAPIResources)
        assert registration.graphql_field_name.endswith("_aggregate")

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_registration_keys_match_field_names(self, entity):
        registration = get_registration(entity)

        assert {name: f.name for name, f in registration.aggregatable.items()} == {
            name: name for name in registration.aggregatable
        }
        assert {name: f.name for name, f in registration.groupable.items()} == {
            name: name for name in registration.groupable
        }
        assert {name: f.name for name, f in registration.relation_counts.items()} == {
            name: name for name in registration.relation_counts
        }

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_every_entity_can_be_grouped_and_aggregated(self, entity):
        registration = get_registration(entity)

        assert registration.groupable, f"{entity} has no group-by dimension"
        assert registration.aggregatable, f"{entity} has no aggregatable field"


class TestFieldKindMapping:
    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_every_aggregatable_field_has_exactly_one_aggregate_type(self, entity):
        registration = get_registration(entity)

        for name, registered in registration.aggregatable.items():
            aggregate_types = {
                candidate
                for kind, candidate in AGGREGATE_TYPE_FOR_KIND.items()
                if kind is registered.kind
            }
            assert len(aggregate_types) == 1, f"{entity}.{name} maps to {aggregate_types}"
            assert registered.aggregate_type is aggregate_types.pop()

    def test_kind_to_type_mapping_is_exactly_the_four_aggregate_types(self):
        assert AGGREGATE_TYPE_FOR_KIND == {
            FieldKind.NUMERIC: NumericAggregate,
            FieldKind.STRING: StringAggregate,
            FieldKind.TEMPORAL: DateTimeAggregate,
            FieldKind.BOOLEAN: BooleanAggregate,
        }

    def test_operations_per_kind_are_the_documented_sets(self):
        assert OPERATIONS_FOR_KIND == {
            FieldKind.NUMERIC: (
                AggregateOp.SUM,
                AggregateOp.AVG,
                AggregateOp.MIN,
                AggregateOp.MAX,
            ),
            FieldKind.STRING: (AggregateOp.CONCAT, AggregateOp.MIN, AggregateOp.MAX),
            FieldKind.TEMPORAL: (AggregateOp.MIN, AggregateOp.MAX),
            FieldKind.BOOLEAN: (AggregateOp.TRUE_COUNT, AggregateOp.FALSE_COUNT),
        }

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_no_numeric_field_offers_concat(self, entity):
        registration = get_registration(entity)

        for name, registered in registration.aggregatable.items():
            if registered.kind is FieldKind.NUMERIC:
                assert not registered.supports(AggregateOp.CONCAT), name

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_no_string_field_offers_sum_or_avg(self, entity):
        registration = get_registration(entity)

        for name, registered in registration.aggregatable.items():
            if registered.kind is FieldKind.STRING:
                assert not registered.supports(AggregateOp.SUM), name
                assert not registered.supports(AggregateOp.AVG), name

    def test_title_is_a_string_aggregate_and_never_a_numeric_one(self):
        title = get_registration(AggregatableEntity.CALENDAR_EVENT).metric_field("title")

        assert title.aggregate_type is StringAggregate
        assert title.aggregate_type is not NumericAggregate


class TestUnknownFields:
    def test_unknown_metric_field_raises(self):
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)

        with pytest.raises(UnknownAggregateFieldError) as excinfo:
            registration.metric_field("not_a_field")

        assert "not_a_field" in str(excinfo.value)

    def test_unknown_group_by_field_raises(self):
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)

        with pytest.raises(UnknownAggregateFieldError):
            registration.group_by_field("not_a_field")

    def test_build_metric_rejects_an_unknown_field(self):
        with pytest.raises(UnknownAggregateFieldError):
            build_metric(AggregatableEntity.CALENDAR_EVENT, "not_a_field", AggregateOp.SUM)

    def test_build_metric_rejects_an_unknown_relation_count(self):
        with pytest.raises(UnknownAggregateFieldError):
            build_metric(AggregatableEntity.CALENDAR_EVENT, "not_a_relation", AggregateOp.COUNT)


class TestOperationValidation:
    def test_sum_over_a_string_field_raises(self):
        with pytest.raises(UnsupportedAggregateOperationError) as excinfo:
            build_metric(AggregatableEntity.CALENDAR_EVENT, "title", AggregateOp.SUM)

        assert "title" in str(excinfo.value)
        assert "SUM" in str(excinfo.value)

    def test_concat_over_a_numeric_field_raises(self):
        with pytest.raises(UnsupportedAggregateOperationError):
            build_metric(AggregatableEntity.CALENDAR_EVENT, "duration_minutes", AggregateOp.CONCAT)

    def test_sum_over_a_temporal_field_raises(self):
        with pytest.raises(UnsupportedAggregateOperationError):
            build_metric(AggregatableEntity.CALENDAR_EVENT, "start_time", AggregateOp.SUM)

    def test_supported_operation_builds_a_metric(self):
        metric = build_metric(
            AggregatableEntity.CALENDAR_EVENT, "duration_minutes", AggregateOp.SUM
        )

        assert metric.alias == "duration_minutes_sum"
        assert metric.field_path == "duration_minutes"
        assert metric.op is AggregateOp.SUM

    def test_relation_count_builds_a_metric(self):
        metric = build_metric(
            AggregatableEntity.CALENDAR, "events", AggregateOp.COUNT, alias="event_count"
        )

        assert metric.alias == "event_count"
        assert metric.field_path == "events"
        assert metric.op is AggregateOp.COUNT


class TestDimensionBuilding:
    def test_dimension_carries_the_orm_path_not_the_registry_name(self):
        dimension = build_dimension(AggregatableEntity.CALENDAR_EVENT, "calendar_id")

        assert dimension.alias == "calendar_id"
        assert dimension.field_path == "calendar_fk_id"

    def test_granularity_on_a_temporal_dimension_is_kept(self):
        dimension = build_dimension(
            AggregatableEntity.CALENDAR_EVENT,
            "start_time",
            granularity=TemporalGranularity.DAY,
        )

        assert dimension.granularity is TemporalGranularity.DAY

    def test_granularity_on_a_non_temporal_dimension_raises(self):
        with pytest.raises(UnsupportedAggregateOperationError):
            build_dimension(
                AggregatableEntity.CALENDAR,
                "provider",
                granularity=TemporalGranularity.DAY,
            )

    def test_unknown_dimension_raises(self):
        with pytest.raises(UnknownAggregateFieldError):
            build_dimension(AggregatableEntity.CALENDAR, "not_a_field")
