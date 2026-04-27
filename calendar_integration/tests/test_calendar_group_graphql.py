"""Tests for CalendarGroup GraphQL types, queries, and mutations."""

from datetime import timedelta
from unittest.mock import Mock, patch

from django.utils import timezone

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AvailableTime,
    Calendar,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
)
from calendar_integration.mutations import (
    CalendarGroupEventInput,
    CalendarGroupInput,
    CalendarGroupMutationDependencies,
    CalendarGroupMutations,
    CalendarGroupSlotInput,
    CalendarGroupSlotSelectionInput,
    DeleteCalendarGroupInput,
    UpdateCalendarGroupInput,
    get_calendar_group_mutation_dependencies,
)
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.calendar_service import CalendarService
from organizations.models import Organization
from public_api.queries import DateTimeRangeInput, Query, QueryDependencies


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Clinic Org", should_sync_rooms=False)


@pytest.fixture
def internal_calendars(organization):
    calendars = {}
    for name, external in (
        ("Dr. A", "phys_a"),
        ("Dr. B", "phys_b"),
        ("Room 1", "room_1"),
    ):
        calendars[external] = Calendar.objects.create(
            organization=organization,
            name=name,
            external_id=external,
            provider=CalendarProvider.INTERNAL,
            calendar_type=(
                CalendarType.PERSONAL if external.startswith("phys_") else CalendarType.RESOURCE
            ),
            manage_available_windows=True,
            accepts_public_scheduling=True,
        )
    return calendars


@pytest.fixture
def clinic_group(organization, internal_calendars):
    group = CalendarGroup.objects.create(organization=organization, name="Clinic")
    physicians = CalendarGroupSlot.objects.create(
        organization=organization, group=group, name="Physicians", order=0
    )
    rooms = CalendarGroupSlot.objects.create(
        organization=organization, group=group, name="Rooms", order=1
    )
    for cal in (internal_calendars["phys_a"], internal_calendars["phys_b"]):
        CalendarGroupSlotMembership.objects.create(
            organization=organization, slot=physicians, calendar=cal
        )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=rooms, calendar=internal_calendars["room_1"]
    )
    return group


def _mock_info_with_org(organization):
    info = Mock()
    info.context = Mock()
    info.context.request = Mock()
    info.context.request.public_api_organization = organization
    info.context.request.public_api_system_user = Mock()
    return info


# ---------------------------------------------------------------------------
# Query tests
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_calendar_group_query_returns_scoped_group(organization, clinic_group):
    info = _mock_info_with_org(organization)
    result = Query().calendar_group(info=info, group_id=clinic_group.id)
    assert result is not None
    assert result.id == clinic_group.id
    assert result.name == "Clinic"


@pytest.mark.django_db
def test_calendar_group_query_returns_none_for_other_org(organization):
    other_org = Organization.objects.create(name="Other", should_sync_rooms=False)
    other_group = CalendarGroup.objects.create(organization=other_org, name="Other")
    info = _mock_info_with_org(organization)
    result = Query().calendar_group(info=info, group_id=other_group.id)
    assert result is None


@pytest.mark.django_db
def test_calendar_groups_query_lists_org_scoped(organization, clinic_group):
    other_org = Organization.objects.create(name="Other", should_sync_rooms=False)
    CalendarGroup.objects.create(organization=other_org, name="Other")
    info = _mock_info_with_org(organization)
    results = Query().calendar_groups(info=info)
    assert [g.id for g in results] == [clinic_group.id]


@pytest.mark.django_db
def test_calendar_group_availability_query(organization, clinic_group, internal_calendars):
    now = timezone.now().replace(microsecond=0)
    start = now + timedelta(hours=1)
    end = start + timedelta(hours=1)
    for cal in internal_calendars.values():
        AvailableTime.objects.create(
            organization=organization,
            calendar=cal,
            start_time_tz_unaware=start,
            end_time_tz_unaware=end,
            timezone="UTC",
        )
    info = _mock_info_with_org(organization)
    deps = QueryDependencies(
        calendar_service=Mock(),
        calendar_group_service=CalendarGroupService(),
    )
    with patch("public_api.queries.get_query_dependencies", return_value=deps):
        ranges = [DateTimeRangeInput(start_time=start, end_time=end)]
        result = Query().calendar_group_availability(
            info=info, group_id=clinic_group.id, ranges=ranges
        )
    assert len(result) == 1
    by_slot_name = {s.id: s.name for s in clinic_group.slots.all()}
    slot_availability = {by_slot_name[s.slot_id]: s for s in result[0].slots}
    assert set(slot_availability["Physicians"].available_calendar_ids) == {
        internal_calendars["phys_a"].id,
        internal_calendars["phys_b"].id,
    }
    assert slot_availability["Rooms"].available_calendar_ids == [internal_calendars["room_1"].id]


