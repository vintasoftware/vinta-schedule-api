import datetime

from django.db import IntegrityError

import pytest
from model_bakery import baker

from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
)


def _make_event(org, calendar, **extra):
    return baker.make(
        CalendarEvent,
        organization=org,
        calendar_fk=calendar,
        title="Event",
        external_id=baker.seq("ev"),
        start_time_tz_unaware=datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC),
        end_time_tz_unaware=datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        **extra,
    )


@pytest.mark.django_db
def test_calendar_group_str():
    org = baker.make("organizations.Organization")
    group = CalendarGroup.objects.create(organization=org, name="Clinic Appointments")

    assert str(group) == "Clinic Appointments"


@pytest.mark.django_db
def test_calendar_group_unique_name_per_org():
    org = baker.make("organizations.Organization")
    CalendarGroup.objects.create(organization=org, name="Clinic")

    with pytest.raises(IntegrityError):
        CalendarGroup.objects.create(organization=org, name="Clinic")


@pytest.mark.django_db
def test_calendar_group_same_name_different_org_allowed():
    org1 = baker.make("organizations.Organization")
    org2 = baker.make("organizations.Organization")

    CalendarGroup.objects.create(organization=org1, name="Clinic")
    CalendarGroup.objects.create(organization=org2, name="Clinic")  # should not raise

    assert CalendarGroup.objects.filter_by_organization(org1.id).count() == 1
    assert CalendarGroup.objects.filter_by_organization(org2.id).count() == 1


@pytest.mark.django_db
def test_calendar_group_slot_str_and_defaults():
    org = baker.make("organizations.Organization")
    group = CalendarGroup.objects.create(organization=org, name="Clinic")
    slot = CalendarGroupSlot.objects.create(organization=org, group=group, name="Physicians")

    assert "Physicians" in str(slot)
    assert slot.required_count == 1
    assert slot.order == 0


@pytest.mark.django_db
def test_calendar_group_slot_unique_name_per_group():
    org = baker.make("organizations.Organization")
    group = CalendarGroup.objects.create(organization=org, name="Clinic")
    CalendarGroupSlot.objects.create(organization=org, group=group, name="Physicians")

    with pytest.raises(IntegrityError):
        CalendarGroupSlot.objects.create(organization=org, group=group, name="Physicians")


@pytest.mark.django_db
def test_calendar_group_slot_same_name_different_group_allowed():
    org = baker.make("organizations.Organization")
    group1 = CalendarGroup.objects.create(organization=org, name="Clinic A")
    group2 = CalendarGroup.objects.create(organization=org, name="Clinic B")

    CalendarGroupSlot.objects.create(organization=org, group=group1, name="Physicians")
    CalendarGroupSlot.objects.create(organization=org, group=group2, name="Physicians")

    assert CalendarGroupSlot.objects.filter_by_organization(org.id).count() == 2


@pytest.mark.django_db
def test_calendar_group_slot_membership_unique():
    org = baker.make("organizations.Organization")
    group = CalendarGroup.objects.create(organization=org, name="Clinic")
    slot = CalendarGroupSlot.objects.create(organization=org, group=group, name="Physicians")
    calendar = baker.make(Calendar, organization=org, external_id=baker.seq("cal"))

    CalendarGroupSlotMembership.objects.create(organization=org, slot=slot, calendar=calendar)

    with pytest.raises(IntegrityError):
        CalendarGroupSlotMembership.objects.create(organization=org, slot=slot, calendar=calendar)


@pytest.mark.django_db
def test_calendar_group_slot_calendars_m2m():
    org = baker.make("organizations.Organization")
    group = CalendarGroup.objects.create(organization=org, name="Clinic")
    slot = CalendarGroupSlot.objects.create(organization=org, group=group, name="Physicians")
    cal1 = baker.make(Calendar, organization=org, external_id="cal-m2m-1")
    cal2 = baker.make(Calendar, organization=org, external_id="cal-m2m-2")

    CalendarGroupSlotMembership.objects.create(organization=org, slot=slot, calendar=cal1)
    CalendarGroupSlotMembership.objects.create(organization=org, slot=slot, calendar=cal2)

    assert set(slot.calendars.values_list("id", flat=True)) == {cal1.id, cal2.id}
    assert set(cal1.group_slots.values_list("id", flat=True)) == {slot.id}


@pytest.mark.django_db
def test_calendar_event_group_selection_unique():
    org = baker.make("organizations.Organization")
    group = CalendarGroup.objects.create(organization=org, name="Clinic")
    slot = CalendarGroupSlot.objects.create(organization=org, group=group, name="Physicians")
    calendar = baker.make(Calendar, organization=org, external_id=baker.seq("cal"))
    event = _make_event(org, calendar)

    CalendarEventGroupSelection.objects.create(
        organization=org, event=event, slot=slot, calendar=calendar
    )

    with pytest.raises(IntegrityError):
        CalendarEventGroupSelection.objects.create(
            organization=org, event=event, slot=slot, calendar=calendar
        )


@pytest.mark.django_db
def test_calendar_event_calendar_group_reverse_relation():
    org = baker.make("organizations.Organization")
    group = CalendarGroup.objects.create(organization=org, name="Clinic")
    calendar = baker.make(Calendar, organization=org, external_id=baker.seq("cal"))
    event = _make_event(org, calendar, calendar_group_fk=group)

    assert event.calendar_group_fk_id == group.id
    assert list(group.events.all()) == [event]
