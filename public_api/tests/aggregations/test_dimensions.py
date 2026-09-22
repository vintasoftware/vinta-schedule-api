"""The group-by surface: what can be grouped, how it is named, where it lands.

Most of these are properties held against the registry rather than against a
hand-written list, so a dimension added to an entity later is either covered
by the enums and the group key type or fails here.
"""

import dataclasses
import datetime
from zoneinfo import ZoneInfo

import pytest

from public_api.aggregations import (
    GROUP_BY_INPUT_BY_ENTITY,
    GROUP_KEY_TYPE_BY_ENTITY,
    SCALAR_GROUP_BY_FIELD_BY_ENTITY,
    TEMPORAL_GROUP_BY_FIELD_BY_ENTITY,
    AggregatableEntity,
    AvailableTimeGroupByInput,
    AvailableTimeScalarGroupByField,
    CalendarEventGroupByInput,
    CalendarEventGroupKey,
    CalendarEventScalarGroupByField,
    CalendarEventTemporalGroupByField,
    CalendarEventTemporalGroupByInput,
    GroupByVariantError,
    TemporalGranularity,
    UnknownTimezoneError,
    build_group_key,
    get_registration,
    resolve_bucketing_timezone,
    resolve_dimensions,
    scalar_alias,
    temporal_alias,
)
from public_api.aggregations.errors import UNKNOWN_TIMEZONE_MESSAGE


ALL_ENTITIES = list(AggregatableEntity)


def _alias_for(entity: AggregatableEntity, field_path: str) -> str:
    registration = get_registration(entity)
    if registration.dimensions[field_path].temporal:
        return temporal_alias(field_path)
    return scalar_alias(field_path)


class TestTimezoneResolution:
    @pytest.mark.parametrize(
        "name", ["UTC", "America/Sao_Paulo", "America/New_York", "Europe/London", "Asia/Tokyo"]
    )
    def test_a_real_iana_name_resolves(self, name):
        assert resolve_bucketing_timezone(name) == ZoneInfo(name)

    @pytest.mark.parametrize(
        "name",
        [
            "Nowhere/Nada",  # well-formed key, no such zone
            "America",  # a region, not a zone
            "",  # not a legal key at all
            "../../etc/passwd",  # path traversal, rejected as a key
            "/etc/localtime",  # absolute path
            "BRT",  # an abbreviation, not an IANA name
        ],
    )
    def test_an_unusable_name_is_refused_the_same_way(self, name):
        with pytest.raises(UnknownTimezoneError) as excinfo:
            resolve_bucketing_timezone(name)
        assert excinfo.value.message == UNKNOWN_TIMEZONE_MESSAGE

    def test_the_refusal_never_repeats_the_rejected_name(self):
        """The name is caller input; the message is a fixed string."""
        with pytest.raises(UnknownTimezoneError) as excinfo:
            resolve_bucketing_timezone("Totally/Made-Up-Zone")
        assert "Totally" not in str(excinfo.value)
        assert "Made-Up" not in str(excinfo.value)
        # ``from None`` keeps the original -- which does quote the name -- out
        # of the chain a traceback or an error reporter would walk.
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True


class TestAliasDerivation:
    @pytest.mark.parametrize(
        ("field_path", "expected"),
        [
            ("calendar_fk_id", "calendar_id"),
            ("appointment_type_fk_id", "appointment_type_id"),
            ("bundle_calendar_fk_id", "bundle_calendar_id"),
            ("id", "id"),
            ("timezone", "timezone"),
            ("accepts_public_scheduling", "accepts_public_scheduling"),
        ],
    )
    def test_a_scalar_alias_drops_the_foreign_key_plumbing(self, field_path, expected):
        assert scalar_alias(field_path) == expected

    @pytest.mark.parametrize(
        ("field_path", "expected"),
        [
            ("start_time", "start_time_bucket"),
            ("end_time", "end_time_bucket"),
            ("created", "created_bucket"),
        ],
    )
    def test_a_temporal_alias_is_marked_as_a_bucket(self, field_path, expected):
        assert temporal_alias(field_path) == expected

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_no_temporal_alias_collides_with_a_model_column(self, entity):
        """Django refuses an annotation named after a field; ``_bucket`` avoids it."""
        registration = get_registration(entity)
        column_names = set()
        for model_field in registration.model._meta.get_fields():
            column_names.add(model_field.name)
            attname = getattr(model_field, "attname", None)
            if attname is not None:
                column_names.add(attname)
        for field_path, dimension in registration.dimensions.items():
            if dimension.temporal:
                assert temporal_alias(field_path) not in column_names


