import datetime
from collections.abc import Iterable
from typing import Protocol

from allauth.socialaccount.models import SocialAccount

from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarSync,
    CalendarWebhookEvent,
    EventAttendance,
    EventExternalAttendance,
    GoogleCalendarServiceAccount,
    Organization,
    RecurrenceRule,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.calendar_side_effects_service import CalendarSideEffectsService
from calendar_integration.services.dataclasses import (
    AvailableTimeWindow,
    CalendarEventData,
    CalendarEventInputData,
    EventAttendanceInputData,
    EventExternalAttendanceInputData,
    EventExternalAttendeeData,
    EventInternalAttendeeData,
    ResourceAllocationInputData,
    UnavailableTimeWindow,
)
from calendar_integration.services.protocols.calendar_adapter import CalendarAdapter
from public_api.models import SystemUser
from users.models import User


class InitializedOrAuthenticatedCalendarService(Protocol):
    organization: Organization
    account: SocialAccount | GoogleCalendarServiceAccount | None
    user_or_token: User | str | SystemUser | None
    calendar_adapter: CalendarAdapter | None
    calendar_side_effects_service: CalendarSideEffectsService
    calendar_permission_service: CalendarPermissionService

    def _get_calendar_by_id(self, calendar_id: int) -> Calendar:
        ...

    def _create_bundle_event(
        self, bundle_calendar: Calendar, event_data: "CalendarEventInputData"
    ) -> CalendarEvent:
        ...

    def _get_write_adapter_for_calendar(self, calendar: Calendar) -> CalendarAdapter | None:
        ...

    def convert_naive_utc_datetime_to_timezone(
        self, naive_utc_datetime: datetime.datetime, timezone_str: str
    ) -> datetime.datetime:
        ...

    def create_event(self, calendar_id: int, event_data: CalendarEventInputData) -> CalendarEvent:
        ...

    def _update_bundle_event(
        self, bundle_event: CalendarEvent, event_data: "CalendarEventInputData"
    ) -> CalendarEvent:
        ...

    def update_event(
        self, calendar_id: str, event_id: int, event_data: CalendarEventInputData
    ) -> CalendarEvent:
        ...

    def _delete_bundle_event(self, bundle_event: CalendarEvent) -> None:
        ...

    def delete_event(self, calendar_id: str, event_id: int) -> None:
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

    def _create_recurrence_rule_if_needed(self, rrule_string: str | None) -> RecurrenceRule | None:
        ...

    def get_blocked_times_expanded(
        self,
        calendar: Calendar,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[BlockedTime]:
        ...

    def get_available_times_expanded(
        self,
        calendar: Calendar,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[AvailableTime]:
        ...

    def create_recurring_blocked_time_exception(
        self,
        parent_blocked_time: BlockedTime,
        exception_date: datetime.date,
        modified_reason: str | None = None,
        modified_start_time: datetime.datetime | None = None,
        modified_end_time: datetime.datetime | None = None,
        is_cancelled: bool = False,
    ) -> BlockedTime | None:
        ...

    def create_recurring_available_time_exception(
        self,
        parent_available_time: AvailableTime,
        exception_date: datetime.date,
        modified_start_time: datetime.datetime | None = None,
        modified_end_time: datetime.datetime | None = None,
        is_cancelled: bool = False,
    ) -> AvailableTime | None:
        ...

    def create_blocked_time(
        self,
        calendar: Calendar,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        reason: str = "",
        rrule_string: str | None = None,
    ) -> BlockedTime:
        ...

    def create_available_time(
        self,
        calendar: Calendar,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        rrule_string: str | None = None,
    ) -> AvailableTime:
        ...

    def _serialize_event(self, event: CalendarEvent) -> CalendarEventData:
        ...

    def _serialize_event_internal_attendee(
        self, attendance: EventAttendance
    ) -> EventInternalAttendeeData:
        ...

    def _serialize_event_external_attendee(
        self, external_attendance: EventExternalAttendance
    ) -> EventExternalAttendeeData:
        ...

    def _serialize_event_data_input(
        self, event: CalendarEvent, event_data: CalendarEventInputData
    ) -> CalendarEventData:
        ...

    def _grant_calendar_owner_permissions(self, calendar: Calendar) -> None:
        ...

    def _grant_event_attendee_permissions(self, event: CalendarEvent) -> None:
        ...

    def request_calendar_sync(
        self,
        calendar: Calendar,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
        should_update_events: bool,
    ) -> CalendarSync:
        ...

    def request_webhook_triggered_sync(
        self, external_calendar_id: str, webhook_event: CalendarWebhookEvent
    ) -> CalendarSync:
        ...
