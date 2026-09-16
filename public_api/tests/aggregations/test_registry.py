"""Tests for the aggregate registry -- the one place that decides what a field exposes."""

import datetime

from django.core.exceptions import FieldError
from django.db.models import BooleanField, DateTimeField
from django.db.models.fields.reverse_related import ForeignObjectRel
from django.db.models.sql.query import Query

import pytest

from public_api.aggregations import errors
from public_api.aggregations.plan import AggregatableEntity, AggregateOp
from public_api.aggregations.registry import (
    AGGREGATE_TYPE_BY_KIND,
    OPS_BY_KIND,
    AggregateKind,
    aggregate_type_for_field,
    assert_granularity_allowed,
    assert_op_supported,
    assert_organization_scoped,
    get_registration,
    registered_entities,
)
from public_api.aggregations.types import (
    BooleanAggregate,
    DateTimeAggregate,
    NumericAggregate,
    StringAggregate,
)


AGGREGATE_TYPES = {NumericAggregate, StringAggregate, DateTimeAggregate, BooleanAggregate}


def _resolve(model: type, path: str) -> object:
    """Resolve an ORM path against `model` without touching the database.

    Query construction only; nothing is executed, so no organization needs to
    be bound and no manager is consulted.
    """
    return Query(model).resolve_ref(path)


class TestRegistryCoverage:
    """Every entity family the plan names has a complete registration."""

    def test_all_six_entities_are_registered(self):
        assert set(registered_entities()) == set(AggregatableEntity)

    @pytest.mark.parametrize("entity", list(AggregatableEntity))
    def test_registration_declares_a_resource_and_a_date_range_field(self, entity):
        registration = get_registration(entity)

        assert registration.entity is entity
        assert registration.resource
        assert registration.date_range_field in {
            field.field_path for field in registration.groupable.values()
        }

    @pytest.mark.parametrize("entity", list(AggregatableEntity))
    def test_registration_has_at_least_one_dimension_and_one_metric(self, entity):
        registration = get_registration(entity)

        assert registration.groupable
        assert registration.aggregatable

    @pytest.mark.parametrize("entity", list(AggregatableEntity))
    def test_every_registered_model_is_organization_scoped(self, entity):
        # No assertion needed beyond "this does not raise": an unscoped model
        # would let a GROUP BY aggregate across tenants.
        assert_organization_scoped(get_registration(entity).model)


class TestFieldKindMapping:
    """A field's kind decides its aggregate type and its operations, once."""

    def test_the_two_kind_tables_cover_exactly_the_same_kinds(self):
        assert set(AGGREGATE_TYPE_BY_KIND) == set(AggregateKind)
        assert set(OPS_BY_KIND) == set(AggregateKind)

    def test_each_kind_maps_to_a_distinct_aggregate_type(self):
        mapped = list(AGGREGATE_TYPE_BY_KIND.values())

        assert set(mapped) == AGGREGATE_TYPES
        assert len(mapped) == len(set(mapped))

    @pytest.mark.parametrize("entity", list(AggregatableEntity))
    def test_every_aggregatable_field_maps_to_exactly_one_aggregate_type(self, entity):
        registration = get_registration(entity)

        for name, field in registration.aggregatable.items():
            aggregate_type = aggregate_type_for_field(entity, name)

            assert aggregate_type in AGGREGATE_TYPES
            assert aggregate_type is AGGREGATE_TYPE_BY_KIND[field.kind]
            assert field.aggregate_type is aggregate_type

    def test_a_numeric_field_never_exposes_concat(self):
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)
        duration = registration.get_aggregatable("duration_minutes")

        assert duration.kind is AggregateKind.NUMERIC
        assert AggregateOp.CONCAT not in duration.supported_ops

        with pytest.raises(errors.UnsupportedOperationError):
            assert_op_supported(duration, AggregateOp.CONCAT)

    def test_a_string_field_never_exposes_sum_or_avg(self):
        title = get_registration(AggregatableEntity.CALENDAR_EVENT).get_aggregatable("title")

        assert title.kind is AggregateKind.STRING
        assert title.supported_ops == (AggregateOp.CONCAT, AggregateOp.MIN, AggregateOp.MAX)

        for op in (AggregateOp.SUM, AggregateOp.AVG):
            with pytest.raises(errors.UnsupportedOperationError):
                assert_op_supported(title, op)

    def test_a_datetime_field_exposes_only_min_and_max(self):
        start = get_registration(AggregatableEntity.CALENDAR_EVENT).get_aggregatable("start_time")

        assert start.supported_ops == (AggregateOp.MIN, AggregateOp.MAX)

    def test_a_boolean_field_exposes_only_the_two_counts(self):
        flag = get_registration(AggregatableEntity.CALENDAR_EVENT).get_aggregatable(
            "is_bundle_primary"
        )

        assert flag.supported_ops == (AggregateOp.TRUE_COUNT, AggregateOp.FALSE_COUNT)

    def test_every_supported_op_is_accepted_by_the_assertion(self):
        for entity in AggregatableEntity:
            registration = get_registration(entity)
            for field in registration.aggregatable.values():
                for op in field.supported_ops:
                    assert_op_supported(field, op)


class TestUnknownNamesRaise:
    """An unknown name raises rather than silently producing no metric."""

    def test_unknown_entity_raises(self):
        with pytest.raises(errors.UnknownEntityError):
            get_registration("not_an_entity")  # type: ignore[arg-type]

    def test_unknown_aggregatable_field_raises(self):
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)

        with pytest.raises(errors.UnknownFieldError, match="no aggregatable field"):
            registration.get_aggregatable("start_time_tz_unaware")

    def test_unknown_groupable_field_raises(self):
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)

        with pytest.raises(errors.UnknownFieldError, match="no groupable field"):
            registration.get_groupable("title")

    def test_unknown_countable_relation_raises(self):
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)

        with pytest.raises(errors.UnknownFieldError, match="no countable relation"):
            registration.get_countable_relation("bundle_representations")

    def test_an_entity_with_no_countable_relations_still_raises_for_one(self):
        registration = get_registration(AggregatableEntity.AVAILABLE_TIME)

        assert registration.countable_relations == {}
        with pytest.raises(errors.UnknownFieldError):
            registration.get_countable_relation("anything")


