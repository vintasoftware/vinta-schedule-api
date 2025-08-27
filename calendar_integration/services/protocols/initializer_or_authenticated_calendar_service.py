import datetime
from collections.abc import Iterable
from typing import Protocol

from allauth.socialaccount.models import SocialAccount

from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    GoogleCalendarServiceAccount,
    Organization,
)
from calendar_integration.services.dataclasses import (
    AvailableTimeWindow,
    CalendarEventInputData,
    EventAttendanceInputData,
    EventExternalAttendanceInputData,
    ResourceAllocationInputData,
    UnavailableTimeWindow,
)
from calendar_integration.services.protocols.calendar_adapter import CalendarAdapter


class InitializedOrAuthenticatedCalendarService(Protocol):
    organization: Organization
    account: SocialAccount | GoogleCalendarServiceAccount | None
    calendar_adapter: CalendarAdapter | None

    def _get_calendar_by_id(self, calendar_id: str) -> Calendar:
        ...

    def _create_bundle_event(
        self, bundle_calendar: Calendar, event_data: "CalendarEventInputData"
    ) -> CalendarEvent:
        ...

    def create_event(self, calendar_id: str, event_data: CalendarEventInputData) -> CalendarEvent:
        ...

    def _update_bundle_event(
        self, bundle_event: CalendarEvent, event_data: "CalendarEventInputData"
    ) -> CalendarEvent:
        ...

    def update_event(
        self, calendar_id: str, event_id: str, event_data: CalendarEventInputData
    ) -> CalendarEvent:
        ...

    def _delete_bundle_event(self, bundle_event: CalendarEvent) -> None:
        ...

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        ...

    def get_unavailable_time_windows_in_range(
        self,
        calendar: Calendar,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
    ) -> list[UnavailableTimeWindow]:
        ...

    def get_availability_windows_in_range(
        self, calendar: Calendar, start_datetime: datetime.datetime, end_datetime: datetime.datetime
    ) -> Iterable[AvailableTimeWindow]:
        ...

    def bulk_create_availability_windows(
        self,
        calendar: Calendar,
        availability_windows: Iterable[tuple[datetime.datetime, datetime.datetime]],
    ) -> Iterable[AvailableTime]:
        ...

    def bulk_create_manual_blocked_times(
        self,
        calendar: Calendar,
        blocked_times: Iterable[tuple[datetime.datetime, datetime.datetime, str]],
    ) -> Iterable[BlockedTime]:
        ...

    def create_recurring_event(
        self,
        calendar_id: str,
        title: str,
        description: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        recurrence_rule: str,
        attendances: list[EventAttendanceInputData] | None = None,
        external_attendances: list[EventExternalAttendanceInputData] | None = None,
        resource_allocations: list[ResourceAllocationInputData] | None = None,
    ) -> CalendarEvent:
        ...

    def create_recurring_exception(
        self,
        parent_event: CalendarEvent,
        exception_date: datetime.datetime,
        modified_title: str | None = None,
        modified_description: str | None = None,
        modified_start_time: datetime.datetime | None = None,
        modified_end_time: datetime.datetime | None = None,
        is_cancelled: bool = False,
    ) -> CalendarEvent | None:
        ...

    def get_calendar_events_expanded(
        self,
        calendar: Calendar,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[CalendarEvent]:
        ...

    def _get_primary_calendar(self, bundle_calendar: Calendar) -> Calendar:
        ...

    def _collect_bundle_attendees(
        self, child_calendars: list[Calendar], event_data: "CalendarEventInputData"
    ) -> list["EventAttendanceInputData"]:
        ...
