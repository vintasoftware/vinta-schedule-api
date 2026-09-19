"""The group-by surface: what a caller may name, and what the key comes back as.

The enums and the ``*GroupKey`` types are written out by hand, so the tests that
matter here are the ones that catch them drifting from the registry.
"""

import datetime
import enum
from zoneinfo import ZoneInfo

import pytest

from public_api.aggregations.dimensions import (
    GROUP_BY_INPUT_TYPES,
    GROUP_KEY_TYPES,
    SCALAR_GROUP_BY_ENUMS,
    TEMPORAL_GROUP_BY_ENUMS,
    AppointmentTypeGroupByInput,
    AppointmentTypeScalarGroupByField,
    CalendarEventGroupByInput,
    CalendarEventGroupKey,
    CalendarEventScalarGroupByField,
    CalendarEventTemporalGroupBy,
    CalendarEventTemporalGroupByField,
    CalendarPoolGroupByInput,
    CalendarPoolTemporalGroupBy,
    CalendarPoolTemporalGroupByField,
    build_group_key,
    resolve_group_by,
    resolve_group_by_inputs,
)
from public_api.aggregations.errors import (
    InvalidAggregatePlanError,
    UnknownTimezoneError,
)
from public_api.aggregations.plan import AggregatableEntity
from public_api.aggregations.registry import FieldKind, get_registration
from public_api.aggregations.timezone import resolve_timezone
from public_api.aggregations.types import TemporalGranularity


ALL_ENTITIES = tuple(AggregatableEntity)
SAO_PAULO = ZoneInfo("America/Sao_Paulo")

#: Python type each field kind takes on a group key.
KEY_TYPE_FOR_KIND = {
    FieldKind.NUMERIC: int,
    FieldKind.STRING: str,
    FieldKind.BOOLEAN: bool,
    FieldKind.TEMPORAL: datetime.datetime,
}


class TestTimezoneValidation:
    def test_a_known_iana_name_resolves(self):
        assert resolve_timezone("America/Sao_Paulo") == SAO_PAULO

    @pytest.mark.parametrize(
        "name",
        ["Mars/Olympus_Mons", "", "Not A Zone", "/etc/localtime", "america/sao_paulo"],
    )
    def test_an_unknown_name_raises(self, name):
        with pytest.raises(UnknownTimezoneError):
            resolve_timezone(name)

    def test_the_error_never_repeats_the_input(self):
        with pytest.raises(UnknownTimezoneError) as excinfo:
            resolve_timezone("Mars/Olympus_Mons")

        assert str(excinfo.value) == "Unknown timezone"
        assert "Mars" not in str(excinfo.value)
        assert excinfo.value.__cause__ is None


class TestEnumsMatchTheRegistry:
    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_temporal_enum_holds_exactly_the_temporal_dimensions(self, entity):
        registration = get_registration(entity)
        expected = {
            name
            for name, registered in registration.groupable.items()
            if registered.kind is FieldKind.TEMPORAL
        }

        assert {member.value for member in TEMPORAL_GROUP_BY_ENUMS[entity]} == expected

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_scalar_enum_holds_exactly_the_non_temporal_dimensions(self, entity):
        registration = get_registration(entity)
        expected = {
            name
            for name, registered in registration.groupable.items()
            if registered.kind is not FieldKind.TEMPORAL
        }

        if not expected:
            # GraphQL has no empty enum, so an entity with nothing categorical
            # to group on gets no scalar variant at all.
            assert entity not in SCALAR_GROUP_BY_ENUMS
            return
        assert {member.value for member in SCALAR_GROUP_BY_ENUMS[entity]} == expected

    def test_calendar_pool_is_the_only_entity_without_a_scalar_variant(self):
        assert set(SCALAR_GROUP_BY_ENUMS) == set(ALL_ENTITIES) - {AggregatableEntity.CALENDAR_POOL}

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_enum_member_names_are_the_upper_cased_field_names(self, entity):
        enums: list[type[enum.Enum]] = [TEMPORAL_GROUP_BY_ENUMS[entity]]
        if entity in SCALAR_GROUP_BY_ENUMS:
            enums.append(SCALAR_GROUP_BY_ENUMS[entity])

        for group_by_enum in enums:
            for member in group_by_enum:
                assert member.name == str(member.value).upper()

    def test_a_field_that_is_not_groupable_is_not_in_any_enum(self):
        """``title`` is aggregatable but never a group key, so it has no member."""
        registration = get_registration(AggregatableEntity.CALENDAR_EVENT)
        assert "title" in registration.aggregatable
        assert "title" not in registration.groupable

        members = {member.value for member in CalendarEventScalarGroupByField} | {
            member.value for member in CalendarEventTemporalGroupByField
        }
        assert "title" not in members

    def test_the_temporal_enum_rejects_a_scalar_field(self):
        with pytest.raises(ValueError, match="is not a valid"):
            CalendarEventTemporalGroupByField("calendar_id")

    def test_the_scalar_enum_rejects_a_temporal_field(self):
        with pytest.raises(ValueError, match="is not a valid"):
            CalendarEventScalarGroupByField("start_time")


