"""The collector's own logic: paths, registrations, and the batching key.

These are the parts of `public_api/aggregations/nested.py` that decide *which*
selections share a query, tested without a database because none of them needs
one. Whether the shared query is then correct is `test_nested_batching.py`'s
job.
"""

import graphql
import pytest
from graphql.language import (
    FieldNode,
    FragmentDefinitionNode,
    OperationDefinitionNode,
    parse,
)

from calendar_integration.models import AppointmentType, BlockedTime, Calendar, CalendarPool
from public_api.aggregations.errors import UnknownNestedAggregateError
from public_api.aggregations.fields import AGGREGATE_RESOURCE_BY_ENTITY
from public_api.aggregations.nested import (
    NESTED_AGGREGATES,
    NESTED_FIELD_NAMES,
    NESTED_RESOURCE_BY_FIELD_NAME,
    PARENT_KEY_ALIAS,
    NestedAggregateCollector,
    _as_model_rows,
    _selected_field_names,
    batch_key,
    nested_aggregate_spec,
    parent_list_path,
    response_path,
)
from public_api.aggregations.plan import (
    AggregatableEntity,
    AggregateQueryPlan,
    DimensionSpec,
    MetricSpec,
    ParentKeySpec,
)
from public_api.constants import PublicAPIResources


class _FakeInfo:
    """Just enough of a resolve info to carry a response path."""

    def __init__(self, path: graphql.pyutils.Path) -> None:
        self.path = path


def _path(first: str, *rest: str | int) -> graphql.pyutils.Path:
    path = graphql.pyutils.Path(None, first, None)
    for key in rest:
        path = graphql.pyutils.Path(path, key, None)
    return path


class TestResponsePath:
    def test_the_path_keeps_its_list_indices(self):
        """Dropping them merged an inner list's occurrences into one batch."""
        path = response_path(_FakeInfo(_path("calendarPools", 1, "calendars", 3, "x")))

        assert path == ("calendarPools", 1, "calendars", 3, "x")

    def test_siblings_of_one_list_share_one_batch(self):
        """The whole basis of batching: row 0 and row 24 name the same batch."""
        first = batch_key(response_path(_FakeInfo(_path("calendars", 0, "eventAggregate"))))
        last = batch_key(response_path(_FakeInfo(_path("calendars", 24, "eventAggregate"))))

        assert first == ("calendars", "eventAggregate")
        assert first == last

    def test_the_same_field_under_two_outer_rows_is_two_batches(self):
        """The blocker: one pool's roster must not answer for another's.

        `calendarPools { calendars { eventAggregate } }` resolves `calendars`
        once per pool. Keyed with the outer index dropped, both occurrences
        named one batch, the second pool's calendars were never queried, and
        every one of them reported no events.
        """
        first = batch_key(
            response_path(_FakeInfo(_path("calendarPools", 0, "calendars", 0, "eventAggregate")))
        )
        second = batch_key(
            response_path(_FakeInfo(_path("calendarPools", 1, "calendars", 0, "eventAggregate")))
        )

        assert first == ("calendarPools", 0, "calendars", "eventAggregate")
        assert second == ("calendarPools", 1, "calendars", "eventAggregate")
        assert first != second

    def test_two_aliases_of_one_field_are_two_batches(self):
        """Aliases may carry different arguments, so they must not be merged."""
        march = batch_key(response_path(_FakeInfo(_path("calendars", 0, "march"))))
        april = batch_key(response_path(_FakeInfo(_path("calendars", 0, "april"))))

        assert march == ("calendars", "march")
        assert april == ("calendars", "april")
        assert march != april

    def test_the_parent_path_is_where_that_list_recorded_itself(self):
        """How a batch finds the parents it is for, at either nesting depth."""
        flat = response_path(_FakeInfo(_path("calendars", 3, "eventAggregate")))
        deep = response_path(_FakeInfo(_path("calendarPools", 2, "calendars", 3, "eventAggregate")))

        assert parent_list_path(flat) == ("calendars",)
        assert parent_list_path(deep) == ("calendarPools", 2, "calendars")

    def test_a_parent_that_is_not_a_list_item_keeps_its_whole_prefix(self):
        """`calendarPool(poolId: 3) { eventAggregate }` has no index to drop."""
        path = response_path(_FakeInfo(_path("calendarPool", "eventAggregate")))

        assert parent_list_path(path) == ("calendarPool",)
        assert batch_key(path) == ("calendarPool", "eventAggregate")


