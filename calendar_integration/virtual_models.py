from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarEventGroupSelection,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarGroupSlotQuotaRule,
    CalendarOwnership,
    EventAttendance,
    EventExternalAttendance,
    EventRecurrenceException,
    ExternalAttendee,
    ExternalEventChangeRequest,
    RecurrenceRule,
    ResourceAllocation,
)
from common.virtual_models import OrganizationScopedVirtualModel
from organizations.virtual_models import OrganizationMembershipVirtualModel


class CalendarOwnershipVirtualModel(OrganizationScopedVirtualModel):
    membership = OrganizationMembershipVirtualModel()

    class Meta:
        model = CalendarOwnership


class CalendarVirtualModel(OrganizationScopedVirtualModel):
    memberships = OrganizationMembershipVirtualModel(many=True)
    calendar_ownerships = CalendarOwnershipVirtualModel(many=True)

    class Meta:
        model = Calendar


class ExternalAttendeeVirtualModel(OrganizationScopedVirtualModel):
    class Meta:
        model = ExternalAttendee


class EventExternalAttendanceVirtualModel(OrganizationScopedVirtualModel):
    external_attendee = ExternalAttendeeVirtualModel()

    class Meta:
        model = EventExternalAttendance


class EventAttendanceVirtualModel(OrganizationScopedVirtualModel):
    membership = OrganizationMembershipVirtualModel()

    class Meta:
        model = EventAttendance


class ResourceAllocationVirtualModel(OrganizationScopedVirtualModel):
    calendar = CalendarVirtualModel()

    class Meta:
        model = ResourceAllocation


class RecurrenceRuleVirtualModel(OrganizationScopedVirtualModel):
    class Meta:
        model = RecurrenceRule


class NestedCalendarEventVirtualModel(OrganizationScopedVirtualModel):
    class Meta:
        model = CalendarEvent


class CalendarGroupSlotMembershipVirtualModel(OrganizationScopedVirtualModel):
    calendar = CalendarVirtualModel()

    class Meta:
        model = CalendarGroupSlotMembership


class CalendarGroupSlotVirtualModel(OrganizationScopedVirtualModel):
    memberships = CalendarGroupSlotMembershipVirtualModel(many=True)
    calendars = CalendarVirtualModel(many=True)

    class Meta:
        model = CalendarGroupSlot


class CalendarGroupVirtualModel(OrganizationScopedVirtualModel):
    slots = CalendarGroupSlotVirtualModel(many=True)

    class Meta:
        model = CalendarGroup


class CalendarEventGroupSelectionVirtualModel(OrganizationScopedVirtualModel):
    slot = CalendarGroupSlotVirtualModel()
    calendar = CalendarVirtualModel()

    class Meta:
        model = CalendarEventGroupSelection


class CalendarEventVirtualModel(OrganizationScopedVirtualModel):
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


class EventRecurrenceExceptionVirtualModel(OrganizationScopedVirtualModel):
    parent_event = CalendarEventVirtualModel()
    modified_event = CalendarEventVirtualModel()

    class Meta:
        model = EventRecurrenceException


class NestedBlockedTimeVirtualModel(OrganizationScopedVirtualModel):
    class Meta:
        model = BlockedTime


class BlockedTimeVirtualModel(OrganizationScopedVirtualModel):
    calendar = CalendarVirtualModel()
    recurrence_rule = RecurrenceRuleVirtualModel()
    parent_recurring_object = NestedBlockedTimeVirtualModel()

    class Meta:
        model = BlockedTime


class NestedAvailableTimeVirtualModel(OrganizationScopedVirtualModel):
    class Meta:
        model = AvailableTime


class AvailableTimeVirtualModel(OrganizationScopedVirtualModel):
    calendar = CalendarVirtualModel()
    recurrence_rule = RecurrenceRuleVirtualModel()
    parent_recurring_object = NestedAvailableTimeVirtualModel()

    class Meta:
        model = AvailableTime


class GroupScopedAvailabilityWindowVirtualModel(OrganizationScopedVirtualModel):
    """Virtual model for ``GroupScopedAvailabilityWindowSerializer``.

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


class GroupScopedBlockedTimeVirtualModel(OrganizationScopedVirtualModel):
    """Virtual model for ``GroupScopedBlockedTimeSerializer``.

    Mirrors ``GroupScopedAvailabilityWindowVirtualModel`` exactly, for the
    same reason: the serializer sources ``calendar_id``/``group_slot_id``
    from the raw FK columns and never nests a ``calendar`` field, so no
    sub-field is declared here for it -- avoids pulling in
    ``CalendarVirtualModel``'s eager memberships/calendar_ownerships graph,
    which this serializer never reads. ``rrule_string``/``is_recurring`` are
    ``no_deferred_fields()``-hinted ``SerializerMethodField``s that read
    ``recurrence_rule`` directly; the view selects that relation explicitly
    (``.select_related("recurrence_rule")`` in
    ``GroupScopedBlockedTimeViewSet.get_queryset``) since it isn't exposed
    under a matching field name here for the optimizer to infer.
    """

    class Meta:
        model = BlockedTime


class GroupScopedQuotaRuleVirtualModel(OrganizationScopedVirtualModel):
    """Virtual model for ``GroupScopedQuotaRuleSerializer``.

    Simpler than ``GroupScopedAvailabilityWindowVirtualModel``/
    ``GroupScopedBlockedTimeVirtualModel``: a quota rule has no recurrence and
    no time range, so there is no ``recurrence_rule``/``parent_recurring_object``
    to fetch. The serializer sources ``calendar_id``/``group_slot_id`` from the
    raw FK columns and never nests a ``calendar`` field, so no sub-field is
    declared here either -- avoids pulling in ``CalendarVirtualModel``'s eager
    memberships/calendar_ownerships graph, which this serializer never reads.
    """

    class Meta:
        model = CalendarGroupSlotQuotaRule


class ExternalEventChangeRequestVirtualModel(OrganizationScopedVirtualModel):
    """Virtual model for ``ExternalEventChangeRequest`` serialization.

    The serializer only reads direct columns (``event_fk_id``,
    ``resolved_by_user_id``) so no nested prefetches are required.
    """

    class Meta:
        model = ExternalEventChangeRequest
