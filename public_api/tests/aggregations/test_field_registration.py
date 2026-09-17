"""The six aggregate root fields exist, and none of them is unguarded.

Both halves are driven off `AggregatableEntity` rather than off a list written
out here. A seventh entity registered with no root field, or a root field added
with no entry in `FIELD_TO_RESOURCE_MAPPING`, has to fail this module rather
than reach a partner: an unmapped field name falls through
`OrganizationResourceAccess`'s `.get(info.field_name, info.field_name)` default
and is then checked against a resource nobody grants, which reads as "always
refused" until somebody creates a resource by that name.
"""

import pytest
from graphql import GraphQLObjectType, get_named_type

from public_api.aggregations.fields import (
    AGGREGATE_ATTRIBUTE_NAME_BY_ENTITY,
    AGGREGATE_FIELD_NAME_BY_ENTITY,
    AGGREGATE_RESOURCE_BY_ENTITY,
    AGGREGATE_ROW_TYPE_BY_ENTITY,
    entity_class_prefix,
)
from public_api.aggregations.plan import AggregatableEntity, AggregateOp
from public_api.aggregations.registry import get_registration
from public_api.permissions import OrganizationResourceAccess
from public_api.schema import schema


def _query_fields() -> dict[str, object]:
    query_type = schema._schema.query_type
    assert isinstance(query_type, GraphQLObjectType)
    return dict(query_type.fields)


class TestAggregateFieldRegistration:
    def test_every_entity_has_a_root_field_on_the_schema(self):
        """All six aggregate fields are reachable from `Query`."""
        fields = _query_fields()
        expected = {AGGREGATE_FIELD_NAME_BY_ENTITY[entity] for entity in AggregatableEntity}
        assert expected == {
            "calendarEventAggregate",
            "availableTimeAggregate",
            "blockedTimeAggregate",
            "appointmentTypeAggregate",
            "calendarAggregate",
            "calendarPoolAggregate",
        }
        missing = expected - set(fields)
        assert not missing, f"aggregate fields absent from the schema: {sorted(missing)}"

    def test_every_root_field_has_a_resource_mapping(self):
        """The loop the phase asks for: no field may be added without its resource."""
        mapping = OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING
        for entity in AggregatableEntity:
            field_name = AGGREGATE_FIELD_NAME_BY_ENTITY[entity]
            assert field_name in mapping, f"{field_name} has no FIELD_TO_RESOURCE_MAPPING entry"
            assert mapping[field_name] == AGGREGATE_RESOURCE_BY_ENTITY[entity]

    def test_aggregate_resource_matches_the_entity_list_field(self):
        """Each aggregate is gated by the same resource as the entity's list field."""
        mapping = OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING
        list_field_by_entity = {
            AggregatableEntity.CALENDAR_EVENT: "calendarEvents",
            AggregatableEntity.AVAILABLE_TIME: "availableTimes",
            AggregatableEntity.BLOCKED_TIME: "blockedTimes",
            AggregatableEntity.APPOINTMENT_TYPE: "appointmentTypes",
            AggregatableEntity.CALENDAR: "calendars",
            AggregatableEntity.CALENDAR_POOL: "calendarPools",
        }
        for entity, list_field in list_field_by_entity.items():
            aggregate_field_name = AGGREGATE_FIELD_NAME_BY_ENTITY[entity]
            assert mapping[aggregate_field_name] == mapping[list_field]

    def test_attribute_names_camel_case_to_the_graphql_names(self):
        """The `Query` attribute is what Strawberry turns into the field name."""
        assert AGGREGATE_ATTRIBUTE_NAME_BY_ENTITY[AggregatableEntity.CALENDAR_EVENT] == (
            "calendar_event_aggregate"
        )
        assert AGGREGATE_FIELD_NAME_BY_ENTITY[AggregatableEntity.CALENDAR_EVENT] == (
            "calendarEventAggregate"
        )

    def test_root_fields_carry_both_permission_classes(self):
        """Neither guard may be dropped from a generated field."""
        from public_api.permissions import IsAuthenticated
        from public_api.queries import Query

        by_name = {field.python_name: field for field in Query.__strawberry_definition__.fields}
        for entity in AggregatableEntity:
            attribute = AGGREGATE_ATTRIBUTE_NAME_BY_ENTITY[entity]
            assert by_name[attribute].permission_classes == [
                IsAuthenticated,
                OrganizationResourceAccess,
            ], f"{attribute} is missing its permission classes"


