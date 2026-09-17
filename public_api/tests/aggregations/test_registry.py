"""What the aggregate registry promises about every entity it describes.

The registry is the single place that decides a field's aggregate type, so the
claims worth testing are the ones that would otherwise be re-decided per
entity: one type per field, a closed operation set per type, and a loud failure
for a field nobody registered.

None of this touches the database -- the registry is a table plus the model
metadata it validates itself against.
"""

from django.db.models import ExpressionWrapper, F, FloatField, Value
from django.db.models.functions import Extract

import pytest

from calendar_integration.models import (
    AppointmentType,
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarPool,
)
from public_api.aggregations.errors import (
    AggregateRegistrationError,
    ConcatArgumentsMismatchError,
    UnknownAggregateEntityError,
    UnknownAggregateFieldError,
    UnknownDimensionError,
)
from public_api.aggregations.plan import AggregatableEntity, AggregateOp
from public_api.aggregations.registry import (
    GRAPHQL_TYPE_BY_KIND,
    OPS_BY_KIND,
    REGISTRY,
    AggregatableField,
    AggregateKind,
    EntityRegistration,
    aggregate_kind_for_model_field,
    dimension_alias,
    get_registration,
    metric_alias,
    reverse_relation_fk_attname,
)
from public_api.aggregations.types import (
    DEFAULT_CONCAT_SEPARATOR,
    BooleanAggregate,
    DateTimeAggregate,
    NumericAggregate,
    StringAggregate,
    TemporalGranularity,
)


ALL_ENTITIES = tuple(AggregatableEntity)

EXPECTED_MODELS = {
    AggregatableEntity.CALENDAR_EVENT: CalendarEvent,
    AggregatableEntity.AVAILABLE_TIME: AvailableTime,
    AggregatableEntity.BLOCKED_TIME: BlockedTime,
    AggregatableEntity.APPOINTMENT_TYPE: AppointmentType,
    AggregatableEntity.CALENDAR: Calendar,
    AggregatableEntity.CALENDAR_POOL: CalendarPool,
}


class TestEveryEntityIsRegistered:
    def test_all_six_entity_families_have_a_registration(self):
        assert set(REGISTRY) == set(ALL_ENTITIES)
        assert len(ALL_ENTITIES) == 6

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_registration_points_at_the_documented_model(self, entity):
        assert get_registration(entity).model is EXPECTED_MODELS[entity]

    def test_an_unregistered_entity_raises(self):
        with pytest.raises(UnknownAggregateEntityError):
            get_registration("calendar_events_but_misspelled")


class TestOneAggregateTypePerField:
    """Every aggregatable field maps to exactly one of the four types."""

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_each_aggregatable_field_has_exactly_one_kind_and_type(self, entity):
        registration = get_registration(entity)
        assert registration.aggregatable, f"{entity} registers nothing aggregatable"

        for spec in registration.aggregatable:
            assert isinstance(spec.kind, AggregateKind)
            assert spec.graphql_type is GRAPHQL_TYPE_BY_KIND[spec.kind]
            assert spec.ops == OPS_BY_KIND[spec.kind]

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_no_field_is_registered_twice(self, entity):
        registration = get_registration(entity)
        names = [spec.name for spec in registration.aggregatable]
        assert len(names) == len(set(names))

        dimension_names = [spec.name for spec in registration.groupable]
        assert len(dimension_names) == len(set(dimension_names))

    def test_the_four_kinds_map_onto_the_four_published_types(self):
        assert GRAPHQL_TYPE_BY_KIND == {
            AggregateKind.NUMERIC: NumericAggregate,
            AggregateKind.STRING: StringAggregate,
            AggregateKind.DATETIME: DateTimeAggregate,
            AggregateKind.BOOLEAN: BooleanAggregate,
        }

    def test_title_is_a_string_aggregate_and_never_a_numeric_one(self):
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)
        title = registration.aggregatable_field("title")

        assert title.kind is AggregateKind.STRING
        assert title.graphql_type is StringAggregate


class TestOperationsFollowTheFieldType:
    def test_a_numeric_field_never_exposes_concat(self):
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)
        duration = registration.aggregatable_field("duration_minutes")

        assert duration.kind is AggregateKind.NUMERIC
        assert AggregateOp.CONCAT not in duration.ops
        assert not registration.supports("duration_minutes", AggregateOp.CONCAT)
        assert duration.ops == frozenset(
            {AggregateOp.SUM, AggregateOp.AVG, AggregateOp.MIN, AggregateOp.MAX}
        )

    def test_a_string_field_is_never_summable(self):
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)

        assert not registration.supports("title", AggregateOp.SUM)
        assert not registration.supports("title", AggregateOp.AVG)
        assert registration.supports("title", AggregateOp.CONCAT)
        assert registration.supports("title", AggregateOp.MIN)
        assert registration.supports("title", AggregateOp.MAX)

    def test_a_datetime_field_offers_only_min_and_max(self):
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)
        start_time = registration.aggregatable_field("start_time")

        assert start_time.ops == frozenset({AggregateOp.MIN, AggregateOp.MAX})

    def test_a_boolean_field_offers_only_the_two_counts(self):
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)
        flag = registration.aggregatable_field("is_bundle_primary")

        assert flag.ops == frozenset({AggregateOp.TRUE_COUNT, AggregateOp.FALSE_COUNT})

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_no_kind_offers_an_operation_outside_its_own_set(self, entity):
        registration = get_registration(entity)
        for spec in registration.aggregatable:
            for op in AggregateOp:
                assert registration.supports(spec.name, op) == (op in OPS_BY_KIND[spec.kind])


