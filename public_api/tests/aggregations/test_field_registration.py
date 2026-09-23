"""All six aggregate fields are on the schema, and every one is resource-mapped.

Two properties, checked independently of each other:

* the schema actually exposes ``<entity>Aggregate`` for all six registered
  entities -- a field that silently failed to wire up would otherwise only
  surface as a confusing "Cannot query field" error much later;
* every field the schema exposes has an entry in
  ``OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING`` naming the same
  resource as the entity's existing list field -- a field present on the
  schema but absent from that mapping is a scope-check gap: any
  authenticated token could call it regardless of its resource grants (see
  ``OrganizationResourceAccess.has_permission``, which falls back to the
  raw field name when the mapping has no entry, refusing only tokens that
  lack a resource of that literal name).
"""

import dataclasses

from strawberry.utils.str_converters import to_camel_case

from public_api.aggregations.output_types import ROW_TYPE_BY_ENTITY
from public_api.aggregations.registry import get_registration, registered_entities
from public_api.permissions import OrganizationResourceAccess
from public_api.schema import schema


def _aggregate_field_name(entity) -> str:
    return to_camel_case(f"{entity.value}_aggregate")


class TestAggregateFieldsOnSchema:
    def test_every_registered_entity_has_a_schema_field(self):
        query_type = schema._schema.type_map["Query"]
        field_names = set(query_type.fields)

        for entity in registered_entities():
            field_name = _aggregate_field_name(entity)
            assert field_name in field_names, (
                f"{field_name!r} is not registered on the schema's Query type"
            )

    def test_schema_has_exactly_six_aggregate_fields(self):
        query_type = schema._schema.type_map["Query"]
        aggregate_fields = {name for name in query_type.fields if name.endswith("Aggregate")}
        expected = {_aggregate_field_name(entity) for entity in registered_entities()}
        assert aggregate_fields == expected

    def test_every_aggregate_field_returns_a_list(self):
        """Every aggregate field's return type is a non-null list of rows,
        never a single row or a connection -- see the plan's API Design."""
        query_type = schema._schema.type_map["Query"]
        for entity in registered_entities():
            field = query_type.fields[_aggregate_field_name(entity)]
            assert str(field.type).startswith("[") and str(field.type).endswith("!]!"), (
                f"{_aggregate_field_name(entity)!r} does not return a non-null list: {field.type}"
            )


class TestAggregateFieldToResourceMappingCompleteness:
    """No aggregate field was added without a resource-scope entry."""

    def test_every_aggregate_field_is_mapped(self):
        mapped = set(OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING)
        for entity in registered_entities():
            field_name = _aggregate_field_name(entity)
            assert field_name in mapped, (
                f"{field_name!r} is reachable but absent from FIELD_TO_RESOURCE_MAPPING "
                "(a scope-check gap: any authenticated token could call it)"
            )

    def test_every_aggregate_field_maps_to_its_entitys_own_resource(self):
        """The resource an aggregate requires is exactly the one its entity's
        registry entry names -- the same resource the existing list field
        for that entity requires, per the plan's Guiding Decisions."""
        from public_api.aggregations.registry import get_registration

        for entity in registered_entities():
            field_name = _aggregate_field_name(entity)
            registration = get_registration(entity)
            assert (
                OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING[field_name]
                == registration.resource
            )


class TestAggregateRowTypesMatchTheRegistry:
    """A row type's fields are exactly what its entity's registration promises.

    ``output_types.py``'s row types are hand-written rather than generated
    from the registry (see its module docstring), so nothing else stops a
    metric or relation count added to a registration from silently never
    reaching its row type. This loops both sides and diffs them.
    """

    def test_row_type_fields_match_registration_exactly(self):
        for entity in registered_entities():
            registration = get_registration(entity)
            row_type = ROW_TYPE_BY_ENTITY[entity]
            row_field_names = {f.name for f in dataclasses.fields(row_type)}

            # ``key``, ``count`` and ``window`` are the three fields a row
            # carries that no registration mentions: the group key, the
            # mandatory row count, and the window columns, which are computed
            # over a metric rather than being one.
            expected = (
                {"key", "count", "window"}
                | set(registration.metrics)
                | set(registration.relation_counts)
            )
            assert row_field_names == expected, (
                f"{row_type.__name__} fields {row_field_names} do not match "
                f"{entity!r}'s registration {expected}"
            )