class TestGroupKeyTypesMatchTheRegistry:
    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_one_nullable_field_per_groupable_dimension(self, entity):
        registration = get_registration(entity)
        key_type = GROUP_KEY_TYPES[entity]
        annotations = key_type.__annotations__

        assert set(annotations) == set(registration.groupable)
        for name, registered in registration.groupable.items():
            expected = KEY_TYPE_FOR_KIND[registered.kind]
            assert annotations[name] == expected | None

    @pytest.mark.parametrize("entity", ALL_ENTITIES)
    def test_every_group_key_field_defaults_to_none(self, entity):
        key = GROUP_KEY_TYPES[entity]()

        for name in get_registration(entity).groupable:
            assert getattr(key, name) is None

    def test_every_entity_has_a_group_by_input_and_a_group_key(self):
        assert set(GROUP_BY_INPUT_TYPES) == set(ALL_ENTITIES)
        assert set(GROUP_KEY_TYPES) == set(ALL_ENTITIES)


class TestResolvingOneGroupBy:
    def test_a_scalar_dimension_resolves_to_its_orm_path(self):
        resolved = resolve_group_by(
            AggregatableEntity.CALENDAR_EVENT,
            CalendarEventGroupByInput(scalar=CalendarEventScalarGroupByField.CALENDAR_ID),
            SAO_PAULO,
        )

        assert resolved.key_field == "calendar_id"
        assert resolved.dimension.alias == "calendar_id"
        assert resolved.dimension.field_path == "calendar_fk_id"
        assert resolved.dimension.granularity is None
        assert resolved.dimension.tzinfo is None

    def test_a_temporal_dimension_carries_the_granularity_and_the_clock(self):
        resolved = resolve_group_by(
            AggregatableEntity.CALENDAR_EVENT,
            CalendarEventGroupByInput(
                temporal=CalendarEventTemporalGroupBy(
                    field=CalendarEventTemporalGroupByField.START_TIME,
                    granularity=TemporalGranularity.DAY,
                )
            ),
            SAO_PAULO,
        )

        assert resolved.key_field == "start_time"
        assert resolved.dimension.alias == "start_time_day"
        assert resolved.dimension.field_path == "start_time"
        assert resolved.dimension.granularity is TemporalGranularity.DAY
        assert resolved.dimension.tzinfo == SAO_PAULO

    def test_setting_neither_variant_is_rejected(self):
        with pytest.raises(InvalidAggregatePlanError) as excinfo:
            resolve_group_by(
                AggregatableEntity.CALENDAR_EVENT, CalendarEventGroupByInput(), SAO_PAULO
            )

        assert (
            str(excinfo.value) == "A group-by dimension must set exactly one of scalar and temporal"
        )

    def test_setting_both_variants_is_rejected(self):
        with pytest.raises(InvalidAggregatePlanError):
            resolve_group_by(
                AggregatableEntity.CALENDAR_EVENT,
                CalendarEventGroupByInput(
                    scalar=CalendarEventScalarGroupByField.CALENDAR_ID,
                    temporal=CalendarEventTemporalGroupBy(
                        field=CalendarEventTemporalGroupByField.START_TIME,
                        granularity=TemporalGranularity.DAY,
                    ),
                ),
                SAO_PAULO,
            )

    def test_a_calendar_pool_group_by_has_only_the_temporal_variant(self):
        resolved = resolve_group_by(
            AggregatableEntity.CALENDAR_POOL,
            CalendarPoolGroupByInput(
                temporal=CalendarPoolTemporalGroupBy(
                    field=CalendarPoolTemporalGroupByField.CREATED,
                    granularity=TemporalGranularity.MONTH,
                )
            ),
            SAO_PAULO,
        )

        assert resolved.key_field == "created"
        assert resolved.dimension.alias == "created_month"
        assert not hasattr(CalendarPoolGroupByInput(), "scalar")