class TestUnknownFieldsRaise:
    """A field nobody registered is a mistake, not an empty result."""

    def test_an_unknown_aggregatable_field_raises(self):
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)

        with pytest.raises(UnknownAggregateFieldError):
            registration.aggregatable_field("patient_ssn")

    def test_an_unknown_dimension_raises(self):
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)

        with pytest.raises(UnknownDimensionError):
            registration.groupable_field("patient_ssn")

    def test_supports_is_false_rather_than_raising_for_an_unknown_field(self):
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)

        assert registration.supports("patient_ssn", AggregateOp.MIN) is False

    def test_an_unregistered_relation_count_is_none(self):
        registration = get_registration(AggregatableEntity.CALENDAR)

        assert registration.relation_count("membership_count") is None
        assert registration.relation_count("event_count") is not None


class TestModelFieldKindMapping:
    """The mapping the registrations are checked against at import time."""

    @pytest.mark.parametrize(
        ("model", "field_name", "expected"),
        [
            (CalendarEvent, "title", AggregateKind.STRING),
            (CalendarEvent, "description", AggregateKind.STRING),
            (CalendarEvent, "is_bundle_primary", AggregateKind.BOOLEAN),
            (CalendarEvent, "start_time", AggregateKind.DATETIME),
            (CalendarEvent, "created", AggregateKind.DATETIME),
            (Calendar, "capacity", AggregateKind.NUMERIC),
            (AppointmentType, "duration", AggregateKind.NUMERIC),
        ],
    )
    def test_field_kind(self, model, field_name, expected):
        assert aggregate_kind_for_model_field(model._meta.get_field(field_name)) is expected

    def test_a_generated_column_is_read_through_its_output_field(self):
        start_time = CalendarEvent._meta.get_field("start_time")

        # The timezone-correct generated column, not the naive editable one.
        assert start_time.__class__.__name__ == "GeneratedField"
        assert aggregate_kind_for_model_field(start_time) is AggregateKind.DATETIME

    def test_a_field_with_no_aggregate_kind_is_none_rather_than_a_guess(self):
        assert aggregate_kind_for_model_field(CalendarEvent._meta.get_field("meta")) is None


class TestADurationColumnMustBeReadThroughAnExpression:
    """A duration column is numeric, but aggregating it returns a ``timedelta``.

    ``NumericAggregate`` publishes floats, so a duration-backed field has to be
    registered with an expression that yields a number. Both duration metrics
    in the registry already do; this is what stops the next one forgetting.
    """

    def test_registering_a_duration_field_without_an_expression_is_refused(self):
        with pytest.raises(AggregateRegistrationError) as excinfo:
            EntityRegistration(
                entity=AggregatableEntity.APPOINTMENT_TYPE,
                model=AppointmentType,
                groupable=(),
                aggregatable=(AggregatableField(name="duration", kind=AggregateKind.NUMERIC),),
            )

        assert "DurationField" in str(excinfo.value)
        assert "expression" in str(excinfo.value)

    def test_the_same_field_is_accepted_behind_a_minutes_expression(self):
        registration = EntityRegistration(
            entity=AggregatableEntity.APPOINTMENT_TYPE,
            model=AppointmentType,
            groupable=(),
            aggregatable=(
                AggregatableField(
                    name="duration_minutes",
                    kind=AggregateKind.NUMERIC,
                    field_path="duration",
                    expression=ExpressionWrapper(
                        Extract(F("duration"), "epoch") / Value(60.0),
                        output_field=FloatField(),
                    ),
                ),
            ),
        )

        assert registration.aggregatable_field("duration_minutes").kind is AggregateKind.NUMERIC

    def test_the_shipped_registrations_read_every_duration_as_minutes(self):
        for entity in (AggregatableEntity.APPOINTMENT_TYPE, AggregatableEntity.CALENDAR_EVENT):
            duration = get_registration(entity).aggregatable_field("duration_minutes")
            assert duration.expression is not None
            assert duration.kind is AggregateKind.NUMERIC