class TestEnumsMatchTheRegistry:
    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_every_entity_has_both_enums_an_input_and_a_group_key(self, entity):
        assert entity in SCALAR_GROUP_BY_FIELD_BY_ENTITY
        assert entity in TEMPORAL_GROUP_BY_FIELD_BY_ENTITY
        assert entity in GROUP_BY_INPUT_BY_ENTITY
        assert entity in GROUP_KEY_TYPE_BY_ENTITY

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_the_scalar_enum_holds_exactly_the_non_temporal_dimensions(self, entity):
        registration = get_registration(entity)
        expected = {
            path for path, dimension in registration.dimensions.items() if not dimension.temporal
        }
        assert {member.value for member in SCALAR_GROUP_BY_FIELD_BY_ENTITY[entity]} == expected

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_the_temporal_enum_holds_exactly_the_temporal_dimensions(self, entity):
        registration = get_registration(entity)
        expected = {
            path for path, dimension in registration.dimensions.items() if dimension.temporal
        }
        assert {member.value for member in TEMPORAL_GROUP_BY_FIELD_BY_ENTITY[entity]} == expected

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_the_two_enums_are_disjoint(self, entity):
        """What makes a bucketed foreign key unrepresentable rather than refused."""
        scalars = {member.value for member in SCALAR_GROUP_BY_FIELD_BY_ENTITY[entity]}
        temporals = {member.value for member in TEMPORAL_GROUP_BY_FIELD_BY_ENTITY[entity]}
        assert not scalars & temporals

    def test_a_field_that_is_not_groupable_is_not_in_either_enum(self):
        """``title`` is aggregatable, never a key."""
        scalars = {member.value for member in CalendarEventScalarGroupByField}
        temporals = {member.value for member in CalendarEventTemporalGroupByField}
        for not_a_key in ("title", "description", "duration_minutes"):
            assert not_a_key not in scalars
            assert not_a_key not in temporals

    def test_one_entitys_enum_does_not_accept_anothers_field(self):
        """``AvailableTime`` has no appointment type, so it cannot group by one."""
        available = {member.value for member in AvailableTimeScalarGroupByField}
        assert "appointment_type_fk_id" not in available
        assert "appointment_type_fk_id" in {
            member.value for member in CalendarEventScalarGroupByField
        }


class TestGroupKeyTypes:
    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_the_group_key_declares_a_field_for_every_dimension_alias(self, entity):
        registration = get_registration(entity)
        declared = {
            key_field.name for key_field in dataclasses.fields(GROUP_KEY_TYPE_BY_ENTITY[entity])
        }
        expected = {_alias_for(entity, path) for path in registration.dimensions}
        assert declared == expected

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_every_group_key_field_is_nullable_and_defaults_to_none(self, entity):
        key_type = GROUP_KEY_TYPE_BY_ENTITY[entity]
        instance = key_type()
        for key_field in dataclasses.fields(key_type):
            assert getattr(instance, key_field.name) is None

    def test_only_the_dimensions_the_query_named_are_populated(self):
        key = build_group_key(
            AggregatableEntity.CALENDAR_EVENT,
            {"calendar_id": 12, "start_time_bucket": datetime.datetime(2026, 10, 1)},
        )
        assert key.calendar_id == 12
        assert key.start_time_bucket == datetime.datetime(2026, 10, 1)
        assert key.appointment_type_id is None
        assert key.timezone is None

    def test_metric_columns_in_the_row_are_ignored(self):
        """A whole result row is safe to hand over; only key slots are read."""
        key = build_group_key(
            AggregatableEntity.CALENDAR_EVENT,
            {"calendar_id": 3, "count": 9, "duration_sum": 120.0, "title_min": "Alfa"},
        )
        assert key == CalendarEventGroupKey(calendar_id=3)


