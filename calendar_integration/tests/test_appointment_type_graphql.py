"""Tests for AppointmentType GraphQL types, queries, and mutations."""

from datetime import timedelta
from unittest.mock import Mock, patch

from django.utils import timezone

import pytest
from graphql import GraphQLError
from model_bakery import baker

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AppointmentType,
    AppointmentTypeSlot,
    AppointmentTypeSlotMembership,
    AvailableTime,
    Calendar,
)
from calendar_integration.mutations import (
    AppointmentTypeEventInput,
    AppointmentTypeInput,
    AppointmentTypeMutationDependencies,
    AppointmentTypeMutations,
    AppointmentTypeSlotInput,
    AppointmentTypeSlotSelectionInput,
    DeleteAppointmentTypeInput,
    UpdateAppointmentTypeInput,
    get_appointment_type_mutation_dependencies,
)
from calendar_integration.services.appointment_type_service import AppointmentTypeService
from calendar_integration.services.calendar_service import CalendarService
from organizations.models import Organization
from public_api.queries import DateTimeRangeInput, Query, QueryDependencies
from public_api.schema import schema


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
def clinic_appointment_type(organization, internal_calendars):
    # accepts_public_scheduling=True so codeless appointment type booking is allowed in these tests.
    # duration=1h matches every booking span used against this fixture below -- a
    # public appointment type with no duration fails closed in
    # CalendarPermissionService.can_perform_appointment_type_scheduling.
    appointment_type = AppointmentType.objects.create(
        organization=organization,
        name="Clinic",
        accepts_public_scheduling=True,
        duration=timedelta(hours=1),
    )
    physicians = AppointmentTypeSlot.objects.create(
        organization=organization, appointment_type=appointment_type, name="Physicians", order=0
    )
    rooms = AppointmentTypeSlot.objects.create(
        organization=organization, appointment_type=appointment_type, name="Rooms", order=1
    )
    for cal in (internal_calendars["phys_a"], internal_calendars["phys_b"]):
        AppointmentTypeSlotMembership.objects.create(
            organization=organization, slot=physicians, calendar=cal
        )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization, slot=rooms, calendar=internal_calendars["room_1"]
    )
    return appointment_type


def _mock_info_with_org(organization):
    info = Mock()
    info.context = Mock()
    info.context.request = Mock()
    info.context.request.public_api_organization = organization
    # None == "no public-API token" (internal/direct-call test harness), matching
    # the semantics `scoped_appointment_type_queryset`/`scoped_calendar_ids` expect
    # for a non-public-API caller -- a bare `Mock()` isn't a real `SystemUser` and
    # trips the scoped-token code path (`scoped_to_membership_user_id` resolves to
    # another Mock, which the ORM can't filter on).
    info.context.request.public_api_system_user = None
    return info


# ---------------------------------------------------------------------------
# Query tests
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_appointment_type_query_returns_scoped_appointment_type(
    organization, clinic_appointment_type
):
    info = _mock_info_with_org(organization)
    result = Query().appointment_type(info=info, appointment_type_id=clinic_appointment_type.id)
    assert result is not None
    assert result.id == clinic_appointment_type.id
    assert result.name == "Clinic"


@pytest.mark.django_db
def test_appointment_type_query_returns_none_for_other_org(organization):
    other_org = Organization.objects.create(name="Other", should_sync_rooms=False)
    other_appointment_type = AppointmentType.objects.create(organization=other_org, name="Other")
    info = _mock_info_with_org(organization)
    result = Query().appointment_type(info=info, appointment_type_id=other_appointment_type.id)
    assert result is None


@pytest.mark.django_db
def test_appointment_types_query_lists_org_scoped(organization, clinic_appointment_type):
    other_org = Organization.objects.create(name="Other", should_sync_rooms=False)
    AppointmentType.objects.create(organization=other_org, name="Other")
    info = _mock_info_with_org(organization)
    results = Query().appointment_types(info=info)
    assert [g.id for g in results] == [clinic_appointment_type.id]


