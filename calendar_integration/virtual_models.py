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
    ExternalEventChangeRequest,
    RecurrenceRule,
    ResourceAllocation,
)
from organizations.virtual_models import OrganizationMembershipVirtualModel


class CalendarOwnershipVirtualModel(v.VirtualModel):
    membership = OrganizationMembershipVirtualModel()

    class Meta:
        model = CalendarOwnership


class CalendarVirtualModel(v.VirtualModel):
    memberships = OrganizationMembershipVirtualModel(many=True)
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
    membership = OrganizationMembershipVirtualModel()

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


class NestedBlockedTimeVirtualModel(v.VirtualModel):
    class Meta:
        model = BlockedTime


class BlockedTimeVirtualModel(v.VirtualModel):
    calendar = CalendarVirtualModel()
    recurrence_rule = RecurrenceRuleVirtualModel()
    parent_recurring_object = NestedBlockedTimeVirtualModel()

    class Meta:
        model = BlockedTime


class NestedAvailableTimeVirtualModel(v.VirtualModel):
    class Meta:
        model = AvailableTime


class AvailableTimeVirtualModel(v.VirtualModel):
    calendar = CalendarVirtualModel()
    recurrence_rule = RecurrenceRuleVirtualModel()
    parent_recurring_object = NestedAvailableTimeVirtualModel()

    class Meta:
        model = AvailableTime


class GroupScopedAvailabilityWindowVirtualModel(v.VirtualModel):
    """Virtual model for ``GroupScopedAvailabilityWindowSerializer``
    (CALENDAR_GROUP_SCOPED_AVAILABILITY Phase 1c).

    Deliberately narrower than ``AvailableTimeVirtualModel``: that serializer
    sources ``calendar_id``/``group_slot_id`` from the raw FK columns and
    never nests a ``calendar`` field, so no sub-field is declared here for it
    -- avoids pulling in ``CalendarVirtualModel``'s eager
    memberships/calendar_ownerships graph, which this serializer never reads.
    ``rrule_string``/``is_recurring`` are ``no_deferred_fields()``-hinted
    ``SerializerMethodField``s that read ``recurrence_rule`` directly; the
    view selects that relation explicitly (``.select_related("recurrence_rule")``
    in ``GroupScopedAvailabilityWindowViewSet.get_queryset``) since it isn't
    exposed under a matching field name here for the optimizer to infer.
    """

    class Meta:
        model = AvailableTime


class ExternalEventChangeRequestVirtualModel(v.VirtualModel):
    """Virtual model for ``ExternalEventChangeRequest`` serialization.

    The serializer only reads direct columns (``event_fk_id``,
    ``resolved_by_user_id``) so no nested prefetches are required.
    """

    class Meta:
        model = ExternalEventChangeRequest