class TestFieldPathsResolve:
    """Every declared path is a real ORM path on the entity's model."""

    @pytest.mark.parametrize("entity", list(AggregatableEntity))
    def test_groupable_paths_resolve(self, entity):
        registration = get_registration(entity)

        for field in registration.groupable.values():
            _resolve(registration.model, field.field_path)

    @pytest.mark.parametrize("entity", list(AggregatableEntity))
    def test_stored_aggregatable_paths_resolve(self, entity):
        registration = get_registration(entity)

        for field in registration.aggregatable.values():
            if field.expression is not None:
                # Derived in SQL from other columns; it has no path of its own.
                assert field.field_path == ""
                continue
            _resolve(registration.model, field.field_path)

    def test_an_unregistered_path_would_not_resolve(self):
        # Guards the check above: it fails for a bad path rather than passing
        # for everything.
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)

        with pytest.raises(FieldError):
            _resolve(registration.model, "no_such_column")

    @pytest.mark.parametrize("entity", list(AggregatableEntity))
    def test_temporal_dimensions_are_datetime_columns(self, entity):
        registration = get_registration(entity)

        for field in registration.groupable.values():
            if not field.is_temporal:
                continue
            resolved = _resolve(registration.model, field.field_path)
            assert isinstance(resolved.output_field, DateTimeField)

    @pytest.mark.parametrize("entity", list(AggregatableEntity))
    def test_boolean_metrics_are_boolean_columns(self, entity):
        registration = get_registration(entity)

        for field in registration.aggregatable.values():
            if field.kind is not AggregateKind.BOOLEAN:
                continue
            resolved = _resolve(registration.model, field.field_path)
            assert isinstance(resolved.output_field, BooleanField)

    @pytest.mark.parametrize("entity", list(AggregatableEntity))
    def test_countable_relations_are_reverse_relations(self, entity):
        registration = get_registration(entity)

        for relation in registration.countable_relations.values():
            rel = registration.model._meta.get_field(relation.relation_path)

            assert isinstance(rel, ForeignObjectRel)
            assert_organization_scoped(rel.related_model)


class TestGranularityGate:
    """A granularity belongs to a temporal dimension and nowhere else."""

    def test_granularity_allowed_on_a_temporal_dimension(self):
        start = get_registration(AggregatableEntity.CALENDAR_EVENT).get_groupable("start_time")

        assert start.is_temporal
        assert_granularity_allowed(start, True)

    def test_granularity_refused_on_a_non_temporal_dimension(self):
        calendar_id = get_registration(AggregatableEntity.CALENDAR_EVENT).get_groupable(
            "calendar_id"
        )

        assert not calendar_id.is_temporal
        assert_granularity_allowed(calendar_id, False)
        with pytest.raises(errors.UnsupportedOperationError, match="takes no granularity"):
            assert_granularity_allowed(calendar_id, True)


class TestDerivedExpressions:
    """The derived metrics are SQL expressions, not Python arithmetic."""

    def test_duration_minutes_is_derived_for_every_timed_entity(self):
        for entity in (
            AggregatableEntity.CALENDAR_EVENT,
            AggregatableEntity.AVAILABLE_TIME,
            AggregatableEntity.BLOCKED_TIME,
        ):
            field = get_registration(entity).get_aggregatable("duration_minutes")

            assert field.kind is AggregateKind.NUMERIC
            assert field.expression is not None
            # Building it twice yields equal expressions -- the registry holds a
            # builder rather than a shared, mutable expression instance.
            assert field.build_source() == field.build_source()

    def test_appointment_type_duration_minutes_is_derived_from_its_interval_column(self):
        field = get_registration(AggregatableEntity.APPOINTMENT_TYPE).get_aggregatable(
            "duration_minutes"
        )

        assert field.expression is not None
        assert "duration" in str(field.build_source())

    def test_naive_wall_clock_columns_are_never_exposed(self):
        # ``start_time_tz_unaware`` is a local reading, not an instant.
        # Grouping or comparing it across timezones is wrong, so it is absent
        # from both halves of every timed registration.
        for entity in (
            AggregatableEntity.CALENDAR_EVENT,
            AggregatableEntity.AVAILABLE_TIME,
            AggregatableEntity.BLOCKED_TIME,
        ):
            registration = get_registration(entity)
            paths = {field.field_path for field in registration.groupable.values()} | {
                field.field_path for field in registration.aggregatable.values()
            }

            assert "start_time_tz_unaware" not in paths
            assert "end_time_tz_unaware" not in paths


class TestRegistrationsAreImmutable:
    """The registry is read-only at runtime."""

    @pytest.mark.parametrize("entity", list(AggregatableEntity))
    def test_field_maps_cannot_be_mutated(self, entity):
        registration = get_registration(entity)

        with pytest.raises(TypeError):
            registration.aggregatable["injected"] = object()  # type: ignore[index]
        with pytest.raises(TypeError):
            registration.groupable["injected"] = object()  # type: ignore[index]

    def test_a_registration_cannot_be_repointed_at_another_model(self):
        registration = get_registration(AggregatableEntity.CALENDAR)

        with pytest.raises(AttributeError):
            registration.model = datetime.datetime  # type: ignore[misc]
