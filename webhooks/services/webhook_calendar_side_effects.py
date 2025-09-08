from typing import TYPE_CHECKING, Annotated

from dependency_injector.wiring import Provide, inject

from calendar_integration.services.dataclasses import (
    CalendarEventData,
    EventExternalAttendeeData,
    EventInternalAttendeeData,
)
from organizations.models import Organization
from webhooks.constants import WebhookEventType
from webhooks.services.payloads import CalendarEventWebhookPayload, EventAttendeeWebhookPayload


if TYPE_CHECKING:
    from webhooks.services import WebhookService


class WebhookCalendarEventSideEffectsService:
    @inject
    def __init__(self, webhook_service: Annotated["WebhookService", Provide["webhook_service"]]):
        self.webhook_service = webhook_service

    def _serialize_event(self, event: CalendarEventData) -> CalendarEventWebhookPayload:
        return {
            "id": event.id,
            "calendar_id": event.calendar_id,
            "is_recurring": event.is_recurring,
            "recurring_event_id": event.recurring_event_id,
            "start_time": event.start_time.isoformat(),
            "end_time": event.end_time.isoformat(),
            "timezone": event.timezone,
            "title": event.title,
            "description": event.description,
        }

    def _serialize_attendee(
        self,
        event: CalendarEventData,
        attendance: EventInternalAttendeeData | EventExternalAttendeeData,
    ) -> EventAttendeeWebhookPayload:
        return {
            "email": attendance.email,
            "name": attendance.name,
            "status": attendance.status,
            "user_id": getattr(attendance, "user_id", None),
            "event": self._serialize_event(event),
        }

    def on_create_event(self, event: CalendarEventData, organization: Organization) -> None:
        self.webhook_service.send_event(
            organization=organization,
            event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
            payload=dict(self._serialize_event(event)),
        )

    def on_update_event(self, event: CalendarEventData, organization: Organization) -> None:
        self.webhook_service.send_event(
            organization=organization,
            event_type=WebhookEventType.CALENDAR_EVENT_UPDATED,
            payload=dict(self._serialize_event(event)),
        )

    def on_delete_event(self, event: CalendarEventData, organization: Organization) -> None:
        self.webhook_service.send_event(
            organization=organization,
            event_type=WebhookEventType.CALENDAR_EVENT_DELETED,
            payload=dict(self._serialize_event(event)),
        )

    def on_add_attendee_to_event(
        self,
        event: CalendarEventData,
        attendance: EventInternalAttendeeData | EventExternalAttendeeData,
        organization: Organization,
    ) -> None:
        self.webhook_service.send_event(
            organization=organization,
            event_type=WebhookEventType.CALENDAR_EVENT_ATTENDEE_ADDED,
            payload=dict(self._serialize_attendee(event, attendance)),
        )

    def on_remove_attendee_from_event(
        self,
        event: CalendarEventData,
        attendance: EventInternalAttendeeData | EventExternalAttendeeData,
        organization: Organization,
    ) -> None:
        self.webhook_service.send_event(
            organization=organization,
            event_type=WebhookEventType.CALENDAR_EVENT_ATTENDEE_REMOVED,
            payload=dict(self._serialize_attendee(event, attendance)),
        )

    def on_update_attendee_in_event(
        self,
        event: CalendarEventData,
        attendance: EventInternalAttendeeData | EventExternalAttendeeData,
        organization: Organization,
    ) -> None:
        self.webhook_service.send_event(
            organization=organization,
            event_type=WebhookEventType.CALENDAR_EVENT_ATTENDEE_UPDATED,
            payload=dict(self._serialize_attendee(event, attendance)),
        )
