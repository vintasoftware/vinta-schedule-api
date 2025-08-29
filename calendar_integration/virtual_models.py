import django_virtual_models as v

from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarOwnership,
    EventAttendance,
    EventExternalAttendance,
    ExternalAttendee,
    RecurrenceException,
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


class CalendarEventVirtualModel(v.VirtualModel):
    calendar = CalendarVirtualModel()
    external_attendances = EventExternalAttendanceVirtualModel(many=True)
    attendances = EventAttendanceVirtualModel(many=True)
    resource_allocations = ResourceAllocationVirtualModel(many=True)
    recurrence_rule = RecurrenceRuleVirtualModel()
    parent_recurring_object = NestedCalendarEventVirtualModel()

    class Meta:
        model = CalendarEvent


class RecurrenceExceptionVirtualModel(v.VirtualModel):
    parent_event = CalendarEventVirtualModel()
    modified_event = CalendarEventVirtualModel()

    class Meta:
        model = RecurrenceException


class BlockedTimeVirtualModel(v.VirtualModel):
    calendar = CalendarVirtualModel()

    class Meta:
        model = BlockedTime


class AvailableTimeVirtualModel(v.VirtualModel):
    calendar = CalendarVirtualModel()

    class Meta:
        model = AvailableTime
