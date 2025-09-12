import datetime
from collections.abc import Iterable
from typing import Protocol

from calendar_integration.services.dataclasses import (
    ApplicationCalendarData,
    CalendarEventAdapterInputData,
    CalendarEventAdapterOutputData,
    CalendarEventsSyncTypedDict,
    CalendarResourceData,
)


class CalendarAdapter(Protocol):
    provider: str

    def __init__(self, credentials: dict | None = None):
        ...

    def create_application_calendar(self, name: str) -> ApplicationCalendarData:
        """
        Create a new application calendar.
        :param calendar_name: Name of the calendar to create.
        :return: Created Calendar instance.
        """
        ...

    def create_event(
        self, event_data: CalendarEventAdapterInputData
    ) -> CalendarEventAdapterOutputData:
        """
        Create a new event in the calendar.
        :param event_data: Dictionary containing event details.
        :return: Response from the calendar client.
        """
        ...

    def get_events(
        self,
        calendar_id: str,
        calendar_is_resource: bool,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        sync_token: str | None = None,
        max_results_per_page: int = 250,
    ) -> CalendarEventsSyncTypedDict:
        """
        Retrieve events within a specified date range.
        :param start_date: Start date for the event search.
        :param end_date: End date for the event search.
        :return: CalendarEventsSyncTypedDict.
        """
        ...

    def get_event(self, calendar_id: str, event_id: str) -> CalendarEventAdapterOutputData:
        """
        Retrieve a specific event by its unique identifier.
        :param event_id: Unique identifier of the event to retrieve.
        :return: Event details if found, otherwise None.
        """
        ...

    def update_event(
        self, calendar_id: str, event_id: str, event_data: CalendarEventAdapterInputData
    ) -> CalendarEventAdapterOutputData:
        """
        Update an existing event in the calendar.
        :param event_id: Unique identifier of the event to update.
        :param event_data: Dictionary containing updated event details.
        :return: Response from the calendar client.
        """
        ...

    def delete_event(self, calendar_id: str, event_id: str):
        """
        Delete an event from the calendar.
        :param event_id: Unique identifier of the event to delete.
        :return: Response from the calendar client.
        """
        ...

    def get_account_calendars(self) -> Iterable[CalendarResourceData]:
        """
        Retrieve account account calendar.
        """
        ...

    def get_calendar_resources(self) -> Iterable[CalendarResourceData]:
        """
        Retrieve resources associated with the calendar.
        :return: List of resources.
        """
        ...

    def get_calendar_resource(self, resource_id: str) -> CalendarResourceData:
        """
        Retrieve a specific calendar resource by its unique identifier.
        :param resource_id: Unique identifier of the resource to retrieve.
        :return: Resource details if found, otherwise None.
        """
        ...

    def get_available_calendar_resources(
        self, start_time: datetime.datetime, end_time: datetime.datetime
    ) -> Iterable[CalendarResourceData]:
        """
        Retrieve available calendar resources within a specified time range.
        :param start_time: Start time for the availability check.
        :param end_time: End time for the availability check.
        :return: List of available resources.
        """
        ...

    def subscribe_to_calendar_events(self, resource_id: str, callback_url: str) -> None:
        ...
