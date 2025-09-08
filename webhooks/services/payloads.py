from typing import TypedDict


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
