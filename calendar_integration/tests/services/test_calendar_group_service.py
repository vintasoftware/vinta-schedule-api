from datetime import timedelta

from django.utils import timezone

import pytest

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.exceptions import (
    CalendarGroupHasFutureEventsError,
    CalendarGroupSlotInUseError,
    CalendarGroupValidationError,
    CalendarServiceOrganizationNotSetError,
)
from calendar_integration.models import (
    AvailableTime,
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroup,
    CalendarGroupSlotMembership,
)
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.dataclasses import (
    CalendarGroupInputData,
    CalendarGroupSlotInputData,
)
from organizations.models import Organization


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Clinic Org", should_sync_rooms=False)


@pytest.fixture
def other_organization(db):
    return Organization.objects.create(name="Other Org", should_sync_rooms=False)


@pytest.fixture
def service(organization):
    svc = CalendarGroupService()
    svc.initialize(organization=organization)
    return svc


@pytest.fixture
def managed_calendars(organization):
    calendars = {}
    for name, external in (
        ("Dr. A", "phys_a"),
        ("Dr. B", "phys_b"),
        ("Room 1", "room_1"),
        ("Room 2", "room_2"),
    ):
        calendars[external] = Calendar.objects.create(
            organization=organization,
            name=name,
            external_id=external,
            provider=CalendarProvider.GOOGLE,
            calendar_type=(
                CalendarType.PERSONAL if external.startswith("phys_") else CalendarType.RESOURCE
            ),
            manage_available_windows=True,
        )
    return calendars


@pytest.fixture
def base_input(managed_calendars):
    return CalendarGroupInputData(
        name="Clinic Appointments",
        description="",
        slots=[
            CalendarGroupSlotInputData(
                name="Physicians",
                calendar_ids=[
                    managed_calendars["phys_a"].id,
                    managed_calendars["phys_b"].id,
                ],
                required_count=1,
                order=0,
            ),
            CalendarGroupSlotInputData(
                name="Rooms",
                calendar_ids=[
                    managed_calendars["room_1"].id,
                    managed_calendars["room_2"].id,
                ],
                required_count=1,
                order=1,
            ),
        ],
    )


def _make_available_time(calendar, start, end):
    return AvailableTime.objects.create(
        organization=calendar.organization,
        calendar=calendar,
        start_time_tz_unaware=start,
        end_time_tz_unaware=end,
        timezone="UTC",
    )


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_requires_initialization():
    svc = CalendarGroupService()
    with pytest.raises(CalendarServiceOrganizationNotSetError):
        svc.create_group(CalendarGroupInputData(name="x"))


# ---------------------------------------------------------------------------
# create_group
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_create_group_happy_path(service, base_input, organization):
    group = service.create_group(base_input)

    assert group.name == "Clinic Appointments"
    assert group.organization_id == organization.id
    slots = list(group.slots.order_by("order"))
    assert [s.name for s in slots] == ["Physicians", "Rooms"]
    assert slots[0].required_count == 1
    assert set(slots[0].calendars.values_list("external_id", flat=True)) == {"phys_a", "phys_b"}
    assert set(slots[1].calendars.values_list("external_id", flat=True)) == {"room_1", "room_2"}


@pytest.mark.django_db
def test_create_group_rejects_empty_slot_pool(service):
    data = CalendarGroupInputData(
        name="Empty",
        slots=[CalendarGroupSlotInputData(name="Nobody", calendar_ids=[])],
    )
    with pytest.raises(CalendarGroupValidationError):
        service.create_group(data)


@pytest.mark.django_db
def test_create_group_rejects_cross_org_calendar(
    service, base_input, other_organization, managed_calendars
):
    foreign = Calendar.objects.create(
        organization=other_organization,
        name="Foreign",
        external_id="foreign",
        provider=CalendarProvider.GOOGLE,
    )
    base_input.slots[0].calendar_ids.append(foreign.id)
    with pytest.raises(CalendarGroupValidationError):
        service.create_group(base_input)