class TestAggregateRowTypes:
    def test_row_type_names_follow_the_entity(self):
        for entity in AggregatableEntity:
            row_type = AGGREGATE_ROW_TYPE_BY_ENTITY[entity]
            assert row_type.__name__ == f"{entity_class_prefix(entity)}AggregateRow"

    def test_row_type_carries_one_field_per_registered_aggregatable(self):
        """The row type is generated from the registration, so it cannot drift."""
        import dataclasses

        for entity in AggregatableEntity:
            registration = get_registration(entity)
            row_fields = {
                field.name for field in dataclasses.fields(AGGREGATE_ROW_TYPE_BY_ENTITY[entity])
            }
            assert "key" in row_fields
            assert "count" in row_fields
            for spec in registration.aggregatable:
                assert spec.name in row_fields, f"{entity.value}.{spec.name} absent from the row"
            for relation in registration.relation_counts:
                assert relation.name in row_fields

    def test_row_field_type_follows_the_registered_kind(self):
        """A string field gets `StringAggregate`; it never gets `sum`."""
        query_field = _query_fields()["calendarEventAggregate"]
        # `[CalendarEventAggregateRow!]!` -- NonNull(List(NonNull(Row))).
        row_type = get_named_type(query_field.type)
        assert isinstance(row_type, GraphQLObjectType)
        assert row_type.name == "CalendarEventAggregateRow"
        assert row_type.fields["title"].type.name == "StringAggregate"
        assert row_type.fields["durationMinutes"].type.name == "NumericAggregate"
        assert row_type.fields["isBundlePrimary"].type.name == "BooleanAggregate"
        assert row_type.fields["startTime"].type.name == "DateTimeAggregate"

    def test_string_aggregate_offers_no_numeric_operation(self):
        """The schema-level guarantee `sum` on a CharField is refused by."""
        string_aggregate = schema._schema.type_map["StringAggregate"]
        assert set(string_aggregate.fields) == {"min", "max", "concat"}
        assert AggregateOp.SUM.value not in string_aggregate.fields


@pytest.mark.django_db
class TestAggregateFieldsAreQueryable:
    def test_schema_validates_a_well_formed_aggregate_document(self):
        """A document the schema accepts is the point of all the wiring above."""
        from graphql import parse, validate

        document = parse(
            """
            query {
              calendarEventAggregate(
                filter: {startDatetime: "2026-01-01T00:00:00Z", endDatetime: "2026-02-01T00:00:00Z"}
                groupBy: [{scalar: CALENDAR_ID}]
                timezone: "UTC"
              ) {
                key { calendarId }
                count
                durationMinutes { sum avg }
                title { concat(separator: "; ") }
              }
            }
            """
        )
        assert validate(schema._schema, document) == []

    def test_schema_refuses_sum_over_a_string_field(self):
        """`title { sum }` fails validation, not a resolver."""
        from graphql import parse, validate

        document = parse(
            """
            query {
              calendarEventAggregate(
                filter: {startDatetime: "2026-01-01T00:00:00Z", endDatetime: "2026-02-01T00:00:00Z"}
                groupBy: [{scalar: CALENDAR_ID}]
                timezone: "UTC"
              ) {
                title { sum }
              }
            }
            """
        )
        errors = validate(schema._schema, document)
        assert errors, "the schema accepted `sum` over a StringAggregate"