@pytest.mark.django_db
def test_calendar_group_bookable_slots_query(organization, clinic_group, internal_calendars):
    now = timezone.now().replace(microsecond=0)
    start = now + timedelta(hours=1)
    end = start + timedelta(hours=1)
    for cal in internal_calendars.values():
        AvailableTime.objects.create(
            organization=organization,
            calendar=cal,
            start_time_tz_unaware=start,
            end_time_tz_unaware=end,
            timezone="UTC",
        )
    info = _mock_info_with_org(organization)
    deps = QueryDependencies(
        calendar_service=Mock(),
        calendar_group_service=CalendarGroupService(),
    )
    with patch("public_api.queries.get_query_dependencies", return_value=deps):
        proposals = Query().calendar_group_bookable_slots(
            info=info,
            group_id=clinic_group.id,
            search_window_start=start,
            search_window_end=end,
            duration_seconds=60 * 60,
            slot_step_seconds=60 * 60,
        )
    assert [(p.start_time, p.end_time) for p in proposals] == [(start, end)]


@pytest.mark.django_db
def test_calendar_group_events_query(organization, clinic_group, internal_calendars):
    now = timezone.now().replace(microsecond=0)
    start = now + timedelta(hours=1)
    end = start + timedelta(hours=1)
    # A non-group event (should not show up).
    baker.make(
        "calendar_integration.CalendarEvent",
        organization=organization,
        calendar_fk=internal_calendars["phys_a"],
        title="Standalone",
        external_id="ev_standalone",
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
    )
    # A group event.
    group_event = baker.make(
        "calendar_integration.CalendarEvent",
        organization=organization,
        calendar_fk=internal_calendars["phys_a"],
        calendar_group_fk=clinic_group,
        title="Group event",
        external_id="ev_grouped",
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
    )
    info = _mock_info_with_org(organization)
    deps = QueryDependencies(
        calendar_service=Mock(),
        calendar_group_service=CalendarGroupService(),
    )
    with patch("public_api.queries.get_query_dependencies", return_value=deps):
        events = Query().calendar_group_events(
            info=info,
            group_id=clinic_group.id,
            start_datetime=start,
            end_datetime=end + timedelta(hours=1),
        )
    assert [e.id for e in events] == [group_event.id]


# ---------------------------------------------------------------------------
# Mutation tests
# ---------------------------------------------------------------------------
def _mock_mutation_deps():
    cs = CalendarService()
    gs = CalendarGroupService(calendar_service=cs)
    return CalendarGroupMutationDependencies(calendar_group_service=gs, calendar_service=cs)