@pytest.mark.django_db
def test_create_group_rejects_duplicate_slot_names(service, managed_calendars):
    data = CalendarGroupInputData(
        name="Dupes",
        slots=[
            CalendarGroupSlotInputData(name="Slot", calendar_ids=[managed_calendars["phys_a"].id]),
            CalendarGroupSlotInputData(name="Slot", calendar_ids=[managed_calendars["phys_b"].id]),
        ],
    )
    with pytest.raises(CalendarGroupValidationError):
        service.create_group(data)


@pytest.mark.django_db
def test_create_group_rejects_duplicate_calendar_within_slot(service, managed_calendars):
    data = CalendarGroupInputData(
        name="Dupes",
        slots=[
            CalendarGroupSlotInputData(
                name="Slot",
                calendar_ids=[
                    managed_calendars["phys_a"].id,
                    managed_calendars["phys_a"].id,
                ],
            ),
        ],
    )
    with pytest.raises(CalendarGroupValidationError):
        service.create_group(data)


@pytest.mark.django_db
def test_create_group_rejects_required_count_exceeding_pool(service, managed_calendars):
    data = CalendarGroupInputData(
        name="Too many",
        slots=[
            CalendarGroupSlotInputData(
                name="Physicians",
                calendar_ids=[managed_calendars["phys_a"].id],
                required_count=2,
            ),
        ],
    )
    with pytest.raises(CalendarGroupValidationError):
        service.create_group(data)


@pytest.mark.django_db
def test_create_group_rejects_required_count_zero(service, managed_calendars):
    data = CalendarGroupInputData(
        name="Zero",
        slots=[
            CalendarGroupSlotInputData(
                name="Physicians",
                calendar_ids=[managed_calendars["phys_a"].id],
                required_count=0,
            ),
        ],
    )
    with pytest.raises(CalendarGroupValidationError):
        service.create_group(data)


@pytest.mark.django_db
def test_create_group_allows_calendar_shared_across_slots(service, managed_calendars):
    shared = managed_calendars["phys_a"]
    data = CalendarGroupInputData(
        name="Shared",
        slots=[
            CalendarGroupSlotInputData(name="A", calendar_ids=[shared.id]),
            CalendarGroupSlotInputData(name="B", calendar_ids=[shared.id]),
        ],
    )
    group = service.create_group(data)
    assert group.slots.count() == 2
    assert (
        CalendarGroupSlotMembership.objects.filter_by_organization(service.organization.id)
        .filter(calendar_fk=shared)
        .count()
        == 2
    )


# ---------------------------------------------------------------------------
# update_group
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_update_group_renames_group_and_updates_slot_fields(service, base_input):
    group = service.create_group(base_input)
    base_input.name = "Updated Clinic"
    base_input.description = "New description"
    base_input.slots[0].required_count = 2
    base_input.slots[0].order = 5

    updated = service.update_group(group.id, base_input)

    assert updated.name == "Updated Clinic"
    assert updated.description == "New description"
    physicians = updated.slots.get(name="Physicians")
    assert physicians.required_count == 2
    assert physicians.order == 5


@pytest.mark.django_db
def test_update_group_adds_and_removes_calendars_in_slot(service, base_input, managed_calendars):
    group = service.create_group(base_input)
    new_cal = Calendar.objects.create(
        organization=service.organization,
        name="Dr. C",
        external_id="phys_c",
        provider=CalendarProvider.GOOGLE,
    )
    base_input.slots[0].calendar_ids = [managed_calendars["phys_a"].id, new_cal.id]

    updated = service.update_group(group.id, base_input)

    physicians = updated.slots.get(name="Physicians")
    assert set(physicians.calendars.values_list("external_id", flat=True)) == {"phys_a", "phys_c"}


