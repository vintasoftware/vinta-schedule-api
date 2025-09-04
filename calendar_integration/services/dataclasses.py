import datetime
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Literal, TypedDict

from calendar_integration.constants import (
    CalendarProvider,
)
from calendar_integration.models import (
    BlockedTime,
    CalendarEvent,
    EventAttendance,
    EventExternalAttendance,
)


@dataclass
class EventAttendeeData:
    email: str
    name: str
    status: Literal["accepted", "declined", "pending"]


@dataclass
class ResourceData:
    email: str
    title: str
    external_id: str | None = None
    status: Literal["accepted", "declined", "pending"] | None = None


@dataclass
class EventAttendanceInputData:
    user_id: int


@dataclass
class ExternalAttendeeInputData:
    email: str
    name: str = ""
    id: int | None = None  # noqa: A003


@dataclass
class EventExternalAttendanceInputData:
    external_attendee: ExternalAttendeeInputData


@dataclass
class ResourceAllocationInputData:
    resource_id: int


@dataclass
class CalendarEventInputData:
    title: str
    description: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str  # IANA timezone string (required)
    attendances: list[EventAttendanceInputData] = dataclass_field(default_factory=list)
    external_attendances: list[EventExternalAttendanceInputData] = dataclass_field(
        default_factory=list
    )
    resource_allocations: list[ResourceAllocationInputData] = dataclass_field(default_factory=list)
    # Recurrence fields
    recurrence_rule: str | None = None  # RRULE string
    parent_event_id: int | None = None  # For creating instances/exceptions
    is_recurring_exception: bool = False


@dataclass
class CalendarEventAdapterInputData:
    calendar_external_id: str
    title: str
    description: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str  # IANA timezone string (required)
    attendees: list[EventAttendeeData]
    resources: list[ResourceData] = dataclass_field(default_factory=list)
    original_payload: dict | None = None
    # Recurrence fields
    recurrence_rule: str | None = None  # RRULE string for creating recurring events
    is_recurring_instance: bool = False  # True if this is a single instance of a recurring event


@dataclass
class CalendarEventData:
    calendar_external_id: str
    title: str
    description: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str  # IANA timezone string (required)
    attendees: list[EventAttendeeData]
    external_id: str
    status: Literal["confirmed", "cancelled"] = "confirmed"
    original_payload: dict | None = None
    id: int | None = None  # noqa: A003
    resources: list[ResourceData] = dataclass_field(default_factory=list)
    # Recurrence fields
    recurrence_rule: str | None = None  # RRULE string
    recurring_event_id: str | None = None  # ID of the master recurring event


@dataclass
class CalendarResourceData:
    name: str
    description: str
    provider: str
    external_id: str
    email: str | None = None
    capacity: int | None = None
    original_payload: dict | None = None
    is_default: bool = False


@dataclass
class EventsSyncChanges:
    events_to_update: list[CalendarEvent] = dataclass_field(default_factory=list)
    events_to_create: list[CalendarEvent] = dataclass_field(default_factory=list)
    blocked_times_to_create: list[BlockedTime] = dataclass_field(default_factory=list)
    blocked_times_to_update: list[BlockedTime] = dataclass_field(default_factory=list)
    attendances_to_create: list[EventAttendance] = dataclass_field(default_factory=list)
    external_attendances_to_create: list[EventExternalAttendance] = dataclass_field(
        default_factory=list
    )
    events_to_delete: list[str] = dataclass_field(default_factory=list)
    blocks_to_delete: list[str] = dataclass_field(default_factory=list)
    matched_event_ids: set[str] = dataclass_field(default_factory=set)
    # New fields for recurring events
    recurrence_rules_to_create: list = dataclass_field(
        default_factory=list
    )  # RecurrenceRule objects


@dataclass
class ApplicationCalendarData:
    id: int | None  # noqa: A003
    organization_id: int | None
    external_id: str
    name: str
    description: str | None = None
    email: str | None = None
    provider: CalendarProvider = CalendarProvider.GOOGLE
    original_payload: dict | None = None


class CalendarEventsSyncTypedDict(TypedDict):
    events: Iterable[CalendarEventData]
    next_sync_token: str | None


@dataclass
class AvailableTimeWindow:
    start_time: datetime.datetime
    end_time: datetime.datetime
    id: int | None = None  # noqa: A003
    can_book_partially: bool = False


@dataclass
class BlockedTimeData:
    id: int | None  # noqa: A003
    calendar_external_id: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str  # IANA timezone string (required)
    reason: str
    external_id: str | None
    meta: dict | None


@dataclass
class UnavailableTimeWindow:
    start_time: datetime.datetime
    end_time: datetime.datetime
    reason: Literal["blocked_time"] | Literal["calendar_event"]
    id: int  # noqa: A003
    data: BlockedTimeData | CalendarEventData


@dataclass
class RecurringObjectInputData:
    """Base class for recurring object input data."""

    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str  # IANA timezone string (required)
    recurrence_rule: str | None = None
    parent_object_id: int | None = None
    is_recurring_exception: bool = False


@dataclass
class BlockedTimeInputData:
    """Input data for creating blocked times."""

    calendar_id: int
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str  # IANA timezone string (required)
    reason: str = ""
    external_id: str = ""
    recurrence_rule: str | None = None
    parent_object_id: int | None = None
    is_recurring_exception: bool = False


@dataclass
class AvailableTimeInputData:
    """Input data for creating available times."""

    calendar_id: int
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str  # IANA timezone string (required)
    recurrence_rule: str | None = None
    parent_object_id: int | None = None
    is_recurring_exception: bool = False
