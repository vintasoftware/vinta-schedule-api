import django_virtual_models as v

from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarOwnership,
    EventAttendance,
    EventExternalAttendance,
    EventRecurrenceException,
    ExternalAttendee,
    RecurrenceRule,
    ResourceAllocation,
)
from users.virtual_models import UserVirtualModel


class CalendarOwnershipVirtualModel(v.VirtualModel):
    user = UserVirtualModel()

    class Meta:
        model = CalendarOwnership


class CalendarVirtualModel(v.VirtualModel):
    users = UserVirtualModel(many=True)
    calendar_ownerships = CalendarOwnershipVirtualModel(many=True)

    class Meta:
        model = Calendar


class ExternalAttendeeVirtualModel(v.VirtualModel):
    class Meta:
        model = ExternalAttendee


class EventExternalAttendanceVirtualModel(v.VirtualModel):
    external_attendee = ExternalAttendeeVirtualModel()

    class Meta:
        model = EventExternalAttendance


class EventAttendanceVirtualModel(v.VirtualModel):
    user = UserVirtualModel()

    class Meta:
        model = EventAttendance


class ResourceAllocationVirtualModel(v.VirtualModel):
    calendar = CalendarVirtualModel()

    class Meta:
        model = ResourceAllocation


class RecurrenceRuleVirtualModel(v.VirtualModel):
    class Meta:
        model = RecurrenceRule


class NestedCalendarEventVirtualModel(v.VirtualModel):
    class Meta:
        model = CalendarEvent


class CalendarGroupSlotMembershipVirtualModel(v.VirtualModel):
    calendar = CalendarVirtualModel()

    class Meta:
        model = CalendarGroupSlotMembership


class CalendarGroupSlotVirtualModel(v.VirtualModel):
    memberships = CalendarGroupSlotMembershipVirtualModel(many=True)
    calendars = CalendarVirtualModel(many=True)

    class Meta:
        model = CalendarGroupSlot


class CalendarGroupVirtualModel(v.VirtualModel):
    slots = CalendarGroupSlotVirtualModel(many=True)

    class Meta:
        model = CalendarGroup


class CalendarEventGroupSelectionVirtualModel(v.VirtualModel):
    slot = CalendarGroupSlotVirtualModel()
    calendar = CalendarVirtualModel()

    class Meta:
        model = CalendarEventGroupSelection


class CalendarEventVirtualModel(v.VirtualModel):
    calendar = CalendarVirtualModel()
    external_attendances = EventExternalAttendanceVirtualModel(many=True)
    attendances = EventAttendanceVirtualModel(many=True)
    resource_allocations = ResourceAllocationVirtualModel(many=True)
    recurrence_rule = RecurrenceRuleVirtualModel()
    parent_recurring_object = NestedCalendarEventVirtualModel()
    group_selections = CalendarEventGroupSelectionVirtualModel(many=True)
    calendar_group = CalendarGroupVirtualModel()

    class Meta:
        model = CalendarEvent


class EventRecurrenceExceptionVirtualModel(v.VirtualModel):
    parent_event = CalendarEventVirtualModel()
    modified_event = CalendarEventVirtualModel()

    class Meta:
        model = EventRecurrenceException


class BlockedTimeVirtualModel(v.VirtualModel):
    calendar = CalendarVirtualModel()

    class Meta:
        model = BlockedTime


class AvailableTimeVirtualModel(v.VirtualModel):
    calendar = CalendarVirtualModel()

    class Meta:
        model = AvailableTime
