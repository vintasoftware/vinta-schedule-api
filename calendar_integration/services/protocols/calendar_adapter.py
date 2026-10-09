import datetime
from collections.abc import Iterable
from typing import Any, Protocol

from django.http import HttpHeaders, HttpRequest

from calendar_integration.constants import BlockedTimeDeclineScope
from calendar_integration.services.dataclasses import (
    ApplicationCalendarData,
    CalendarEventAdapterInputData,
    CalendarEventAdapterOutputData,
    CalendarEventsSyncTypedDict,
    CalendarResourceData,
)


class CalendarAdapter(Protocol):
    provider: str
    #: Whether blocked times can be written to this provider's calendars as events.
    supports_blocked_time_push: bool

    def __init__(self, credentials: dict | None = None): ...

    @staticmethod
    def parse_webhook_headers(headers: HttpHeaders) -> dict[str, str]: ...

    @staticmethod
    def extract_calendar_external_id_from_webhook_request(request: HttpRequest) -> str: ...

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

    def delete_event(self, calendar_id: str, event_id: str, send_updates: str | None = None):
        """
        Delete an event from the calendar.
        :param event_id: Unique identifier of the event to delete.
        :param send_updates: Who the provider emails about the delete (Google's
            ``sendUpdates``, e.g. ``"all"``). None keeps the provider's default.
        :return: Response from the calendar client.
        """
        ...

    def write_out_of_office_event(
        self,
        calendar_id: str,
        event_id: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        timezone: str,
        decline_scope: BlockedTimeDeclineScope,
    ) -> str:
        """
        Create or update a blocked time's Out of office event, which declines conflicting
        invitations with the provider's default reply. The title is a fixed, neutral one;
        the block's reason and decline message are never sent.
        :param event_id: The id of the provider event. When an event with this id exists it
            is updated, so a repeated write never creates a second event.
        :param decline_scope: Which invitations to decline.
        :return: The provider event id.
        :raises OutOfOfficeNotSupportedError: When the calendar refuses Out of office events.
        """
        ...

    def write_busy_event(
        self,
        calendar_id: str,
        event_id: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        timezone: str,
    ) -> str:
        """
        Create or update a blocked time's plain event that shows the time as busy. The
        title is a fixed, neutral one.
        :param event_id: The id of the provider event (see ``write_out_of_office_event``).
        :return: The provider event id.
        """
        ...

    def delete_blocked_time_event(self, calendar_id: str, event_id: str) -> None:
        """
        Delete a blocked time's provider event without notifying anyone. An event the
        provider no longer has counts as deleted.
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

    def subscribe_to_calendar_events(self, resource_id: str, callback_url: str) -> None: ...

    def create_webhook_subscription_with_tracking(
        self,
        resource_id: str,
        callback_url: str,
        tracking_params: dict | None = None,
    ) -> dict[str, Any]:
        """
        Create a webhook subscription with tracking parameters.
        :param organization_id: ID of the organization.
        :param resource_id: ID of the calendar resource to subscribe to.
        :param callback_url: URL to receive webhook notifications.
        :param tracking_params: Optional dictionary of tracking parameters.
        :return: A dict.
        """
        ...

    def stop_webhook_subscription(self, subscription_id: str, resource_id: str) -> None:
        """
        Stop a webhook subscription at the provider so it sends no more notifications.
        :param subscription_id: The provider's id for the subscription (a Google channel id).
        :param resource_id: The provider's id for the watched resource, where it needs one.
        """
        ...

    def validate_webhook_notification(
        self,
        headers: dict[str, str],
        body: bytes | str,
        expected_channel_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Validate an incoming webhook notification.
        :param headers: Headers from the webhook request.
        :param body: Body from the webhook request.
        :param expected_channel_id: Optional expected channel ID for validation.
        :return: Parsed notification data.
        """
        ...

    @staticmethod
    def validate_webhook_notification_static(
        headers: dict[str, str],
        body: bytes | str,
        expected_channel_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Validate an incoming webhook notification (static method).
        :param headers: Headers from the webhook request.
        :param body: Body from the webhook request.
        :param expected_channel_id: Optional expected channel ID for validation.
        :return: Parsed notification data.
        """
        ...