@pytest.mark.django_db
def test_create_calendar_group_mutation(organization, internal_calendars):
    mutations = CalendarGroupMutations()
    input_data = CalendarGroupInput(
        organization_id=organization.id,
        name="Clinic",
        description="Docs",
        slots=[
            CalendarGroupSlotInput(
                name="Physicians",
                calendar_ids=[
                    internal_calendars["phys_a"].id,
                    internal_calendars["phys_b"].id,
                ],
            ),
            CalendarGroupSlotInput(
                name="Rooms",
                calendar_ids=[internal_calendars["room_1"].id],
                order=1,
            ),
        ],
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_calendar_group_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.create_calendar_group(input=input_data)
    assert result.success is True
    assert result.group is not None
    assert result.group.name == "Clinic"
    assert CalendarGroup.objects.filter_by_organization(organization.id).count() == 1


@pytest.mark.django_db
def test_create_calendar_group_mutation_rejects_unknown_org():
    mutations = CalendarGroupMutations()
    input_data = CalendarGroupInput(organization_id=99_999, name="Lost", slots=[])
    result = mutations.create_calendar_group(input=input_data)
    assert result.success is False
    assert "Organization not found" in (result.error_message or "")


@pytest.mark.django_db
def test_create_calendar_group_mutation_surfaces_validation_error(organization, internal_calendars):
    # Duplicate calendar in slot → validation error surfaced on the result, not raised.
    mutations = CalendarGroupMutations()
    input_data = CalendarGroupInput(
        organization_id=organization.id,
        name="Bad",
        slots=[
            CalendarGroupSlotInput(
                name="Physicians",
                calendar_ids=[
                    internal_calendars["phys_a"].id,
                    internal_calendars["phys_a"].id,
                ],
            )
        ],
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_calendar_group_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.create_calendar_group(input=input_data)
    assert result.success is False
    assert "duplicate" in (result.error_message or "").lower()


@pytest.mark.django_db
def test_update_calendar_group_mutation(organization, clinic_group, internal_calendars):
    mutations = CalendarGroupMutations()
    input_data = UpdateCalendarGroupInput(
        organization_id=organization.id,
        group_id=clinic_group.id,
        name="Clinic renamed",
        description="Updated",
        slots=[
            CalendarGroupSlotInput(
                name="Physicians",
                calendar_ids=[internal_calendars["phys_a"].id],
                order=0,
            ),
            CalendarGroupSlotInput(
                name="Rooms",
                calendar_ids=[internal_calendars["room_1"].id],
                order=1,
            ),
        ],
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_calendar_group_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.update_calendar_group(input=input_data)
    assert result.success is True
    clinic_group.refresh_from_db()
    assert clinic_group.name == "Clinic renamed"
    physicians = clinic_group.slots.get(name="Physicians")
    assert set(physicians.calendars.values_list("external_id", flat=True)) == {"phys_a"}


@pytest.mark.django_db
def test_update_calendar_group_mutation_missing_group(organization):
    mutations = CalendarGroupMutations()
    input_data = UpdateCalendarGroupInput(
        organization_id=organization.id,
        group_id=99_999,
        name="Nope",
        slots=[],
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_calendar_group_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.update_calendar_group(input=input_data)
    assert result.success is False
    assert "not found" in (result.error_message or "").lower()


@pytest.mark.django_db
def test_delete_calendar_group_mutation(organization, clinic_group):
    mutations = CalendarGroupMutations()
    input_data = DeleteCalendarGroupInput(organization_id=organization.id, group_id=clinic_group.id)
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_calendar_group_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.delete_calendar_group(input=input_data)
    assert result.success is True
    assert (
        not CalendarGroup.objects.filter_by_organization(organization.id)
        .filter(id=clinic_group.id)
        .exists()
    )


@pytest.mark.django_db
def test_create_calendar_group_event_mutation(organization, clinic_group, internal_calendars):
    now = timezone.now().replace(microsecond=0)
    start = now + timedelta(hours=1)
    end = start + timedelta(hours=1)
    for cal in internal_calendars.values():
        AvailableTime.objects.create(
            organization=organization,
            calendar=cal,
            start_time_tz_unaware=start,
            end_time_tz_unaware=end,
            timezone="UTC",
        )
    physicians = clinic_group.slots.get(name="Physicians")
    rooms = clinic_group.slots.get(name="Rooms")

    mutations = CalendarGroupMutations()
    input_data = CalendarGroupEventInput(
        organization_id=organization.id,
        group_id=clinic_group.id,
        title="Follow-up",
        description="",
        start_time=start,
        end_time=end,
        timezone="UTC",
        slot_selections=[
            CalendarGroupSlotSelectionInput(
                slot_id=physicians.id, calendar_ids=[internal_calendars["phys_a"].id]
            ),
            CalendarGroupSlotSelectionInput(
                slot_id=rooms.id, calendar_ids=[internal_calendars["room_1"].id]
            ),
        ],
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_calendar_group_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.create_calendar_group_event(input=input_data)
    assert result.success is True
    assert result.event is not None
    assert result.event.calendar_fk_id == internal_calendars["phys_a"].id
    assert result.event.calendar_group_fk_id == clinic_group.id


@pytest.mark.django_db
def test_create_calendar_group_event_mutation_surfaces_validation_error(
    organization, clinic_group, internal_calendars
):
    # No availability, so the selection is unavailable — expect a validation error.
    now = timezone.now().replace(microsecond=0)
    start = now + timedelta(hours=1)
    end = start + timedelta(hours=1)
    physicians = clinic_group.slots.get(name="Physicians")
    rooms = clinic_group.slots.get(name="Rooms")

    mutations = CalendarGroupMutations()
    input_data = CalendarGroupEventInput(
        organization_id=organization.id,
        group_id=clinic_group.id,
        title="Will fail",
        description="",
        start_time=start,
        end_time=end,
        timezone="UTC",
        slot_selections=[
            CalendarGroupSlotSelectionInput(
                slot_id=physicians.id, calendar_ids=[internal_calendars["phys_a"].id]
            ),
            CalendarGroupSlotSelectionInput(
                slot_id=rooms.id, calendar_ids=[internal_calendars["room_1"].id]
            ),
        ],
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_calendar_group_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.create_calendar_group_event(input=input_data)
    assert result.success is False
    assert "not available" in (result.error_message or "").lower()


# ---------------------------------------------------------------------------
# Dependency-factory tests
# ---------------------------------------------------------------------------
def test_get_calendar_group_mutation_dependencies_missing_raises():
    from graphql import GraphQLError

    with pytest.raises(GraphQLError, match="Missing required dependency"):
        get_calendar_group_mutation_dependencies(calendar_group_service=None, calendar_service=None)


# ---------------------------------------------------------------------------
# Schema-level smoke test
# ---------------------------------------------------------------------------
def test_schema_exposes_calendar_group_operations():
    from public_api.schema import schema

    sdl = schema.as_str()
    for expected in (
        "type CalendarGroupGraphQLType",
        "type CalendarGroupSlotGraphQLType",
        "type CalendarGroupRangeAvailabilityGraphQLType",
        "type BookableSlotProposalGraphQLType",
        "input CalendarGroupInput",
        "input UpdateCalendarGroupInput",
        "input DeleteCalendarGroupInput",
        "input CalendarGroupEventInput",
        "createCalendarGroup(",
        "updateCalendarGroup(",
        "deleteCalendarGroup(",
        "createCalendarGroupEvent(",
        "calendarGroup(",
        "calendarGroups(",
        "calendarGroupAvailability(",
        "calendarGroupBookableSlots(",
    ):
        assert expected in sdl, f"missing {expected!r} in schema SDL"
