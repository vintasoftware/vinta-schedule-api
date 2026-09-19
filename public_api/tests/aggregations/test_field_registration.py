"""The six aggregate fields are on the schema, and none of them is ungated.

The load-bearing test here is the one that loops over the registry: a seventh
entity added later gets a field the moment it is registered, and this is what
notices if it got one without a resource to gate it.
"""

import pytest
from graphql import GraphQLNonNull

from public_api.aggregations.fields import (
    FILTER_INPUT_TYPES,
    aggregate_field_name,
)
from public_api.aggregations.having import HAVING_INPUT_TYPES
from public_api.aggregations.ordering import ORDER_INPUT_TYPES
from public_api.aggregations.plan import AggregatableEntity
from public_api.aggregations.registry import get_registration
from public_api.aggregations.rows import AGGREGATE_ROW_TYPES, relation_count_field_name
from public_api.aggregations.types import (
    BooleanAggregate,
    DateTimeAggregate,
    NumericAggregate,
    StringAggregate,
)
from public_api.constants import MAX_PAGE_SIZE
from public_api.permissions import OrganizationResourceAccess
from public_api.schema import schema


ALL_ENTITIES = tuple(AggregatableEntity)

ROW_TYPE_FOR_KIND = {
    "NUMERIC": NumericAggregate,
    "STRING": StringAggregate,
    "TEMPORAL": DateTimeAggregate,
    "BOOLEAN": BooleanAggregate,
}


def _query_fields():
    return schema.as_str()


@pytest.fixture(scope="module")
def query_type():
    return schema._schema.query_type


class TestFieldsAreOnTheSchema:
    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_the_field_exists(self, entity, query_type):
        assert aggregate_field_name(entity) in query_type.fields

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_the_field_takes_the_documented_arguments(self, entity, query_type):
        field = query_type.fields[aggregate_field_name(entity)]

        assert set(field.args) == {
            "filter",
            "groupBy",
            "timezone",
            "having",
            "orderBy",
            "limit",
            "offset",
        }
        # filter, groupBy and timezone are all non-null: an aggregate without a
        # bounded filter or without a dimension is the query this plan exists to
        # prevent, and "per day" has no answer without a clock.
        for required in ("filter", "groupBy", "timezone"):
            assert isinstance(field.args[required].type, GraphQLNonNull), required

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_having_and_order_by_are_optional(self, entity, query_type):
        """Omitting either leaves the field behaving as it did before Phase 4."""
        field = query_type.fields[aggregate_field_name(entity)]

        for optional in ("having", "orderBy"):
            assert not isinstance(field.args[optional].type, GraphQLNonNull), optional
            assert field.args[optional].default_value is None, optional

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_having_and_order_by_take_the_entity_types(self, entity, query_type):
        field = query_type.fields[aggregate_field_name(entity)]

        assert HAVING_INPUT_TYPES[entity].__name__ in str(field.args["having"].type)
        assert ORDER_INPUT_TYPES[entity].__name__ in str(field.args["orderBy"].type)

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_limit_defaults_to_the_shared_page_size(self, entity, query_type):
        field = query_type.fields[aggregate_field_name(entity)]

        assert field.args["limit"].default_value == MAX_PAGE_SIZE
        assert field.args["offset"].default_value == 0

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_the_field_returns_the_entity_row_type(self, entity, query_type):
        field = query_type.fields[aggregate_field_name(entity)]
        row_name = AGGREGATE_ROW_TYPES[entity].__name__

        assert row_name in str(field.type)

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_the_filter_input_is_the_entity_filter(self, entity, query_type):
        field = query_type.fields[aggregate_field_name(entity)]

        assert FILTER_INPUT_TYPES[entity].__name__ in str(field.args["filter"].type)


class TestEveryFieldIsGated:
    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_the_field_has_a_resource_mapping(self, entity):
        """No aggregate field may be added without the resource that gates it."""
        mapping = OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING

        assert aggregate_field_name(entity) in mapping

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_the_resource_is_the_registry_resource(self, entity):
        """The aggregate requires the same resource as the entity's list field."""
        mapping = OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING

        assert mapping[aggregate_field_name(entity)] == get_registration(entity).resource

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_the_field_carries_both_permission_classes(self, entity):
        from public_api.queries import Query

        attribute = get_registration(entity).graphql_field_name
        field = next(
            one for one in Query.__strawberry_definition__.fields if one.python_name == attribute
        )

        assert [cls.__name__ for cls in field.permission_classes] == [
            "IsAuthenticated",
            "OrganizationResourceAccess",
        ]

    def test_no_aggregate_field_maps_to_a_dedicated_analytics_resource(self):
        """A new grant path over the same PHI-bearing columns is the thing to avoid."""
        mapping = OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING

        for entity in ALL_ENTITIES:
            resource = mapping[aggregate_field_name(entity)]
            assert "analytics" not in str(resource).lower()


class TestRowTypesMatchTheRegistry:
    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_one_field_per_aggregatable_field_and_relation_count(self, entity):
        registration = get_registration(entity)
        row_type = AGGREGATE_ROW_TYPES[entity]
        annotations = dict(row_type.__annotations__)

        expected = (
            {"key", "count"}
            | set(registration.aggregatable)
            | {relation_count_field_name(name) for name in registration.relation_counts}
        )
        assert set(annotations) == expected

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_each_field_is_typed_by_its_kind(self, entity):
        registration = get_registration(entity)
        annotations = dict(AGGREGATE_ROW_TYPES[entity].__annotations__)

        for name, registered in registration.aggregatable.items():
            expected = ROW_TYPE_FOR_KIND[registered.kind.value]
            assert annotations[name] == expected | None, name

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_relation_counts_are_nullable_integers(self, entity):
        registration = get_registration(entity)
        annotations = dict(AGGREGATE_ROW_TYPES[entity].__annotations__)

        for name in registration.relation_counts:
            assert annotations[relation_count_field_name(name)] == int | None

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_count_is_non_null_on_the_schema(self, entity, query_type):
        row_name = AGGREGATE_ROW_TYPES[entity].__name__
        row = schema._schema.type_map[row_name]

        assert str(row.fields["count"].type) == "Int!"

    def test_a_string_field_has_no_sum_on_the_schema(self):
        """The schema, not a resolver, is what refuses ``title { sum }``."""
        sdl = _query_fields()

        assert "type StringAggregate" in sdl
        string_block = sdl.split("type StringAggregate")[1].split("}")[0]
        assert "sum" not in string_block
        assert "avg" not in string_block
        assert "concat(" in string_block