class TestResolveDimensions:
    def test_a_scalar_entry_becomes_an_untruncated_dimension(self):
        (dimension,) = resolve_dimensions(
            [CalendarEventGroupByInput(field=CalendarEventScalarGroupByField.CALENDAR_ID)], "UTC"
        )
        assert dimension.alias == "calendar_id"
        assert dimension.field_path == "calendar_fk_id"
        assert dimension.granularity is None
        assert dimension.tzinfo is None

    def test_a_temporal_entry_carries_the_granularity_and_the_timezone(self):
        (dimension,) = resolve_dimensions(
            [
                CalendarEventGroupByInput(
                    temporal=CalendarEventTemporalGroupByInput(
                        field=CalendarEventTemporalGroupByField.START_TIME,
                        granularity=TemporalGranularity.WEEK,
                    )
                )
            ],
            "America/Sao_Paulo",
        )
        assert dimension.alias == "start_time_bucket"
        assert dimension.field_path == "start_time"
        assert dimension.granularity is TemporalGranularity.WEEK
        assert dimension.tzinfo == ZoneInfo("America/Sao_Paulo")

    def test_request_order_is_preserved(self):
        dimensions = resolve_dimensions(
            [
                CalendarEventGroupByInput(
                    temporal=CalendarEventTemporalGroupByInput(
                        field=CalendarEventTemporalGroupByField.START_TIME,
                        granularity=TemporalGranularity.DAY,
                    )
                ),
                CalendarEventGroupByInput(field=CalendarEventScalarGroupByField.CALENDAR_ID),
            ],
            "UTC",
        )
        assert [dimension.alias for dimension in dimensions] == [
            "start_time_bucket",
            "calendar_id",
        ]

    def test_every_temporal_dimension_of_one_query_shares_one_timezone(self):
        """Two wall clocks in one result set is the failure this prevents."""
        dimensions = resolve_dimensions(
            [
                CalendarEventGroupByInput(
                    temporal=CalendarEventTemporalGroupByInput(
                        field=CalendarEventTemporalGroupByField.START_TIME,
                        granularity=TemporalGranularity.DAY,
                    )
                ),
                CalendarEventGroupByInput(
                    temporal=CalendarEventTemporalGroupByInput(
                        field=CalendarEventTemporalGroupByField.CREATED,
                        granularity=TemporalGranularity.MONTH,
                    )
                ),
            ],
            "Asia/Tokyo",
        )
        assert {dimension.tzinfo for dimension in dimensions} == {ZoneInfo("Asia/Tokyo")}

    def test_an_entry_setting_neither_slot_is_refused(self):
        with pytest.raises(GroupByVariantError):
            resolve_dimensions([CalendarEventGroupByInput()], "UTC")

    def test_an_entry_setting_both_slots_is_refused(self):
        with pytest.raises(GroupByVariantError):
            resolve_dimensions(
                [
                    CalendarEventGroupByInput(
                        field=CalendarEventScalarGroupByField.CALENDAR_ID,
                        temporal=CalendarEventTemporalGroupByInput(
                            field=CalendarEventTemporalGroupByField.START_TIME,
                            granularity=TemporalGranularity.DAY,
                        ),
                    )
                ],
                "UTC",
            )

    def test_the_timezone_is_validated_even_when_no_dimension_is_temporal(self):
        """A bad name is refused the same way whatever else the query asked."""
        with pytest.raises(UnknownTimezoneError):
            resolve_dimensions(
                [CalendarEventGroupByInput(field=CalendarEventScalarGroupByField.CALENDAR_ID)],
                "Nowhere/Nada",
            )

    def test_the_timezone_is_validated_before_any_entry_is_read(self):
        with pytest.raises(UnknownTimezoneError):
            resolve_dimensions([CalendarEventGroupByInput()], "Nowhere/Nada")

    def test_other_entities_resolve_through_the_same_path(self):
        (dimension,) = resolve_dimensions(
            [AvailableTimeGroupByInput(field=AvailableTimeScalarGroupByField.CALENDAR_ID)], "UTC"
        )
        assert dimension.alias == "calendar_id"
        assert dimension.field_path == "calendar_fk_id"