@pytest.mark.django_db
def test_update_group_creates_new_slot_and_removes_old(service, base_input, managed_calendars):
    group = service.create_group(base_input)
    base_input.slots = [
        CalendarGroupSlotInputData(
            name="Nurses",
            calendar_ids=[managed_calendars["phys_b"].id],
        ),
    ]

    updated = service.update_group(group.id, base_input)

    names = set(updated.slots.values_list("name", flat=True))
    assert names == {"Nurses"}


@pytest.mark.django_db
def test_update_group_refuses_evicting_calendar_with_future_booking(
    service, base_input, managed_calendars
):
    group = service.create_group(base_input)
    physicians = group.slots.get(name="Physicians")
    # Simulate a future-booked event with a group selection for phys_a
    event = CalendarEvent.objects.create(
        organization=service.organization,
        calendar_fk=managed_calendars["phys_a"],
        title="Future appointment",
        description="",
        external_id="ev_future",
        start_time_tz_unaware=timezone.now() + timedelta(days=2),
        end_time_tz_unaware=timezone.now() + timedelta(days=2, hours=1),
        timezone="UTC",
        calendar_group_fk=group,
    )
    CalendarEventGroupSelection.objects.create(
        organization=service.organization,
        event=event,
        slot=physicians,
        calendar=managed_calendars["phys_a"],
    )

    base_input.slots[0].calendar_ids = [managed_calendars["phys_b"].id]

    with pytest.raises(CalendarGroupSlotInUseError):
        service.update_group(group.id, base_input)


@pytest.mark.django_db
def test_update_group_refuses_removing_slot_with_future_booking(
    service, base_input, managed_calendars
):
    group = service.create_group(base_input)
    physicians = group.slots.get(name="Physicians")
    event = CalendarEvent.objects.create(
        organization=service.organization,
        calendar_fk=managed_calendars["phys_a"],
        title="Future appointment",
        description="",
        external_id="ev_future_2",
        start_time_tz_unaware=timezone.now() + timedelta(days=2),
        end_time_tz_unaware=timezone.now() + timedelta(days=2, hours=1),
        timezone="UTC",
        calendar_group_fk=group,
    )
    CalendarEventGroupSelection.objects.create(
        organization=service.organization,
        event=event,
        slot=physicians,
        calendar=managed_calendars["phys_a"],
    )

    # Remove the Physicians slot entirely
    base_input.slots = [
        CalendarGroupSlotInputData(
            name="Rooms",
            calendar_ids=[managed_calendars["room_1"].id],
        ),
    ]
    with pytest.raises(CalendarGroupSlotInUseError):
        service.update_group(group.id, base_input)


@pytest.mark.django_db
def test_update_group_allows_evicting_calendar_with_past_booking(
    service, base_input, managed_calendars
):
    group = service.create_group(base_input)
    physicians = group.slots.get(name="Physicians")
    past_event = CalendarEvent.objects.create(
        organization=service.organization,
        calendar_fk=managed_calendars["phys_a"],
        title="Old appointment",
        description="",
        external_id="ev_past",
        start_time_tz_unaware=timezone.now() - timedelta(days=2),
        end_time_tz_unaware=timezone.now() - timedelta(days=2) + timedelta(hours=1),
        timezone="UTC",
        calendar_group_fk=group,
    )
    CalendarEventGroupSelection.objects.create(
        organization=service.organization,
        event=past_event,
        slot=physicians,
        calendar=managed_calendars["phys_a"],
    )
    base_input.slots[0].calendar_ids = [managed_calendars["phys_b"].id]

    updated = service.update_group(group.id, base_input)
    physicians = updated.slots.get(name="Physicians")
    assert set(physicians.calendars.values_list("external_id", flat=True)) == {"phys_b"}


# ---------------------------------------------------------------------------
# delete_group
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_delete_group_without_events(service, base_input):
    group = service.create_group(base_input)
    service.delete_group(group.id)
    assert (
        not CalendarGroup.objects.filter_by_organization(service.organization.id)
        .filter(id=group.id)
        .exists()
    )