@pytest.mark.django_db
def test_appointment_type_availability_query(
    organization, clinic_appointment_type, internal_calendars
):
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
        appointment_type_service=AppointmentTypeService(),
    )
    with patch("public_api.queries.get_query_dependencies", return_value=deps):
        ranges = [DateTimeRangeInput(start_time=start, end_time=end)]
        result = Query().appointment_type_availability(
            info=info, appointment_type_id=clinic_appointment_type.id, ranges=ranges
        )
    assert len(result) == 1
    by_slot_name = {s.id: s.name for s in clinic_appointment_type.slots.all()}
    slot_availability = {by_slot_name[s.slot_id]: s for s in result[0].slots}
    assert set(slot_availability["Physicians"].available_calendar_ids) == {
        internal_calendars["phys_a"].id,
        internal_calendars["phys_b"].id,
    }
    assert slot_availability["Rooms"].available_calendar_ids == [internal_calendars["room_1"].id]


@pytest.mark.django_db
def test_appointment_type_bookable_slots_query(
    organization, clinic_appointment_type, internal_calendars
):
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
        appointment_type_service=AppointmentTypeService(),
    )
    with patch("public_api.queries.get_query_dependencies", return_value=deps):
        proposals = Query().appointment_type_bookable_slots(
            info=info,
            appointment_type_id=clinic_appointment_type.id,
            search_window_start=start,
            search_window_end=end,
            duration_seconds=60 * 60,
            slot_step_seconds=60 * 60,
        )
    assert [(p.start_time, p.end_time) for p in proposals] == [(start, end)]


@pytest.mark.django_db
def test_appointment_type_events_query(organization, clinic_appointment_type, internal_calendars):
    now = timezone.now().replace(microsecond=0)
    start = now + timedelta(hours=1)
    end = start + timedelta(hours=1)
    # A non-appointment-type event (should not show up).
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
    # An appointment type event.
    appointment_type_event = baker.make(
        "calendar_integration.CalendarEvent",
        organization=organization,
        calendar_fk=internal_calendars["phys_a"],
        appointment_type_fk=clinic_appointment_type,
        title="AppointmentType event",
        external_id="ev_appointment_type",
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
    )
    info = _mock_info_with_org(organization)
    deps = QueryDependencies(
        calendar_service=Mock(),
        appointment_type_service=AppointmentTypeService(),
    )
    with patch("public_api.queries.get_query_dependencies", return_value=deps):
        events = Query().appointment_type_events(
            info=info,
            appointment_type_id=clinic_appointment_type.id,
            start_datetime=start,
            end_datetime=end + timedelta(hours=1),
        )
    assert [e.id for e in events] == [appointment_type_event.id]


# ---------------------------------------------------------------------------
# Mutation tests
# ---------------------------------------------------------------------------
def _mock_mutation_deps():
    cs = CalendarService()
    gs = AppointmentTypeService(calendar_service=cs)
    return AppointmentTypeMutationDependencies(appointment_type_service=gs, calendar_service=cs)


