import datetime

from django.db import IntegrityError

import pytest
from model_bakery import baker

from calendar_integration.models import (
    AppointmentType,
    AppointmentTypeSlot,
    AppointmentTypeSlotMembership,
    Calendar,
    CalendarEvent,
    CalendarEventAppointmentTypeSelection,
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
def test_appointment_type_str():
    org = baker.make("organizations.Organization")
    appointment_type = AppointmentType.objects.create(organization=org, name="Clinic Appointments")

    assert str(appointment_type) == "Clinic Appointments"


@pytest.mark.django_db
def test_appointment_type_unique_name_per_org():
    org = baker.make("organizations.Organization")
    AppointmentType.objects.create(organization=org, name="Clinic")

    with pytest.raises(IntegrityError):
        AppointmentType.objects.create(organization=org, name="Clinic")


@pytest.mark.django_db
def test_appointment_type_same_name_different_org_allowed():
    org1 = baker.make("organizations.Organization")
    org2 = baker.make("organizations.Organization")

    AppointmentType.objects.create(organization=org1, name="Clinic")
    AppointmentType.objects.create(organization=org2, name="Clinic")  # should not raise

    assert AppointmentType.objects.filter_by_organization(org1.id).count() == 1
    assert AppointmentType.objects.filter_by_organization(org2.id).count() == 1


@pytest.mark.django_db
def test_appointment_type_slot_str_and_defaults():
    org = baker.make("organizations.Organization")
    appointment_type = AppointmentType.objects.create(organization=org, name="Clinic")
    slot = AppointmentTypeSlot.objects.create(
        organization=org, appointment_type=appointment_type, name="Physicians"
    )

    assert "Physicians" in str(slot)
    assert slot.required_count == 1
    assert slot.order == 0


@pytest.mark.django_db
def test_appointment_type_slot_unique_name_per_appointment_type():
    org = baker.make("organizations.Organization")
    appointment_type = AppointmentType.objects.create(organization=org, name="Clinic")
    AppointmentTypeSlot.objects.create(
        organization=org, appointment_type=appointment_type, name="Physicians"
    )

    with pytest.raises(IntegrityError):
        AppointmentTypeSlot.objects.create(
            organization=org, appointment_type=appointment_type, name="Physicians"
        )


@pytest.mark.django_db
def test_appointment_type_slot_same_name_different_appointment_type_allowed():
    org = baker.make("organizations.Organization")
    appointment_type1 = AppointmentType.objects.create(organization=org, name="Clinic A")
    appointment_type2 = AppointmentType.objects.create(organization=org, name="Clinic B")

    AppointmentTypeSlot.objects.create(
        organization=org, appointment_type=appointment_type1, name="Physicians"
    )
    AppointmentTypeSlot.objects.create(
        organization=org, appointment_type=appointment_type2, name="Physicians"
    )

    assert AppointmentTypeSlot.objects.filter_by_organization(org.id).count() == 2


@pytest.mark.django_db
def test_appointment_type_slot_membership_unique():
    org = baker.make("organizations.Organization")
    appointment_type = AppointmentType.objects.create(organization=org, name="Clinic")
    slot = AppointmentTypeSlot.objects.create(
        organization=org, appointment_type=appointment_type, name="Physicians"
    )
    calendar = baker.make(Calendar, organization=org, external_id=baker.seq("cal"))

    AppointmentTypeSlotMembership.objects.create(organization=org, slot=slot, calendar=calendar)

    with pytest.raises(IntegrityError):
        AppointmentTypeSlotMembership.objects.create(organization=org, slot=slot, calendar=calendar)


@pytest.mark.django_db
def test_appointment_type_slot_calendars_m2m():
    org = baker.make("organizations.Organization")
    appointment_type = AppointmentType.objects.create(organization=org, name="Clinic")
    slot = AppointmentTypeSlot.objects.create(
        organization=org, appointment_type=appointment_type, name="Physicians"
    )
    cal1 = baker.make(Calendar, organization=org, external_id="cal-m2m-1")
    cal2 = baker.make(Calendar, organization=org, external_id="cal-m2m-2")

    AppointmentTypeSlotMembership.objects.create(organization=org, slot=slot, calendar=cal1)
    AppointmentTypeSlotMembership.objects.create(organization=org, slot=slot, calendar=cal2)

    assert set(slot.calendars.values_list("id", flat=True)) == {cal1.id, cal2.id}
    assert set(cal1.appointment_type_slots.values_list("id", flat=True)) == {slot.id}


@pytest.mark.django_db
def test_calendar_event_appointment_type_selection_unique():
    org = baker.make("organizations.Organization")
    appointment_type = AppointmentType.objects.create(organization=org, name="Clinic")
    slot = AppointmentTypeSlot.objects.create(
        organization=org, appointment_type=appointment_type, name="Physicians"
    )
    calendar = baker.make(Calendar, organization=org, external_id=baker.seq("cal"))
    event = _make_event(org, calendar)

    CalendarEventAppointmentTypeSelection.objects.create(
        organization=org, event=event, slot=slot, calendar=calendar
    )

    with pytest.raises(IntegrityError):
        CalendarEventAppointmentTypeSelection.objects.create(
            organization=org, event=event, slot=slot, calendar=calendar
        )


@pytest.mark.django_db
def test_calendar_event_appointment_type_reverse_relation():
    org = baker.make("organizations.Organization")
    appointment_type = AppointmentType.objects.create(organization=org, name="Clinic")
    calendar = baker.make(Calendar, organization=org, external_id=baker.seq("cal"))
    event = _make_event(org, calendar, appointment_type_fk=appointment_type)

    assert event.appointment_type_fk_id == appointment_type.id
    assert list(appointment_type.events.all()) == [event]


@pytest.mark.django_db
def test_appointment_type_accepts_public_scheduling_default_false():
    org = baker.make("organizations.Organization")
    appointment_type = AppointmentType.objects.create(organization=org, name="Test AppointmentType")

    assert appointment_type.accepts_public_scheduling is False


@pytest.mark.django_db
def test_appointment_type_accepts_public_scheduling_true():
    org = baker.make("organizations.Organization")
    appointment_type = AppointmentType.objects.create(
        organization=org, name="Public AppointmentType", accepts_public_scheduling=True
    )

    assert appointment_type.accepts_public_scheduling is True


@pytest.mark.django_db
def test_appointment_type_accepts_public_scheduling_update():
    org = baker.make("organizations.Organization")
    appointment_type = AppointmentType.objects.create(organization=org, name="Test AppointmentType")

    assert appointment_type.accepts_public_scheduling is False
    appointment_type.accepts_public_scheduling = True
    appointment_type.save()
    appointment_type.refresh_from_db()

    assert appointment_type.accepts_public_scheduling is True
