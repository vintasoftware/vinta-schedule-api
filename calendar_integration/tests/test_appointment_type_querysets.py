from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

import pytest

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AppointmentType,
    AppointmentTypeSlot,
    AppointmentTypeSlotMembership,
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
)
from organizations.models import Organization


@pytest.mark.django_db
class TestAppointmentTypeBookableInRanges(TestCase):
    """Tests for AppointmentTypeQuerySet.only_appointment_types_bookable_in_ranges."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org", should_sync_rooms=True)
        self.now = timezone.now().replace(microsecond=0)

        # Two ranges
        self.range1 = (self.now + timedelta(hours=1), self.now + timedelta(hours=2))
        self.range2 = (self.now + timedelta(hours=3), self.now + timedelta(hours=4))

        # Two physician calendars (managed: must have AvailableTime in range)
        self.physician_a = Calendar.objects.create(
            name="Dr. A",
            external_id="phys_a",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=True,
            organization=self.organization,
        )
        self.physician_b = Calendar.objects.create(
            name="Dr. B",
            external_id="phys_b",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=True,
            organization=self.organization,
        )

        # Two room calendars (resource, managed)
        self.room_1 = Calendar.objects.create(
            name="Room 1",
            external_id="room_1",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.RESOURCE,
            manage_available_windows=True,
            capacity=4,
            organization=self.organization,
        )
        self.room_2 = Calendar.objects.create(
            name="Room 2",
            external_id="room_2",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.RESOURCE,
            manage_available_windows=True,
            capacity=4,
            organization=self.organization,
        )

        # All physicians + rooms have availability in range1; only physician_a + room_1 in range2
        for cal in (self.physician_a, self.physician_b, self.room_1, self.room_2):
            AvailableTime.objects.create(
                organization=self.organization,
                calendar=cal,
                start_time_tz_unaware=self.range1[0],
                end_time_tz_unaware=self.range1[1],
                timezone="UTC",
            )
        for cal in (self.physician_a, self.room_1):
            AvailableTime.objects.create(
                organization=self.organization,
                calendar=cal,
                start_time_tz_unaware=self.range2[0],
                end_time_tz_unaware=self.range2[1],
                timezone="UTC",
            )

        # Build the clinic appointment type
        self.appointment_type = AppointmentType.objects.create(
            organization=self.organization, name="Clinic Appointments"
        )
        self.physicians_slot = AppointmentTypeSlot.objects.create(
            organization=self.organization,
            appointment_type=self.appointment_type,
            name="Physicians",
            order=0,
        )
        self.rooms_slot = AppointmentTypeSlot.objects.create(
            organization=self.organization,
            appointment_type=self.appointment_type,
            name="Rooms",
            order=1,
        )
        for cal in (self.physician_a, self.physician_b):
            AppointmentTypeSlotMembership.objects.create(
                organization=self.organization, slot=self.physicians_slot, calendar=cal
            )
        for cal in (self.room_1, self.room_2):
            AppointmentTypeSlotMembership.objects.create(
                organization=self.organization, slot=self.rooms_slot, calendar=cal
            )

    def _bookable(self, ranges):
        return list(
            AppointmentType.objects.filter_by_organization(
                self.organization.id
            ).only_appointment_types_bookable_in_ranges(ranges)
        )

    def test_empty_ranges_returns_none(self):
        assert self._bookable([]) == []

    def test_single_range_all_slots_satisfied(self):
        bookable = self._bookable([self.range1])
        assert bookable == [self.appointment_type]

    def test_multi_range_all_slots_satisfied(self):
        bookable = self._bookable([self.range1, self.range2])
        # range2 still has physician_a and room_1 available; required_count=1 each
        assert bookable == [self.appointment_type]

    def test_slot_with_zero_available_excludes_appointment_type(self):
        # Block out room_1 in range2; room_2 has no availability in range2 either
        # so the rooms slot has 0 available calendars in range2.
        AvailableTime.objects.filter_by_organization(self.organization.id).filter(
            calendar_fk=self.room_1, start_time_tz_unaware=self.range2[0]
        ).delete()
        bookable = self._bookable([self.range2])
        assert bookable == []

    def test_required_count_greater_than_available_excludes_appointment_type(self):
        # Need 2 physicians but only 1 (physician_a) available in range2
        self.physicians_slot.required_count = 2
        self.physicians_slot.save()
        bookable = self._bookable([self.range2])
        assert bookable == []

    def test_required_count_satisfied_when_enough_available(self):
        # Need 2 physicians; both are available in range1
        self.physicians_slot.required_count = 2
        self.physicians_slot.save()
        bookable = self._bookable([self.range1])
        assert bookable == [self.appointment_type]

    def test_unmanaged_calendar_with_event_excluded_from_pool(self):
        # Convert physician_b to an unmanaged calendar with a conflicting event in range1.
        # Then physicians slot still has physician_a available — appointment type bookable for required_count=1.
        self.physician_b.manage_available_windows = False
        self.physician_b.save()
        AvailableTime.objects.filter_by_organization(self.organization.id).filter(
            calendar_fk=self.physician_b
        ).delete()
        CalendarEvent.objects.create(
            organization=self.organization,
            calendar=self.physician_b,
            title="Existing appointment",
            description="",
            start_time_tz_unaware=self.range1[0],
            end_time_tz_unaware=self.range1[1],
            timezone="UTC",
            external_id="ev_phys_b_busy",
        )
        bookable = self._bookable([self.range1])
        assert bookable == [self.appointment_type]

        # If we now require 2 physicians, the appointment type should be excluded.
        self.physicians_slot.required_count = 2
        self.physicians_slot.save()
        bookable = self._bookable([self.range1])
        assert bookable == []

    def test_unmanaged_calendar_with_blocked_time_excluded_from_pool(self):
        self.physician_b.manage_available_windows = False
        self.physician_b.save()
        AvailableTime.objects.filter_by_organization(self.organization.id).filter(
            calendar_fk=self.physician_b
        ).delete()
        BlockedTime.objects.create(
            organization=self.organization,
            calendar=self.physician_b,
            start_time_tz_unaware=self.range1[0],
            end_time_tz_unaware=self.range1[1],
            reason="Out of office",
            timezone="UTC",
        )
        # required_count=1 still satisfied by physician_a
        bookable = self._bookable([self.range1])
        assert bookable == [self.appointment_type]

    def test_other_org_appointment_types_not_returned(self):
        other_org = Organization.objects.create(name="Other Org", should_sync_rooms=False)
        AppointmentType.objects.create(organization=other_org, name="Other Clinic")

        bookable = self._bookable([self.range1])
        assert bookable == [self.appointment_type]

    def test_appointment_type_with_no_slots_is_bookable(self):
        # Edge case: an appointment type with zero slots has no unsatisfied slot, so it's vacuously bookable.
        empty_appointment_type = AppointmentType.objects.create(
            organization=self.organization, name="Empty AppointmentType"
        )
        bookable = self._bookable([self.range1])
        assert empty_appointment_type in bookable
        assert self.appointment_type in bookable

    def test_appointment_type_with_empty_slot_pool_excluded(self):
        empty_pool_appointment_type = AppointmentType.objects.create(
            organization=self.organization, name="Pool-less"
        )
        AppointmentTypeSlot.objects.create(
            organization=self.organization,
            appointment_type=empty_pool_appointment_type,
            name="Nobody",
        )
        bookable = self._bookable([self.range1])
        assert empty_pool_appointment_type not in bookable

    def test_another_organizations_slot_pointing_at_this_appointment_type_is_ignored(self):
        """The ``unsatisfied_slot`` subquery is correlated on the organization as
        well as on the appointment type key.

        Correlating on the key alone let a slot row owned by *another*
        organization count towards this appointment type's ``~Exists(unsatisfied_slot)``
        filter: the row below has no pool at all, so it can never be satisfied,
        and it would silently remove this organization's appointment type from every
        availability result -- a cross-tenant read with no error and no trace.

        The row is written by setting the concrete ``appointment_type_fk_id`` directly,
        which is the only way to produce it: assigning ``appointment_type=`` copies the
        target's organization onto the slot, which is what the safe relation is
        for. The control below (the same slot inside this organization) proves
        the fixture really is unsatisfiable, so this cannot pass by accident.
        """
        other_org = Organization.objects.create(name="Other Org", should_sync_rooms=False)
        AppointmentTypeSlot.objects.create(
            organization=other_org,
            appointment_type_fk_id=self.appointment_type.id,
            name="Other org's unsatisfiable slot",
            order=99,
        )

        assert self._bookable([self.range1]) == [self.appointment_type]

        # Control: the identical slot inside *this* organization does exclude the
        # appointment type, so the assertion above is about the organization and nothing else.
        AppointmentTypeSlot.objects.create(
            organization=self.organization,
            appointment_type_fk_id=self.appointment_type.id,
            name="Our own unsatisfiable slot",
            order=100,
        )

        assert self._bookable([self.range1]) == []