class TestRegistrations:
    def test_the_four_fields_the_phase_names_are_registered(self):
        registered = {(spec.parent_model, spec.attribute_name) for spec in NESTED_AGGREGATES}

        assert registered == {
            (Calendar, "event_aggregate"),
            (Calendar, "blocked_time_aggregate"),
            (CalendarPool, "event_aggregate"),
            (AppointmentType, "event_aggregate"),
        }

    @pytest.mark.parametrize(
        ("parent_model", "attribute_name", "entity", "parent_key_path"),
        [
            (
                Calendar,
                "event_aggregate",
                AggregatableEntity.CALENDAR_EVENT,
                "calendar_fk",
            ),
            (
                Calendar,
                "blocked_time_aggregate",
                AggregatableEntity.BLOCKED_TIME,
                "calendar_fk",
            ),
            (
                CalendarPool,
                "event_aggregate",
                AggregatableEntity.CALENDAR_EVENT,
                "calendar__pool_memberships__pool_fk",
            ),
            (
                AppointmentType,
                "event_aggregate",
                AggregatableEntity.CALENDAR_EVENT,
                "appointment_type_fk",
            ),
        ],
    )
    def test_each_field_aggregates_the_right_entity_by_the_right_key(
        self, parent_model, attribute_name, entity, parent_key_path
    ):
        spec = nested_aggregate_spec(parent_model, attribute_name)

        assert spec.entity is entity
        assert spec.parent_key_path == parent_key_path
        assert spec.parent_key == ParentKeySpec(alias=PARENT_KEY_ALIAS, field_path=parent_key_path)

    def test_every_parent_key_path_resolves_against_the_aggregated_model(self):
        """A path that does not resolve is an N+1 nobody would see as one.

        Walked rather than trusted, because the registry's import-time check
        covers the caller-facing dimensions and not this one.
        """
        from public_api.aggregations.registry import get_registration

        for spec in NESTED_AGGREGATES:
            model = get_registration(spec.entity).model
            current = model
            *hops, final = spec.parent_key_path.split("__")
            for hop in hops:
                current = current._meta.get_field(hop).related_model
            assert current._meta.get_field(final) is not None

    def test_an_unregistered_field_is_refused_rather_than_resolved_per_parent(self):
        with pytest.raises(UnknownNestedAggregateError) as excinfo:
            nested_aggregate_spec(BlockedTime, "event_aggregate")

        assert "BlockedTime.event_aggregate" in str(excinfo.value)

    def test_field_names_are_the_camel_cased_attribute_names(self):
        assert NESTED_FIELD_NAMES == {"eventAggregate", "blockedTimeAggregate"}

    def test_each_nested_field_requires_the_aggregated_entity_s_resource(self):
        """Not the parent's: reading a calendar is not reading its events."""
        assert NESTED_RESOURCE_BY_FIELD_NAME == {
            "eventAggregate": PublicAPIResources.CALENDAR_EVENT,
            "blockedTimeAggregate": PublicAPIResources.BLOCKED_TIME,
        }
        for spec in NESTED_AGGREGATES:
            assert (
                NESTED_RESOURCE_BY_FIELD_NAME[spec.field_name]
                == AGGREGATE_RESOURCE_BY_ENTITY[spec.entity]
            )