@pytest.mark.django_db
def test_delete_group_refused_with_future_events(service, base_input, managed_calendars):
    group = service.create_group(base_input)
    CalendarEvent.objects.create(
        organization=service.organization,
        calendar_fk=managed_calendars["phys_a"],
        title="Future",
        description="",
        external_id="ev_future_del",
        start_time_tz_unaware=timezone.now() + timedelta(days=1),
        end_time_tz_unaware=timezone.now() + timedelta(days=1, hours=1),
        timezone="UTC",
        calendar_group_fk=group,
    )
    with pytest.raises(CalendarGroupHasFutureEventsError):
        service.delete_group(group.id)


@pytest.mark.django_db
def test_delete_group_refused_with_past_events(service, base_input, managed_calendars):
    # The PROTECT FK on CalendarEvent.calendar_group blocks deletion regardless
    # of whether the event is in the past or the future; the service surfaces
    # this with a clearer error before hitting the DB-level ProtectedError.
    group = service.create_group(base_input)
    CalendarEvent.objects.create(
        organization=service.organization,
        calendar_fk=managed_calendars["phys_a"],
        title="Past",
        description="",
        external_id="ev_past_del",
        start_time_tz_unaware=timezone.now() - timedelta(days=2),
        end_time_tz_unaware=timezone.now() - timedelta(days=2) + timedelta(hours=1),
        timezone="UTC",
        calendar_group_fk=group,
    )
    with pytest.raises(CalendarGroupHasFutureEventsError):
        service.delete_group(group.id)


# ---------------------------------------------------------------------------
# get_group_events
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_get_group_events_filters_by_group_and_range(service, base_input, managed_calendars):
    group = service.create_group(base_input)
    other_group = service.create_group(
        CalendarGroupInputData(
            name="Other",
            slots=[
                CalendarGroupSlotInputData(
                    name="Rooms", calendar_ids=[managed_calendars["room_1"].id]
                ),
            ],
        )
    )

    now = timezone.now().replace(microsecond=0)

    in_range = CalendarEvent.objects.create(
        organization=service.organization,
        calendar_fk=managed_calendars["phys_a"],
        title="In range",
        description="",
        external_id="ev_in",
        start_time_tz_unaware=now + timedelta(hours=1),
        end_time_tz_unaware=now + timedelta(hours=2),
        timezone="UTC",
        calendar_group_fk=group,
    )
    CalendarEvent.objects.create(
        organization=service.organization,
        calendar_fk=managed_calendars["phys_a"],
        title="Out of range",
        description="",
        external_id="ev_out",
        start_time_tz_unaware=now + timedelta(days=5),
        end_time_tz_unaware=now + timedelta(days=5, hours=1),
        timezone="UTC",
        calendar_group_fk=group,
    )
    CalendarEvent.objects.create(
        organization=service.organization,
        calendar_fk=managed_calendars["room_1"],
        title="Other group",
        description="",
        external_id="ev_other",
        start_time_tz_unaware=now + timedelta(hours=1),
        end_time_tz_unaware=now + timedelta(hours=2),
        timezone="UTC",
        calendar_group_fk=other_group,
    )

    events = list(service.get_group_events(group.id, now, now + timedelta(days=1)))
    assert [e.id for e in events] == [in_range.id]


