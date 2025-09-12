from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from calendar_integration.models import CalendarManagementToken
from calendar_integration.services.dataclasses import CalendarEventData, EventExternalAttendeeData
from organizations.models import Organization
from public_api.models import SystemUser
from users.models import User


@runtime_checkable
class OnCreateEventHandler(Protocol):
    def on_create_event(
        self,
        actor: User | CalendarManagementToken | SystemUser | None,
        event: CalendarEventData,
        organization: Organization,
    ) -> None:
        ...


@runtime_checkable
class OnUpdateEventHandler(Protocol):
    def on_update_event(
        self,
        actor: User | CalendarManagementToken | SystemUser | None,
        event: CalendarEventData,
        organization: Organization,
    ) -> None:
        ...


@runtime_checkable
class OnDeleteEventHandler(Protocol):
    def on_delete_event(
        self,
        actor: User | CalendarManagementToken | SystemUser | None,
        event: CalendarEventData,
        organization: Organization,
    ) -> None:
        ...


@runtime_checkable
class OnAddAttendeeToEventHandler(Protocol):
    def on_add_attendee_to_event(
        self,
        actor: User | CalendarManagementToken | SystemUser | None,
        event: CalendarEventData,
        attendance: EventExternalAttendeeData | EventExternalAttendeeData,
        organization: Organization,
    ) -> None:
        ...


@runtime_checkable
class OnRemoveAttendeeFromEventHandler(Protocol):
    def on_remove_attendee_from_event(
        self,
        actor: User | CalendarManagementToken | SystemUser | None,
        event: CalendarEventData,
        attendance: EventExternalAttendeeData | EventExternalAttendeeData,
        organization: Organization,
    ) -> None:
        ...


@runtime_checkable
class OnUpdateAttendeeOnEventHandler(Protocol):
    def on_update_attendee_on_event(
        self,
        actor: User | CalendarManagementToken | SystemUser | None,
        event: CalendarEventData,
        attendance: EventExternalAttendeeData | EventExternalAttendeeData,
        organization: Organization,
    ) -> None:
        ...


class CalendarSideEffectsService:
    def __init__(
        self,
        side_effects_pipeline: Iterable[
            OnCreateEventHandler
            | OnUpdateEventHandler
            | OnDeleteEventHandler
            | OnAddAttendeeToEventHandler
            | OnRemoveAttendeeFromEventHandler
            | OnUpdateAttendeeOnEventHandler
        ],
    ):
        self.side_effects_pipeline = side_effects_pipeline

    def on_create_event(
        self,
        actor: User | CalendarManagementToken | SystemUser | None,
        event: CalendarEventData,
        organization: Organization,
    ) -> None:
        """Handle side effects when a calendar event is created."""
        for handler in self.side_effects_pipeline:
            if isinstance(handler, OnCreateEventHandler):
                handler.on_create_event(actor, event, organization)

    def on_update_event(
        self,
        actor: User | CalendarManagementToken | SystemUser | None,
        event: CalendarEventData,
        organization: Organization,
    ) -> None:
        """Handle side effects when a calendar event is updated."""
        for handler in self.side_effects_pipeline:
            if isinstance(handler, OnUpdateEventHandler):
                handler.on_update_event(actor, event, organization)

    def on_delete_event(
        self,
        actor: User | CalendarManagementToken | SystemUser | None,
        event: CalendarEventData,
        organization: Organization,
    ) -> None:
        """Handle side effects when a calendar event is deleted."""
        for handler in self.side_effects_pipeline:
            if isinstance(handler, OnDeleteEventHandler):
                handler.on_delete_event(actor, event, organization)

    def on_add_attendee_to_event(
        self,
        actor: User | CalendarManagementToken | SystemUser | None,
        event: CalendarEventData,
        attendee: EventExternalAttendeeData | EventExternalAttendeeData,
        organization: Organization,
    ) -> None:
        """Handle side effects when an attendee is added to an event."""
        for handler in self.side_effects_pipeline:
            if isinstance(handler, OnAddAttendeeToEventHandler):
                handler.on_add_attendee_to_event(actor, event, attendee, organization)

    def on_remove_attendee_from_event(
        self,
        actor: User | CalendarManagementToken | SystemUser | None,
        event: CalendarEventData,
        attendee: EventExternalAttendeeData | EventExternalAttendeeData,
        organization: Organization,
    ) -> None:
        """Handle side effects when an attendee is removed from an event."""
        for handler in self.side_effects_pipeline:
            if isinstance(handler, OnRemoveAttendeeFromEventHandler):
                handler.on_remove_attendee_from_event(actor, event, attendee, organization)

    def on_update_attendee_on_event(
        self,
        actor: User | CalendarManagementToken | SystemUser | None,
        event: CalendarEventData,
        attendee: EventExternalAttendeeData | EventExternalAttendeeData,
        organization: Organization,
    ) -> None:
        """Handle side effects when an attendee on an event is updated."""
        for handler in self.side_effects_pipeline:
            if isinstance(handler, OnUpdateAttendeeOnEventHandler):
                handler.on_update_attendee_on_event(actor, event, attendee, organization)