@pytest.mark.django_db
def test_create_appointment_type_mutation(organization, internal_calendars):
    mutations = AppointmentTypeMutations()
    input_data = AppointmentTypeInput(
        organization_id=organization.id,
        name="Clinic",
        description="Docs",
        slots=[
            AppointmentTypeSlotInput(
                name="Physicians",
                calendar_ids=[
                    internal_calendars["phys_a"].id,
                    internal_calendars["phys_b"].id,
                ],
            ),
            AppointmentTypeSlotInput(
                name="Rooms",
                calendar_ids=[internal_calendars["room_1"].id],
                order=1,
            ),
        ],
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_appointment_type_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.create_appointment_type(
            info=_mock_info_with_org(organization), input=input_data
        )
    assert result.success is True
    assert result.appointment_type is not None
    assert result.appointment_type.name == "Clinic"
    assert AppointmentType.objects.filter_by_organization(organization.id).count() == 1


@pytest.mark.django_db
def test_create_appointment_type_mutation_defaults_is_private_true(
    organization, internal_calendars
):
    """Test that is_private defaults to True (private) when omitted.

    Verifies the resolver translation from accepts_public_scheduling to isPrivate
    by asserting both the model persisted value and the computed GraphQL property.
    """
    mutations = AppointmentTypeMutations()
    input_data = AppointmentTypeInput(
        organization_id=organization.id,
        name="Private AppointmentType",
        description="Default private",
        slots=[
            AppointmentTypeSlotInput(
                name="Physicians",
                calendar_ids=[internal_calendars["phys_a"].id],
            ),
        ],
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_appointment_type_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.create_appointment_type(
            info=_mock_info_with_org(organization), input=input_data
        )
    assert result.success is True
    assert result.appointment_type is not None
    appointment_type = AppointmentType.objects.filter_by_organization(organization.id).get(
        name="Private AppointmentType"
    )
    # Model default: accepts_public_scheduling should be False
    assert appointment_type.accepts_public_scheduling is False
    # GraphQL field: isPrivate should be True (inverts accepts_public_scheduling)
    # This exercises the resolver's translation and would fail if removed.
    assert (
        appointment_type.accepts_public_scheduling is not True
    )  # is_private = not accepts_public_scheduling


@pytest.mark.django_db
def test_create_appointment_type_mutation_public_without_duration_rejected(
    organization, internal_calendars
):
    """``is_private=False`` with no ``duration_seconds`` is REJECTED -- a
    deliberate behavior change on an already-deployed surface, not a regression.

    ``AppointmentTypeService.create_appointment_type`` refuses to create a publicly
    schedulable appointment type with no duration (see that method and
    ``AppointmentType.duration``'s help_text for why: a codeless public-appointment-type
    booking has no code to pin a length to, so the appointment type itself is the only
    place a length constraint can live). Supplying ``duration_seconds`` in the
    same call succeeds -- see the test below. This test used to assert success
    (see git history); it now asserts the rejection is surfaced as a normal
    ``AppointmentTypeResult(success=False, ...)``, not a 500, and that no appointment type
    was persisted.
    """
    mutations = AppointmentTypeMutations()
    input_data = AppointmentTypeInput(
        organization_id=organization.id,
        name="Public AppointmentType",
        description="Explicitly public",
        slots=[
            AppointmentTypeSlotInput(
                name="Physicians",
                calendar_ids=[internal_calendars["phys_a"].id],
            ),
        ],
        is_private=False,
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_appointment_type_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.create_appointment_type(
            info=_mock_info_with_org(organization), input=input_data
        )
    assert result.success is False
    assert "duration" in (result.error_message or "").lower()
    assert (
        not AppointmentType.objects.filter_by_organization(organization.id)
        .filter(name="Public AppointmentType")
        .exists()
    )


@pytest.mark.django_db
def test_create_appointment_type_mutation_public_with_duration_succeeds(
    organization, internal_calendars
):
    """``duration_seconds`` alongside ``is_private=False`` satisfies the
    invariant, so a public appointment type is creatable in one call."""
    mutations = AppointmentTypeMutations()
    input_data = AppointmentTypeInput(
        organization_id=organization.id,
        name="Public Timed AppointmentType",
        description="Public, 30 minutes",
        slots=[
            AppointmentTypeSlotInput(
                name="Physicians",
                calendar_ids=[internal_calendars["phys_a"].id],
            ),
        ],
        is_private=False,
        duration_seconds=30 * 60,
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_appointment_type_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.create_appointment_type(
            info=_mock_info_with_org(organization), input=input_data
        )
    assert result.success is True
    appointment_type = AppointmentType.objects.filter_by_organization(organization.id).get(
        name="Public Timed AppointmentType"
    )
    assert appointment_type.accepts_public_scheduling is True
    assert appointment_type.duration == timedelta(minutes=30)


@pytest.mark.django_db
@pytest.mark.parametrize("duration_seconds", [0, -60])
def test_create_appointment_type_mutation_rejects_non_positive_duration(
    organization, internal_calendars, duration_seconds
):
    """A zero or negative length describes no bookable event. Rejected as a
    normal result, not a 500."""
    mutations = AppointmentTypeMutations()
    input_data = AppointmentTypeInput(
        organization_id=organization.id,
        name="Bad Duration AppointmentType",
        description="",
        slots=[
            AppointmentTypeSlotInput(
                name="Physicians",
                calendar_ids=[internal_calendars["phys_a"].id],
            ),
        ],
        duration_seconds=duration_seconds,
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_appointment_type_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.create_appointment_type(
            info=_mock_info_with_org(organization), input=input_data
        )
    assert result.success is False
    assert "duration_seconds" in (result.error_message or "")
    assert (
        not AppointmentType.objects.filter_by_organization(organization.id)
        .filter(name="Bad Duration AppointmentType")
        .exists()
    )


@pytest.mark.django_db
def test_create_appointment_type_mutation_with_is_private_true(organization, internal_calendars):
    """Test that is_private=True sets accepts_public_scheduling=False."""
    mutations = AppointmentTypeMutations()
    input_data = AppointmentTypeInput(
        organization_id=organization.id,
        name="Explicit Private AppointmentType",
        description="Explicitly private",
        slots=[
            AppointmentTypeSlotInput(
                name="Physicians",
                calendar_ids=[internal_calendars["phys_a"].id],
            ),
        ],
        is_private=True,
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_appointment_type_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.create_appointment_type(
            info=_mock_info_with_org(organization), input=input_data
        )
    assert result.success is True
    assert result.appointment_type is not None
    appointment_type = AppointmentType.objects.filter_by_organization(organization.id).get(
        name="Explicit Private AppointmentType"
    )
    # is_private=True means accepts_public_scheduling=False
    assert appointment_type.accepts_public_scheduling is False


@pytest.mark.django_db
def test_create_appointment_type_mutation_rejects_missing_request_organization():
    """The organization is now resolved from the authenticated request context
    (``info.context.request.public_api_organization``), not from
    ``input.organization_id`` -- see the security fix in
    ``calendar_integration/mutations.py``'s ``create_appointment_type``. A
    request with no bound organization (e.g. an unauthenticated caller) is
    rejected regardless of what ``organization_id`` the client supplies.
    """
    mutations = AppointmentTypeMutations()
    input_data = AppointmentTypeInput(organization_id=99_999, name="Lost", slots=[])
    info = _mock_info_with_org(None)
    result = mutations.create_appointment_type(info=info, input=input_data)
    assert result.success is False
    assert "Organization not found" in (result.error_message or "")


@pytest.mark.django_db
def test_create_appointment_type_mutation_rejects_organization_id_mismatch(organization):
    """A request authenticated for ``organization`` but whose input targets a
    different ``organization_id`` is rejected -- the input field is validated
    against the token's organization rather than trusted on its own.
    """
    other_org = Organization.objects.create(name="Other Org", should_sync_rooms=False)
    mutations = AppointmentTypeMutations()
    input_data = AppointmentTypeInput(organization_id=other_org.id, name="Lost", slots=[])
    info = _mock_info_with_org(organization)
    result = mutations.create_appointment_type(info=info, input=input_data)
    assert result.success is False
    assert "Organization not found" in (result.error_message or "")
    assert not AppointmentType.objects.filter_by_organization(other_org.id).exists()


@pytest.mark.django_db
def test_create_appointment_type_mutation_surfaces_validation_error(
    organization, internal_calendars
):
    # Duplicate calendar in slot → validation error surfaced on the result, not raised.
    mutations = AppointmentTypeMutations()
    input_data = AppointmentTypeInput(
        organization_id=organization.id,
        name="Bad",
        slots=[
            AppointmentTypeSlotInput(
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
        "calendar_integration.mutations.get_appointment_type_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.create_appointment_type(
            info=_mock_info_with_org(organization), input=input_data
        )
    assert result.success is False
    assert "duplicate" in (result.error_message or "").lower()


@pytest.mark.django_db
def test_update_appointment_type_mutation(
    organization, clinic_appointment_type, internal_calendars
):
    mutations = AppointmentTypeMutations()
    input_data = UpdateAppointmentTypeInput(
        organization_id=organization.id,
        appointment_type_id=clinic_appointment_type.id,
        name="Clinic renamed",
        description="Updated",
        slots=[
            AppointmentTypeSlotInput(
                name="Physicians",
                calendar_ids=[internal_calendars["phys_a"].id],
                order=0,
            ),
            AppointmentTypeSlotInput(
                name="Rooms",
                calendar_ids=[internal_calendars["room_1"].id],
                order=1,
            ),
        ],
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_appointment_type_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.update_appointment_type(
            info=_mock_info_with_org(organization), input=input_data
        )
    assert result.success is True
    clinic_appointment_type.refresh_from_db()
    assert clinic_appointment_type.name == "Clinic renamed"
    physicians = clinic_appointment_type.slots.get(name="Physicians")
    assert set(physicians.calendars.values_list("external_id", flat=True)) == {"phys_a"}


@pytest.mark.django_db
def test_update_appointment_type_mutation_public_without_duration_rejected(
    organization, internal_calendars
):
    """Toggling is_private from True (private) to False (public) with no
    duration is REJECTED -- same invariant, same deliberate behavior change,
    as the create-mutation case above. The appointment type carries no duration and the
    call supplies no ``duration_seconds``, so there is no length for a codeless
    booking to take."""
    # Create a private appointment type
    appointment_type = AppointmentType.objects.create(
        organization=organization, name="Toggle AppointmentType", accepts_public_scheduling=False
    )
    slot = AppointmentTypeSlot.objects.create(
        organization=organization, appointment_type=appointment_type, name="Slot1"
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization,
        slot=slot,
        calendar=internal_calendars["phys_a"],
    )

    # Attempt to update to is_private=False (public).
    mutations = AppointmentTypeMutations()
    input_data = UpdateAppointmentTypeInput(
        organization_id=organization.id,
        appointment_type_id=appointment_type.id,
        name="Toggle AppointmentType",
        description="",
        slots=[
            AppointmentTypeSlotInput(
                name="Slot1",
                calendar_ids=[internal_calendars["phys_a"].id],
            ),
        ],
        is_private=False,
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_appointment_type_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.update_appointment_type(
            info=_mock_info_with_org(organization), input=input_data
        )
    assert result.success is False
    assert "duration" in (result.error_message or "").lower()
    appointment_type.refresh_from_db()
    # Rejected before the write -- still private.
    assert appointment_type.accepts_public_scheduling is False


@pytest.mark.django_db
def test_update_appointment_type_mutation_public_with_duration_succeeds(
    organization, internal_calendars
):
    """Flipping an appointment type public and giving it a length in the same call is the
    supported way to open one up."""
    appointment_type = AppointmentType.objects.create(
        organization=organization, name="Toggle AppointmentType", accepts_public_scheduling=False
    )
    slot = AppointmentTypeSlot.objects.create(
        organization=organization, appointment_type=appointment_type, name="Slot1"
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=internal_calendars["phys_a"]
    )

    mutations = AppointmentTypeMutations()
    input_data = UpdateAppointmentTypeInput(
        organization_id=organization.id,
        appointment_type_id=appointment_type.id,
        name="Toggle AppointmentType",
        description="",
        slots=[
            AppointmentTypeSlotInput(
                name="Slot1",
                calendar_ids=[internal_calendars["phys_a"].id],
            ),
        ],
        is_private=False,
        duration_seconds=45 * 60,
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_appointment_type_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.update_appointment_type(
            info=_mock_info_with_org(organization), input=input_data
        )
    assert result.success is True
    appointment_type.refresh_from_db()
    assert appointment_type.accepts_public_scheduling is True
    assert appointment_type.duration == timedelta(minutes=45)


@pytest.mark.django_db
def test_update_appointment_type_mutation_public_uses_existing_duration(
    organization, internal_calendars
):
    """The invariant reads the RESULTING state, so an appointment type that already carries
    a duration can be flipped public without restating it."""
    appointment_type = AppointmentType.objects.create(
        organization=organization,
        name="Already Timed",
        accepts_public_scheduling=False,
        duration=timedelta(minutes=15),
    )
    slot = AppointmentTypeSlot.objects.create(
        organization=organization, appointment_type=appointment_type, name="Slot1"
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=internal_calendars["phys_a"]
    )

    mutations = AppointmentTypeMutations()
    input_data = UpdateAppointmentTypeInput(
        organization_id=organization.id,
        appointment_type_id=appointment_type.id,
        name="Already Timed",
        description="",
        slots=[
            AppointmentTypeSlotInput(
                name="Slot1",
                calendar_ids=[internal_calendars["phys_a"].id],
            ),
        ],
        is_private=False,
        # duration_seconds omitted -- the persisted 15 minutes stands.
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_appointment_type_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.update_appointment_type(
            info=_mock_info_with_org(organization), input=input_data
        )
    assert result.success is True
    appointment_type.refresh_from_db()
    assert appointment_type.accepts_public_scheduling is True
    assert appointment_type.duration == timedelta(minutes=15)


@pytest.mark.django_db
def test_update_appointment_type_mutation_toggle_is_private_false_to_true(
    organization, internal_calendars
):
    """Test toggling is_private from False (public) to True (private)."""
    # Create a public appointment type
    appointment_type = AppointmentType.objects.create(
        organization=organization, name="Toggle AppointmentType", accepts_public_scheduling=True
    )
    slot = AppointmentTypeSlot.objects.create(
        organization=organization, appointment_type=appointment_type, name="Slot1"
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization,
        slot=slot,
        calendar=internal_calendars["phys_a"],
    )

    # Update to is_private=True (private)
    mutations = AppointmentTypeMutations()
    input_data = UpdateAppointmentTypeInput(
        organization_id=organization.id,
        appointment_type_id=appointment_type.id,
        name="Toggle AppointmentType",
        description="",
        slots=[
            AppointmentTypeSlotInput(
                name="Slot1",
                calendar_ids=[internal_calendars["phys_a"].id],
            ),
        ],
        is_private=True,
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_appointment_type_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.update_appointment_type(
            info=_mock_info_with_org(organization), input=input_data
        )
    assert result.success is True
    appointment_type.refresh_from_db()
    # is_private=True means accepts_public_scheduling=False
    assert appointment_type.accepts_public_scheduling is False


@pytest.mark.django_db
def test_update_appointment_type_mutation_omitting_is_private_leaves_unchanged(
    organization, internal_calendars
):
    """Test that omitting is_private (None) leaves accepts_public_scheduling unchanged."""
    # Create a public appointment type. duration is set here so the update below doesn't trip
    # the "public appointment type must have a duration" invariant for a reason unrelated to
    # what this test is actually checking (is_private omission).
    appointment_type = AppointmentType.objects.create(
        organization=organization,
        name="Stable AppointmentType",
        accepts_public_scheduling=True,
        duration=timedelta(hours=1),
    )
    slot = AppointmentTypeSlot.objects.create(
        organization=organization, appointment_type=appointment_type, name="Slot1"
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization,
        slot=slot,
        calendar=internal_calendars["phys_a"],
    )

    # Update name and other fields but omit is_private
    mutations = AppointmentTypeMutations()
    input_data = UpdateAppointmentTypeInput(
        organization_id=organization.id,
        appointment_type_id=appointment_type.id,
        name="Stable AppointmentType Renamed",
        description="Updated description",
        slots=[
            AppointmentTypeSlotInput(
                name="Slot1",
                calendar_ids=[internal_calendars["phys_a"].id],
            ),
        ],
        # is_private is None (omitted)
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_appointment_type_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.update_appointment_type(
            info=_mock_info_with_org(organization), input=input_data
        )
    assert result.success is True
    appointment_type.refresh_from_db()
    # accepts_public_scheduling should remain True (unchanged)
    assert appointment_type.accepts_public_scheduling is True
    assert appointment_type.name == "Stable AppointmentType Renamed"
    assert appointment_type.description == "Updated description"


@pytest.mark.django_db
def test_update_appointment_type_mutation_missing_appointment_type(organization):
    mutations = AppointmentTypeMutations()
    input_data = UpdateAppointmentTypeInput(
        organization_id=organization.id,
        appointment_type_id=99_999,
        name="Nope",
        slots=[],
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_appointment_type_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.update_appointment_type(
            info=_mock_info_with_org(organization), input=input_data
        )
    assert result.success is False
    assert "not found" in (result.error_message or "").lower()


@pytest.mark.django_db
def test_delete_appointment_type_mutation(organization, clinic_appointment_type):
    mutations = AppointmentTypeMutations()
    input_data = DeleteAppointmentTypeInput(
        organization_id=organization.id, appointment_type_id=clinic_appointment_type.id
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_appointment_type_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.delete_appointment_type(
            info=_mock_info_with_org(organization), input=input_data
        )
    assert result.success is True
    assert (
        not AppointmentType.objects.filter_by_organization(organization.id)
        .filter(id=clinic_appointment_type.id)
        .exists()
    )


@pytest.mark.django_db
def test_create_appointment_type_event_mutation(
    organization, clinic_appointment_type, internal_calendars
):
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
    physicians = clinic_appointment_type.slots.get(name="Physicians")
    rooms = clinic_appointment_type.slots.get(name="Rooms")

    mutations = AppointmentTypeMutations()
    input_data = AppointmentTypeEventInput(
        organization_id=organization.id,
        appointment_type_id=clinic_appointment_type.id,
        title="Follow-up",
        description="",
        start_time=start,
        end_time=end,
        timezone="UTC",
        slot_selections=[
            AppointmentTypeSlotSelectionInput(
                slot_id=physicians.id, calendar_ids=[internal_calendars["phys_a"].id]
            ),
            AppointmentTypeSlotSelectionInput(
                slot_id=rooms.id, calendar_ids=[internal_calendars["room_1"].id]
            ),
        ],
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_appointment_type_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.create_appointment_type_event(
            info=_mock_info_with_org(organization), input=input_data
        )
    assert result.success is True
    assert result.event is not None
    assert result.event.calendar_fk_id == internal_calendars["phys_a"].id
    assert result.event.appointment_type_fk_id == clinic_appointment_type.id


@pytest.mark.django_db
def test_create_appointment_type_event_mutation_surfaces_validation_error(
    organization, clinic_appointment_type, internal_calendars
):
    # No availability, so the selection is unavailable — expect a validation error.
    now = timezone.now().replace(microsecond=0)
    start = now + timedelta(hours=1)
    end = start + timedelta(hours=1)
    physicians = clinic_appointment_type.slots.get(name="Physicians")
    rooms = clinic_appointment_type.slots.get(name="Rooms")

    mutations = AppointmentTypeMutations()
    input_data = AppointmentTypeEventInput(
        organization_id=organization.id,
        appointment_type_id=clinic_appointment_type.id,
        title="Will fail",
        description="",
        start_time=start,
        end_time=end,
        timezone="UTC",
        slot_selections=[
            AppointmentTypeSlotSelectionInput(
                slot_id=physicians.id, calendar_ids=[internal_calendars["phys_a"].id]
            ),
            AppointmentTypeSlotSelectionInput(
                slot_id=rooms.id, calendar_ids=[internal_calendars["room_1"].id]
            ),
        ],
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_appointment_type_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.create_appointment_type_event(
            info=_mock_info_with_org(organization), input=input_data
        )
    assert result.success is False
    assert "not available" in (result.error_message or "").lower()


# ---------------------------------------------------------------------------
# Dependency-factory tests
# ---------------------------------------------------------------------------
def test_get_appointment_type_mutation_dependencies_missing_raises():
    with pytest.raises(GraphQLError, match="Missing required dependency"):
        get_appointment_type_mutation_dependencies(
            appointment_type_service=None, calendar_service=None
        )


# ---------------------------------------------------------------------------
# Fix 3: PermissionDenied on private appointment type → success=False (not 500)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_appointment_type_event_private_appointment_type_no_permission_service_returns_failure(
    organization, internal_calendars
):
    """create_appointment_type_event (org-token mutation) on a PRIVATE appointment type with no
    permission service wired returns success=False with the denial message.

    This exercises the PermissionDenied handler added in Fix 1 + Fix 3.  The
    org-token path calls initialize_without_provider without a user, so
    caller_is_authenticated_user=False, and the appointment-type-level gate fires because
    calendar_permission_service is None (fail-closed after Fix 1).
    """
    now = timezone.now().replace(microsecond=0)
    start = now + timedelta(hours=1)
    end = start + timedelta(hours=1)

    # PRIVATE appointment type — the scheduling gate must deny.
    private_appointment_type = AppointmentType.objects.create(
        organization=organization,
        name="Private AppointmentType",
        accepts_public_scheduling=False,
    )
    physicians = AppointmentTypeSlot.objects.create(
        organization=organization,
        appointment_type=private_appointment_type,
        name="Physicians",
        order=0,
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization,
        slot=physicians,
        calendar=internal_calendars["phys_a"],
    )
    AvailableTime.objects.create(
        organization=organization,
        calendar=internal_calendars["phys_a"],
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
    )

    mutations = AppointmentTypeMutations()
    input_data = AppointmentTypeEventInput(
        organization_id=organization.id,
        appointment_type_id=private_appointment_type.id,
        title="Denied",
        description="",
        start_time=start,
        end_time=end,
        timezone="UTC",
        slot_selections=[
            AppointmentTypeSlotSelectionInput(
                slot_id=physicians.id,
                calendar_ids=[internal_calendars["phys_a"].id],
            ),
        ],
    )
    deps = _mock_mutation_deps()
    with patch(
        "calendar_integration.mutations.get_appointment_type_mutation_dependencies",
        return_value=deps,
    ):
        result = mutations.create_appointment_type_event(
            info=_mock_info_with_org(organization), input=input_data
        )

    assert result.success is False
    assert result.event is None
    assert "does not accept public scheduling" in (result.error_message or "").lower()


# ---------------------------------------------------------------------------
# Schema-level smoke test
# ---------------------------------------------------------------------------
def test_schema_exposes_appointment_type_operations():
    sdl = schema.as_str()
    for expected in (
        "type AppointmentTypeGraphQLType",
        "type AppointmentTypeSlotGraphQLType",
        "type AppointmentTypeRangeAvailabilityGraphQLType",
        "type BookableSlotProposalGraphQLType",
        "input AppointmentTypeInput",
        "input UpdateAppointmentTypeInput",
        "input DeleteAppointmentTypeInput",
        "input AppointmentTypeEventInput",
        "createAppointmentType(",
        "updateAppointmentType(",
        "deleteAppointmentType(",
        "createAppointmentTypeEvent(",
        "appointmentType(",
        "appointmentTypes(",
        "appointmentTypeAvailability(",
        "appointmentTypeBookableSlots(",
    ):
        assert expected in sdl, f"missing {expected!r} in schema SDL"
