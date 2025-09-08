import datetime
from collections.abc import Iterable
from typing import Protocol

from django.contrib.auth import get_user_model

from calendar_integration.models import (
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarOrganizationResourcesImport,
    CalendarSync,
    GoogleCalendarServiceAccount,
    Organization,
)
from calendar_integration.services.dataclasses import (
    ApplicationCalendarData,
    CalendarEventAdapterOutputData,
    CalendarResourceData,
    EventsSyncChanges,
)
from calendar_integration.services.protocols.calendar_adapter import CalendarAdapter
from calendar_integration.services.protocols.initializer_or_authenticated_calendar_service import (
    InitializedOrAuthenticatedCalendarService,
)


User = get_user_model()


class AuthenticatedCalendarService(InitializedOrAuthenticatedCalendarService, Protocol):
    organization: Organization
    account: User | GoogleCalendarServiceAccount
    calendar_adapter: CalendarAdapter

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

    def import_account_calendars(self):
        ...

    def create_application_calendar(
        self, name: str, organization: Organization
    ) -> ApplicationCalendarData:
        ...

    def _get_calendar_by_external_id(self, calendar_external_id: str) -> Calendar:
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

    def _execute_calendar_sync(
        self,
        calendar_sync: CalendarSync,
        sync_token: str | None = None,
    ) -> None:
        ...

    def _execute_organization_calendar_resources_import(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
    ) -> Iterable[CalendarResourceData]:
        ...

    def _get_existing_calendar_data(
        self, calendar_id: int, start_date: datetime.datetime, end_date: datetime.datetime
    ):
        ...

    def _process_events_for_sync(
        self,
        events: Iterable[CalendarEventAdapterOutputData],
        calendar_events_by_external_id: dict,
        blocked_times_by_external_id: dict,
        calendar: Calendar,
        update_events: bool,
    ) -> EventsSyncChanges:
        ...

    def _process_existing_event(
        self,
        event: CalendarEventAdapterOutputData,
        existing_event: CalendarEvent,
        changes: EventsSyncChanges,
        update_events: bool,
    ):
        ...

    def _process_existing_blocked_time(
        self,
        event: CalendarEventAdapterOutputData,
        existing_blocked_time: BlockedTime,
        changes: EventsSyncChanges,
    ):
        ...

    def _process_new_event(
        self, event: CalendarEventAdapterOutputData, calendar: Calendar, changes: EventsSyncChanges
    ):
        ...

    def _process_event_attendees(
        self,
        event: CalendarEventAdapterOutputData,
        existing_event: CalendarEvent,
        changes: EventsSyncChanges,
    ):
        ...

    def _handle_deletions_for_full_sync(
        self,
        calendar_id: int,
        calendar_events_by_external_id: dict,
        matched_event_ids: set[str],
        start_date: datetime.datetime,
    ):
        ...

    def _apply_sync_changes(self, calendar_id: int, changes: EventsSyncChanges):
        ...

    def _link_orphaned_recurring_instances(self, calendar_id: int):
        ...

    def _remove_available_time_windows_that_overlap_with_blocked_times_and_events(
        self,
        calendar_id: int,
        blocked_times: Iterable[BlockedTime],
        events: Iterable[CalendarEvent],
        start_time: datetime.datetime,
        end_time: datetime.datetime,
    ):
        ...
