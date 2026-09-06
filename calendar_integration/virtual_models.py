from typing import Any

from django.db.models import Model, QuerySet

from calendar_integration.models import (
    AppointmentType,
    AppointmentTypeSlot,
    AppointmentTypeSlotMembership,
    AppointmentTypeSlotQuotaRule,
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarEventAppointmentTypeSelection,
    CalendarOwnership,
    CalendarPool,
    CalendarPoolMembership,
    EventAttendance,
    EventExternalAttendance,
    EventRecurrenceException,
    ExternalAttendee,
    ExternalClientIdentifier,
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


class ExternalClientIdentifierVirtualModel(OrganizationScopedVirtualModel):
    class Meta:
        model = ExternalClientIdentifier


class ExternalAttendeeVirtualModel(OrganizationScopedVirtualModel):
    external_client_identifiers = ExternalClientIdentifierVirtualModel(many=True)

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


class AppointmentTypeSlotMembershipVirtualModel(OrganizationScopedVirtualModel):
    calendar = CalendarVirtualModel()

    class Meta:
        model = AppointmentTypeSlotMembership


class DistinctCalendarVirtualModel(CalendarVirtualModel):
    """``CalendarVirtualModel`` whose prefetch cannot return the same row twice.

    ``AppointmentTypeSlot.calendars`` goes through ``AppointmentTypeSlotMembership``,
    which since Calendar Pools holds one row per (slot, calendar, source): a
    calendar that is both inline on the slot and in an attached ``CalendarPool``
    has two, and the M2M prefetch would hand the serializer the same calendar
    twice. ``DISTINCT`` collapses them; the prefetch's own join columns are part
    of the ``SELECT`` list, so it dedupes per parent slot rather than globally.
    """

    def get_prefetch_queryset(self, user: Model | None = None, **kwargs: Any) -> QuerySet:
        return super().get_prefetch_queryset(user=user, **kwargs).distinct()


class CalendarPoolMembershipVirtualModel(OrganizationScopedVirtualModel):
    calendar = CalendarVirtualModel()

    class Meta:
        model = CalendarPoolMembership


class CalendarPoolVirtualModel(OrganizationScopedVirtualModel):
    memberships = CalendarPoolMembershipVirtualModel(many=True)
    calendars = CalendarVirtualModel(many=True)

    class Meta:
        model = CalendarPool


class AppointmentTypeSlotVirtualModel(OrganizationScopedVirtualModel):
    memberships = AppointmentTypeSlotMembershipVirtualModel(many=True)
    calendars = DistinctCalendarVirtualModel(many=True)
    # Added alongside `AppointmentTypeSlotSerializer.pools` (Phase 4) -- Phase 3
    # deliberately left this hint off, since an unconditional prefetch with no
    # serializer field would cost a query on every appointment type fetch for nothing.
    # `AppointmentTypeSlotPool` (the through table) is a plain M2M attachment --
    # unique on (slot, pool) -- so no dedup is needed here the way
    # `DistinctCalendarVirtualModel` is for `calendars`.
    pools = CalendarPoolVirtualModel(many=True)

    class Meta:
        model = AppointmentTypeSlot


class AppointmentTypeVirtualModel(OrganizationScopedVirtualModel):
    slots = AppointmentTypeSlotVirtualModel(many=True)

    class Meta:
        model = AppointmentType


class CalendarEventAppointmentTypeSelectionVirtualModel(OrganizationScopedVirtualModel):
    slot = AppointmentTypeSlotVirtualModel()
    calendar = CalendarVirtualModel()

    class Meta:
        model = CalendarEventAppointmentTypeSelection


class CalendarEventVirtualModel(OrganizationScopedVirtualModel):
    calendar = CalendarVirtualModel()
    external_attendances = EventExternalAttendanceVirtualModel(many=True)
    attendances = EventAttendanceVirtualModel(many=True)
    resource_allocations = ResourceAllocationVirtualModel(many=True)
    recurrence_rule = RecurrenceRuleVirtualModel()
    parent_recurring_object = NestedCalendarEventVirtualModel()
    appointment_type_selections = CalendarEventAppointmentTypeSelectionVirtualModel(many=True)
    appointment_type = AppointmentTypeVirtualModel()
    external_client_identifiers = ExternalClientIdentifierVirtualModel(many=True)

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


class AppointmentTypeScopedAvailabilityWindowVirtualModel(OrganizationScopedVirtualModel):
    """Virtual model for ``AppointmentTypeScopedAvailabilityWindowSerializer``.

    Deliberately narrower than ``AvailableTimeVirtualModel``: that serializer
    sources ``calendar_id``/``appointment_type_slot_id`` from the raw FK columns and
    never nests a ``calendar`` field, so no sub-field is declared here for it
    -- avoids pulling in ``CalendarVirtualModel``'s eager
    memberships/calendar_ownerships graph, which this serializer never reads.
    ``rrule_string``/``is_recurring`` are ``no_deferred_fields()``-hinted
    ``SerializerMethodField``s that read ``recurrence_rule`` directly; the
    view selects that relation explicitly (``.select_related("recurrence_rule")``
    in ``AppointmentTypeScopedAvailabilityWindowViewSet.get_queryset``) since it isn't
    exposed under a matching field name here for the optimizer to infer.
    """

    class Meta:
        model = AvailableTime


class AppointmentTypeScopedBlockedTimeVirtualModel(OrganizationScopedVirtualModel):
    """Virtual model for ``AppointmentTypeScopedBlockedTimeSerializer``.

    Mirrors ``AppointmentTypeScopedAvailabilityWindowVirtualModel`` exactly, for the
    same reason: the serializer sources ``calendar_id``/``appointment_type_slot_id``
    from the raw FK columns and never nests a ``calendar`` field, so no
    sub-field is declared here for it -- avoids pulling in
    ``CalendarVirtualModel``'s eager memberships/calendar_ownerships graph,
    which this serializer never reads. ``rrule_string``/``is_recurring`` are
    ``no_deferred_fields()``-hinted ``SerializerMethodField``s that read
    ``recurrence_rule`` directly; the view selects that relation explicitly
    (``.select_related("recurrence_rule")`` in
    ``AppointmentTypeScopedBlockedTimeViewSet.get_queryset``) since it isn't exposed
    under a matching field name here for the optimizer to infer.
    """

    class Meta:
        model = BlockedTime


class AppointmentTypeScopedQuotaRuleVirtualModel(OrganizationScopedVirtualModel):
    """Virtual model for ``AppointmentTypeScopedQuotaRuleSerializer``.

    Simpler than ``AppointmentTypeScopedAvailabilityWindowVirtualModel``/
    ``AppointmentTypeScopedBlockedTimeVirtualModel``: a quota rule has no recurrence and
    no time range, so there is no ``recurrence_rule``/``parent_recurring_object``
    to fetch. The serializer sources ``calendar_id``/``appointment_type_slot_id`` from the
    raw FK columns and never nests a ``calendar`` field, so no sub-field is
    declared here either -- avoids pulling in ``CalendarVirtualModel``'s eager
    memberships/calendar_ownerships graph, which this serializer never reads.
    """

    class Meta:
        model = AppointmentTypeSlotQuotaRule


class ExternalEventChangeRequestVirtualModel(OrganizationScopedVirtualModel):
    """Virtual model for ``ExternalEventChangeRequest`` serialization.

    The serializer only reads direct columns (``event_fk_id``,
    ``resolved_by_user_id``) so no nested prefetches are required.
    """

    class Meta:
        model = ExternalEventChangeRequest
