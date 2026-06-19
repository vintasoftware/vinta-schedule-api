from typing import Any, TypedDict


class CalendarEventWebhookPayload(TypedDict):
    id: int
    calendar_id: int
    is_recurring: bool
    recurring_event_id: str | None
    start_time: str
    end_time: str
    timezone: str
    title: str
    description: str | None


class EventAttendeeWebhookPayload(TypedDict):
    email: str
    name: str | None
    status: str
    user_id: int | None
    event: CalendarEventWebhookPayload


class OrganizationMemberCreatedWebhookPayload(TypedDict):
    user_id: int
    email: str
    organization_id: int
    organization_name: str
    membership_role: str
    membership_id: int


class WebhookEnvelope(TypedDict):
    id: str
    type: str
    timestamp: str
    data: dict[str, Any]