class TestResolvingAList:
    def test_dimensions_keep_the_order_they_were_given(self):
        resolved = resolve_group_by_inputs(
            AggregatableEntity.CALENDAR_EVENT,
            [
                CalendarEventGroupByInput(
                    temporal=CalendarEventTemporalGroupBy(
                        field=CalendarEventTemporalGroupByField.START_TIME,
                        granularity=TemporalGranularity.DAY,
                    )
                ),
                CalendarEventGroupByInput(scalar=CalendarEventScalarGroupByField.CALENDAR_ID),
            ],
            SAO_PAULO,
        )

        assert [one.key_field for one in resolved] == ["start_time", "calendar_id"]
        assert [one.dimension.alias for one in resolved] == ["start_time_day", "calendar_id"]

    def test_an_empty_group_by_is_rejected(self):
        with pytest.raises(InvalidAggregatePlanError) as excinfo:
            resolve_group_by_inputs(AggregatableEntity.CALENDAR_EVENT, [], SAO_PAULO)

        assert str(excinfo.value) == "An aggregate query must group by at least one dimension"

    def test_the_same_field_named_twice_is_rejected(self):
        with pytest.raises(InvalidAggregatePlanError):
            resolve_group_by_inputs(
                AggregatableEntity.CALENDAR_EVENT,
                [
                    CalendarEventGroupByInput(scalar=CalendarEventScalarGroupByField.CALENDAR_ID),
                    CalendarEventGroupByInput(scalar=CalendarEventScalarGroupByField.CALENDAR_ID),
                ],
                SAO_PAULO,
            )

    def test_one_field_at_two_granularities_is_rejected(self):
        """``start_time`` by day and by month would collide on one key field."""
        with pytest.raises(InvalidAggregatePlanError):
            resolve_group_by_inputs(
                AggregatableEntity.CALENDAR_EVENT,
                [
                    CalendarEventGroupByInput(
                        temporal=CalendarEventTemporalGroupBy(
                            field=CalendarEventTemporalGroupByField.START_TIME,
                            granularity=TemporalGranularity.DAY,
                        )
                    ),
                    CalendarEventGroupByInput(
                        temporal=CalendarEventTemporalGroupBy(
                            field=CalendarEventTemporalGroupByField.START_TIME,
                            granularity=TemporalGranularity.MONTH,
                        )
                    ),
                ],
                SAO_PAULO,
            )

    def test_an_entity_only_accepts_its_own_dimensions(self):
        """An appointment type cannot be grouped by a calendar event's field."""
        with pytest.raises(Exception, match="Unknown group-by field"):
            resolve_group_by_inputs(
                AggregatableEntity.APPOINTMENT_TYPE,
                [CalendarEventGroupByInput(scalar=CalendarEventScalarGroupByField.CALENDAR_ID)],
                SAO_PAULO,
            )


class TestBuildingAGroupKey:
    def test_only_the_requested_dimensions_are_populated(self):
        resolved = resolve_group_by_inputs(
            AggregatableEntity.CALENDAR_EVENT,
            [
                CalendarEventGroupByInput(scalar=CalendarEventScalarGroupByField.CALENDAR_ID),
                CalendarEventGroupByInput(
                    temporal=CalendarEventTemporalGroupBy(
                        field=CalendarEventTemporalGroupByField.START_TIME,
                        granularity=TemporalGranularity.DAY,
                    )
                ),
            ],
            SAO_PAULO,
        )
        bucket = datetime.datetime(2026, 3, 2, tzinfo=SAO_PAULO)

        key = build_group_key(
            AggregatableEntity.CALENDAR_EVENT,
            resolved,
            {"calendar_id": 7, "start_time_day": bucket, "count": 3},
        )

        assert key == CalendarEventGroupKey(calendar_id=7, start_time=bucket)
        assert key.appointment_type_id is None
        assert key.timezone is None
        assert key.end_time is None

    def test_a_key_over_a_single_dimension_leaves_the_rest_null(self):
        resolved = resolve_group_by_inputs(
            AggregatableEntity.APPOINTMENT_TYPE,
            [
                AppointmentTypeGroupByInput(
                    scalar=AppointmentTypeScalarGroupByField.ACCEPTS_PUBLIC_SCHEDULING
                )
            ],
            SAO_PAULO,
        )

        key = build_group_key(
            AggregatableEntity.APPOINTMENT_TYPE,
            resolved,
            {"accepts_public_scheduling": True, "count": 2},
        )

        assert key.accepts_public_scheduling is True
        assert key.created is None