class TestRelationCountsResolve:
    """Each relation count names a reverse foreign key with a concrete column."""

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_every_registered_relation_resolves_to_a_key_column(self, entity):
        registration = get_registration(entity)
        for spec in registration.relation_counts:
            attname = reverse_relation_fk_attname(registration.model, spec.relation)
            assert attname.endswith("_id")

    def test_the_calendar_relations_resolve_to_the_concrete_fk_column(self):
        # ``OrganizationSafeForeignKey`` declares both a ``calendar`` ForeignObject
        # and a concrete ``calendar_fk``; the correlated subquery needs the latter.
        assert reverse_relation_fk_attname(Calendar, "events") == "calendar_fk_id"
        assert reverse_relation_fk_attname(Calendar, "blocked_times") == "calendar_fk_id"


class TestConcatArgumentsAreCheckedNotIgnored:
    """``separator`` / ``distinct`` change the SQL, so the field must verify them.

    A resolver that forgot to read them off the selection would otherwise
    return a string joined on a separator the caller never asked for, and
    nothing downstream could tell.
    """

    def test_matching_arguments_return_the_concatenation(self):
        aggregate = StringAggregate(
            concat_value="a; b",
            concat_separator="; ",
            concat_distinct=True,
        )

        assert aggregate.concat(separator="; ", distinct=True) == "a; b"

    def test_the_defaults_match_a_query_built_with_the_defaults(self):
        aggregate = StringAggregate(concat_value="a,b")

        assert aggregate.concat() == "a,b"
        assert aggregate.concat(separator=DEFAULT_CONCAT_SEPARATOR, distinct=False) == "a,b"

    def test_a_separator_the_query_did_not_use_is_refused(self):
        aggregate = StringAggregate(concat_value="a,b", concat_separator=",")

        with pytest.raises(ConcatArgumentsMismatchError):
            aggregate.concat(separator="; ")

    def test_a_distinctness_the_query_did_not_use_is_refused(self):
        aggregate = StringAggregate(concat_value="a,a,b", concat_distinct=False)

        with pytest.raises(ConcatArgumentsMismatchError):
            aggregate.concat(distinct=True)


class TestAliasNaming:
    """One naming scheme, so a row key is predictable and never shadows a column."""

    def test_metric_alias_joins_the_field_and_the_operation(self):
        assert metric_alias("title", AggregateOp.MIN) == "title_min"
        assert metric_alias("duration_minutes", AggregateOp.SUM) == "duration_minutes_sum"

    def test_default_options_do_not_lengthen_the_alias(self):
        assert metric_alias("title", AggregateOp.CONCAT) == "title_concat"
        assert (
            metric_alias("title", AggregateOp.CONCAT, {"separator": DEFAULT_CONCAT_SEPARATOR})
            == "title_concat"
        )
        assert (
            metric_alias("title", AggregateOp.CONCAT, {"separator": ",", "distinct": False})
            == "title_concat"
        )
        assert metric_alias("id", AggregateOp.COUNT, {"distinct": False}) == "id_count"

    def test_distinct_is_named_in_the_alias(self):
        assert (
            metric_alias("title", AggregateOp.CONCAT, {"distinct": True}) == "title_concat_distinct"
        )
        assert metric_alias("id", AggregateOp.COUNT, {"distinct": True}) == "id_count_distinct"

    def test_two_concats_with_different_separators_get_different_aliases(self):
        # One legal GraphQL document may select concat twice under two response
        # keys; one row key for two different strings would lose one of them.
        commas = metric_alias("title", AggregateOp.CONCAT, {"separator": ","})
        lines = metric_alias("title", AggregateOp.CONCAT, {"separator": "\n"})
        semicolons = metric_alias("title", AggregateOp.CONCAT, {"separator": "; "})

        assert len({commas, lines, semicolons}) == 3

    def test_separator_and_distinctness_vary_independently(self):
        aliases = {
            metric_alias("title", AggregateOp.CONCAT, options)
            for options in (
                {},
                {"distinct": True},
                {"separator": "; "},
                {"separator": "; ", "distinct": True},
            )
        }

        assert len(aliases) == 4

    def test_the_alias_for_one_set_of_options_is_stable(self):
        # Not `hash()`, which is salted per process: a row key has to survive
        # into the next request that builds the same query.
        first = metric_alias("title", AggregateOp.CONCAT, {"separator": "; ", "distinct": True})
        second = metric_alias("title", AggregateOp.CONCAT, {"distinct": True, "separator": "; "})

        assert first == second
        assert first == "title_concat_distinct_ccb7d2c9"

    def test_dimension_alias_carries_the_bucket_size_when_there_is_one(self):
        assert dimension_alias("calendar_id") == "calendar_id"
        assert dimension_alias("start_time", TemporalGranularity.DAY) == "start_time_day"
        assert dimension_alias("start_time", TemporalGranularity.WEEK) == "start_time_week"

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_no_metric_alias_collides_with_a_column_of_its_own_model(self, entity):
        registration = get_registration(entity)
        column_names = {
            name
            for model_field in registration.model._meta.get_fields()
            for name in (model_field.name, getattr(model_field, "attname", None))
            if name
        }

        for spec in registration.aggregatable:
            for op in spec.ops:
                assert metric_alias(spec.name, op) not in column_names
