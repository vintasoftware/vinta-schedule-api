import datetime

import strawberry
import strawberry_django

from calendar_integration.models import (
    AvailableTime,
    AvailableTimeRecurrenceException,
    BlockedTime,
    BlockedTimeRecurrenceException,
    Calendar,
    CalendarEvent,
    EventAttendance,
    EventExternalAttendance,
    EventRecurrenceException,
    ExternalAttendee,
    RecurrenceRule,
    ResourceAllocation,
)
from users.graphql import UserGraphQLType


@strawberry_django.type(Calendar)
class CalendarGraphQLType:
    id: strawberry.auto  # noqa: A003
    name: strawberry.auto
    description: strawberry.auto
    email: strawberry.auto
    external_id: strawberry.auto
    provider: strawberry.auto
    calendar_type: strawberry.auto
    capacity: strawberry.auto
    manage_available_windows: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime


@strawberry_django.type(RecurrenceRule)
class RecurrenceRuleGraphQLType:
    id: strawberry.auto  # noqa: A003
    frequency: strawberry.auto
    interval: strawberry.auto
    count: strawberry.auto
    until: strawberry.auto
    by_weekday: strawberry.auto
    by_month_day: strawberry.auto
    by_month: strawberry.auto
    by_year_day: strawberry.auto
    by_week_number: strawberry.auto
    by_hour: strawberry.auto
    by_minute: strawberry.auto
    by_second: strawberry.auto
    week_start: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    @strawberry.field
    def rrule_string(self) -> str:
        return self.to_rrule_string()  # type: ignore


@strawberry_django.type(ExternalAttendee)
class ExternalAttendeeGraphQLType:
    id: strawberry.auto  # noqa: A003
    name: strawberry.auto
    email: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime


@strawberry_django.type(EventAttendance)
class EventAttendanceGraphQLType:
    id: strawberry.auto  # noqa: A003
    status: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    user: UserGraphQLType = strawberry_django.field()


@strawberry_django.type(EventExternalAttendance)
class EventExternalAttendanceGraphQLType:
    id: strawberry.auto  # noqa: A003
    status: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    event: "CalendarEventGraphQLType" = strawberry_django.field()
    external_attendee: ExternalAttendeeGraphQLType = strawberry_django.field()


@strawberry_django.type(ResourceAllocation)
class ResourceAllocationGraphQLType:
    id: strawberry.auto  # noqa: A003
    status: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    calendar: CalendarGraphQLType = strawberry_django.field()


@strawberry_django.type(EventRecurrenceException)
class EventRecurrenceExceptionGraphQLType:
    id: strawberry.auto  # noqa: A003
    exception_date: strawberry.auto
    is_cancelled: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    parent_event: "CalendarEventGraphQLType" = strawberry_django.field()
    modified_event: "CalendarEventGraphQLType" = strawberry_django.field()


@strawberry_django.type(CalendarEvent)
class CalendarEventGraphQLType:
    id: strawberry.auto  # noqa: A003
    title: strawberry.auto
    description: strawberry.auto
    external_id: strawberry.auto
    start_time: strawberry.auto
    end_time: strawberry.auto
    recurrence_id: strawberry.auto
    is_recurring_exception: strawberry.auto
    is_bundle_primary: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    # Relationships
    calendar: CalendarGraphQLType = strawberry_django.field()
    bundle_calendar: CalendarGraphQLType = strawberry_django.field()
    bundle_primary_event: "CalendarEventGraphQLType" = strawberry_django.field()
    bulk_modification_parent: "CalendarEventGraphQLType" = strawberry_django.field()
    parent_recurring_object: "CalendarEventGraphQLType" = strawberry_django.field()
    recurrence_rule: RecurrenceRuleGraphQLType = strawberry_django.field()

    # Many-to-many relationships through intermediary models
    attendances: list[EventAttendanceGraphQLType] = strawberry_django.field()
    external_attendances: list[EventExternalAttendanceGraphQLType] = strawberry_django.field()
    resource_allocations: list[ResourceAllocationGraphQLType] = strawberry_django.field()
    recurrence_exceptions: list[EventRecurrenceExceptionGraphQLType] = strawberry_django.field()

    # Direct many-to-many relationships (simplified access)
    attendees: list[UserGraphQLType] = strawberry_django.field()
    external_attendees: list[ExternalAttendeeGraphQLType] = strawberry_django.field()
    resources: list[CalendarGraphQLType] = strawberry_django.field()

    # Bundle representations - events that represent this primary event in child calendars
    bundle_representations: list["CalendarEventGraphQLType"] = strawberry_django.field()

    # Bulk modifications - continuation events created by bulk modifications
    bulk_modifications: list["CalendarEventGraphQLType"] = strawberry_django.field()

    # Recurring instances - individual instances of this recurring event
    recurring_instances: list["CalendarEventGraphQLType"] = strawberry_django.field()

    # Properties
    @strawberry.field
    def is_recurring(self) -> bool:
        return self.is_recurring  # type: ignore

    @strawberry.field
    def is_recurring_instance(self) -> bool:
        return self.is_recurring_instance  # type: ignore

    @strawberry.field
    def is_bundle_event(self) -> bool:
        return self.is_bundle_event  # type: ignore

    @strawberry.field
    def is_bundle_representation(self) -> bool:
        return self.is_bundle_representation  # type: ignore

    @strawberry.field
    def duration_seconds(self) -> int:
        """Duration of the event in seconds"""
        return int(self.duration.total_seconds())  # type: ignore


@strawberry_django.type(BlockedTimeRecurrenceException)
class BlockedTimeRecurringExceptionGraphQLType:
    parent_blocked_time: "BlockedTimeGraphQLType" = strawberry_django.field()
    modified_blocked_time: "BlockedTimeGraphQLType" = strawberry_django.field()
    id: strawberry.auto  # noqa: A003
    exception_date: strawberry.auto
    is_cancelled: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime


@strawberry_django.type(BlockedTime)
class BlockedTimeGraphQLType:
    id: strawberry.auto  # noqa: A003
    start_time: strawberry.auto
    end_time: strawberry.auto
    external_id: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    user: UserGraphQLType = strawberry_django.field()
    calendar: CalendarGraphQLType = strawberry_django.field()
    recurrence_rule: RecurrenceRuleGraphQLType = strawberry_django.field()
    recurrence_exceptions: list[
        BlockedTimeRecurringExceptionGraphQLType
    ] = strawberry_django.field()


@strawberry_django.type(AvailableTimeRecurrenceException)
class AvailableTimeRecurringExceptionGraphQLType:
    parent_available_time: "AvailableTimeGraphQLType" = strawberry_django.field()
    modified_available_time: "AvailableTimeGraphQLType" = strawberry_django.field()
    id: strawberry.auto  # noqa: A003
    exception_date: strawberry.auto
    is_cancelled: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime


@strawberry_django.type(AvailableTime)
class AvailableTimeGraphQLType:
    id: strawberry.auto  # noqa: A003
    start_time: strawberry.auto
    end_time: strawberry.auto
    created: datetime.datetime
    modified: datetime.datetime

    user: UserGraphQLType = strawberry_django.field()
    calendar: CalendarGraphQLType = strawberry_django.field()
    recurrence_rule: RecurrenceRuleGraphQLType = strawberry_django.field()
    recurrence_exceptions: list[
        AvailableTimeRecurringExceptionGraphQLType
    ] = strawberry_django.field()


@strawberry.type
class AvailableTimeWindowGraphQLType:
    start_time: datetime.datetime
    end_time: datetime.datetime
    id: int | None  # noqa: A003
    can_book_partially: bool