class TestSelectionScanning:
    """The gate that decides whether a field's result is worth recording."""

    @staticmethod
    def _names(document: str) -> set[str]:
        """The names the scanner sees under `calendars` in ``document``."""
        parsed = parse(document)
        fragments = {
            definition.name.value: definition
            for definition in parsed.definitions
            if isinstance(definition, FragmentDefinitionNode)
        }
        operation = next(
            definition
            for definition in parsed.definitions
            if isinstance(definition, OperationDefinitionNode)
        )
        calendars = operation.selection_set.selections[0]
        assert isinstance(calendars, FieldNode)
        return set(_selected_field_names(calendars.selection_set, fragments))

    def test_a_plain_selection(self):
        assert "eventAggregate" in self._names("{ calendars { id eventAggregate { count } } }")

    def test_an_inline_fragment(self):
        document = "{ calendars { id ... on CalendarGraphQLType { eventAggregate { count } } } }"

        assert "eventAggregate" in self._names(document)

    def test_a_named_fragment(self):
        document = """
        fragment Rollup on CalendarGraphQLType { eventAggregate { count } }
        { calendars { id ...Rollup } }
        """

        assert "eventAggregate" in self._names(document)

    def test_a_selection_with_no_nested_aggregate(self):
        names = self._names("{ calendars { id name } }")

        assert not names & NESTED_FIELD_NAMES


class TestParentRecording:
    def test_each_occurrence_of_an_inner_list_records_its_own_parents(self):
        """Two pools, two rosters, two keys -- because the key carries the index.

        The keys differ, so neither list has to win; the bug was that they did
        not differ and the first one silently did.
        """
        collector = NestedAggregateCollector()
        first_pool = ("calendarPools", 0, "calendars")
        second_pool = ("calendarPools", 1, "calendars")

        collector.record_parents(first_pool, [Calendar(id=10), Calendar(id=11)])
        collector.record_parents(second_pool, [Calendar(id=20), Calendar(id=21)])

        assert [calendar.id for calendar in collector._parents[first_pool]] == [10, 11]
        assert [calendar.id for calendar in collector._parents[second_pool]] == [20, 21]

    def test_only_model_rows_are_recorded(self):
        assert _as_model_rows(["a", "b"]) is None
        assert _as_model_rows(None) is None
        assert _as_model_rows([]) is None
        assert _as_model_rows("a calendar") is None

        rows = _as_model_rows([Calendar(id=7)])
        assert rows is not None
        assert rows[0].id == 7


class TestPlanCarriesTheParentKey:
    """One plan per shape, and the parent key is in it without being a dimension."""

    _PARENT_KEY = ParentKeySpec(alias=PARENT_KEY_ALIAS, field_path="calendar_fk")

    @classmethod
    def _plan(
        cls,
        dimensions: tuple[DimensionSpec, ...] = (
            DimensionSpec(alias="timezone", field_path="timezone"),
        ),
        parent_key: ParentKeySpec | None = _PARENT_KEY,
    ) -> AggregateQueryPlan:
        return AggregateQueryPlan(
            entity=AggregatableEntity.CALENDAR_EVENT,
            dimensions=dimensions,
            metrics=(MetricSpec.row_count(),),
            parent_key=parent_key,
        )

    def test_the_parent_key_is_a_row_key_but_not_a_group_key(self):
        """It is grouped on and dispatched on; it is never published."""
        plan = self._plan()

        assert PARENT_KEY_ALIAS in plan.aliases
        assert PARENT_KEY_ALIAS not in plan.dimension_aliases

    def test_a_dimension_colliding_with_the_parent_key_is_refused(self):
        """Two row keys under one name would overwrite, not merge."""
        from public_api.aggregations.errors import AliasCollisionError

        with pytest.raises(AliasCollisionError):
            self._plan(dimensions=(DimensionSpec(alias=PARENT_KEY_ALIAS, field_path="timezone"),))

    def test_the_audit_record_names_the_batching_key_and_no_parent_ids(self):
        """The shape is recorded; the ids arrive as filter bounds like any other."""
        record = self._plan().as_audit_dict()

        assert record["parent_key"] == {
            "alias": PARENT_KEY_ALIAS,
            "field_path": "calendar_fk",
        }

    def test_an_unbatched_plan_records_no_parent_key(self):
        """A root aggregate's audit record is unchanged by this phase."""
        record = self._plan(parent_key=None).as_audit_dict()

        assert record["parent_key"] is None