# ---------------------------------------------------------------------------
# check_group_availability
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_check_group_availability_per_slot_lists(service, base_input, managed_calendars):
    group = service.create_group(base_input)

    now = timezone.now().replace(microsecond=0)
    range1 = (now + timedelta(hours=1), now + timedelta(hours=2))
    range2 = (now + timedelta(hours=3), now + timedelta(hours=4))

    # All four have availability in range1; only phys_a + room_1 in range2
    for cal in managed_calendars.values():
        _make_available_time(cal, range1[0], range1[1])
    _make_available_time(managed_calendars["phys_a"], range2[0], range2[1])
    _make_available_time(managed_calendars["room_1"], range2[0], range2[1])

    result = service.check_group_availability(group.id, [range1, range2])
    assert len(result) == 2

    by_slot_name = {s.id: s.name for s in group.slots.all()}
    range1_by_name = {by_slot_name[s.slot_id]: s for s in result[0].slots}
    assert set(range1_by_name["Physicians"].available_calendar_ids) == {
        managed_calendars["phys_a"].id,
        managed_calendars["phys_b"].id,
    }
    assert set(range1_by_name["Rooms"].available_calendar_ids) == {
        managed_calendars["room_1"].id,
        managed_calendars["room_2"].id,
    }

    range2_by_name = {by_slot_name[s.slot_id]: s for s in result[1].slots}
    assert range2_by_name["Physicians"].available_calendar_ids == [managed_calendars["phys_a"].id]
    assert range2_by_name["Rooms"].available_calendar_ids == [managed_calendars["room_1"].id]


@pytest.mark.django_db
def test_check_group_availability_empty_slot_when_none_available(
    service, base_input, managed_calendars
):
    group = service.create_group(base_input)
    now = timezone.now().replace(microsecond=0)
    range1 = (now + timedelta(hours=1), now + timedelta(hours=2))
    # No AvailableTime rows created — managed calendars have nothing available.

    [availability] = service.check_group_availability(group.id, [range1])
    for slot_availability in availability.slots:
        assert slot_availability.available_calendar_ids == []
        assert not slot_availability.is_satisfied_for_required_count


# ---------------------------------------------------------------------------
# find_bookable_slots
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_find_bookable_slots_returns_windows_where_every_slot_satisfied(
    service, base_input, managed_calendars
):
    group = service.create_group(base_input)

    now = timezone.now().replace(microsecond=0)
    window_start = now + timedelta(hours=1)
    # Make only one 30-minute block available for at least one calendar in every slot.
    good_start = window_start + timedelta(minutes=15)
    good_end = good_start + timedelta(minutes=30)
    _make_available_time(managed_calendars["phys_a"], good_start, good_end)
    _make_available_time(managed_calendars["room_1"], good_start, good_end)

    proposals = service.find_bookable_slots(
        group_id=group.id,
        search_window_start=window_start,
        search_window_end=window_start + timedelta(hours=1),
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=15),
    )
    assert [(p.start_time, p.end_time) for p in proposals] == [(good_start, good_end)]


@pytest.mark.django_db
def test_find_bookable_slots_empty_when_any_slot_unsatisfied(
    service, base_input, managed_calendars
):
    group = service.create_group(base_input)
    now = timezone.now().replace(microsecond=0)
    window_start = now + timedelta(hours=1)
    # Only physicians have availability; rooms slot has no available calendar.
    _make_available_time(
        managed_calendars["phys_a"], window_start, window_start + timedelta(hours=1)
    )

    proposals = service.find_bookable_slots(
        group_id=group.id,
        search_window_start=window_start,
        search_window_end=window_start + timedelta(hours=1),
        duration=timedelta(minutes=30),
        slot_step=timedelta(minutes=15),
    )
    assert proposals == []


@pytest.mark.django_db
def test_find_bookable_slots_rejects_invalid_durations(service, base_input):
    group = service.create_group(base_input)
    now = timezone.now()
    with pytest.raises(CalendarGroupValidationError):
        service.find_bookable_slots(
            group_id=group.id,
            search_window_start=now,
            search_window_end=now + timedelta(hours=1),
            duration=timedelta(0),
        )
    with pytest.raises(CalendarGroupValidationError):
        service.find_bookable_slots(
            group_id=group.id,
            search_window_start=now,
            search_window_end=now + timedelta(hours=1),
            duration=timedelta(minutes=30),
            slot_step=timedelta(0),
        )
