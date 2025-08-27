import datetime
from collections.abc import Iterable
from typing import Protocol

from allauth.socialaccount.models import SocialAccount

from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarOrganizationResourcesImport,
    CalendarSync,
    GoogleCalendarServiceAccount,
)
from calendar_integration.services.dataclasses import (
    ApplicationCalendarData,
    AvailableTimeWindow,
    CalendarEventInputData,
    CalendarResourceData,
    UnavailableTimeWindow,
)
from calendar_integration.services.protocols.calendar_adapter import CalendarAdapter
from organizations.models import Organization


class BaseCalendarService(Protocol):
    @staticmethod
    def get_calendar_adapter_for_account(
        account: SocialAccount | GoogleCalendarServiceAccount,
    ) -> CalendarAdapter:
        ...

    def authenticate(
        self,
        account: SocialAccount | GoogleCalendarServiceAccount,
        organization: Organization,
    ) -> None:
        ...

    def import_account_calendars(self):
        ...

    def request_organization_calendar_resources_import(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
    ) -> None:
        ...

    def import_organization_calendar_resources(
        self,
        import_workflow_state: CalendarOrganizationResourcesImport,
    ) -> None:
        ...

    def create_application_calendar(
        self, name: str, organization: Organization
    ) -> ApplicationCalendarData:
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

    def transfer_event(self, event: CalendarEvent, new_calendar: Calendar) -> CalendarEvent:
        ...

    def request_calendar_sync(
        self,
        calendar: Calendar,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
        should_update_events: bool = False,
    ) -> CalendarSync:
        ...

    def sync_events(
        self,
        calendar_sync: CalendarSync,
    ) -> None:
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

    def _execute_organization_calendar_resources_import(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
    ) -> Iterable[CalendarResourceData]:
        ...

    def _get_calendar_by_external_id(self, calendar_external_id: str) -> Calendar:
        ...

    def _execute_calendar_sync(
        self,
        calendar_sync: CalendarSync,
        sync_token: str | None = None,
    ) -> None:
        ...

    def get_calendar_events_expanded(
        self,
        calendar: Calendar,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[CalendarEvent]:
        ...
