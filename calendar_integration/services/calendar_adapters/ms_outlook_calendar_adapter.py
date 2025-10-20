"""
Microsoft Outlook Calendar Adapter - Webhook Subscriptions Implementation

This adapter implements webhook subscription support for Microsoft Graph calendar events,
providing compatibility with the CalendarAdapter protocol.

Webhook Features:
- subscribe_to_calendar_events(): Creates Microsoft Graph subscriptions for calendar events
- unsubscribe_from_calendar_events(): Finds and deletes subscriptions for a specific calendar
- Comprehensive error handling with logging
- Support for multiple change types (created, updated, deleted)
- Automatic subscription discovery and cleanup

Implementation Notes:
- Uses the MSOutlookCalendarAPIClient for low-level subscription management
- Handles both primary calendar ("primary") and specific calendar IDs
- Logs subscription creation/deletion for monitoring
- Raises ValueError on subscription failures with meaningful error messages
"""

import datetime
import logging
from collections.abc import Iterable
from typing import Any, ClassVar, Literal, TypedDict, TypeGuard

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpHeaders, HttpRequest

from calendar_integration.constants import CalendarProvider
from calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client import (
    MSGraphAPIError,
    MSGraphEvent,
    MSOutlookCalendarAPIClient,
)
from calendar_integration.services.dataclasses import (
    ApplicationCalendarData,
    CalendarEventAdapterInputData,
    CalendarEventAdapterOutputData,
    CalendarEventsSyncTypedDict,
    CalendarResourceData,
    EventAttendeeData,
)
from calendar_integration.services.protocols.calendar_adapter import CalendarAdapter


logger = logging.getLogger(__name__)


class MSOutlookCredentialTypedDict(TypedDict):
    token: str
    refresh_token: str
    account_id: str


class RRuleComponentsTypedDict(TypedDict, total=False):
    """Type definition for parsed RRULE components."""

    FREQ: str
    INTERVAL: str
    COUNT: str
    UNTIL: str
    BYDAY: str
    BYMONTHDAY: str
    BYMONTH: str
    BYYEARDAY: str
    BYWEEKNO: str
    BYSETPOS: str


class MSGraphRecurrencePatternTypedDict(TypedDict, total=False):
    """Type definition for Microsoft Graph recurrence pattern."""

    type: str  # "daily", "weekly", "absoluteMonthly", "relativeMonthly", "absoluteYearly", "relativeYearly"
    interval: int
    daysOfWeek: list[str]  # ["monday", "tuesday", etc.]
    dayOfMonth: int
    month: int
    index: str  # "first", "second", "third", "fourth", "last"


class MSGraphRecurrenceRangeTypedDict(TypedDict, total=False):
    """Type definition for Microsoft Graph recurrence range."""

    type: str  # "noEnd", "numbered", "endDate"
    numberOfOccurrences: int
    startDate: str  # YYYY-MM-DD format
    endDate: str  # YYYY-MM-DD format


class MSGraphRecurrenceTypedDict(TypedDict):
    """Type definition for Microsoft Graph recurrence object."""

    pattern: MSGraphRecurrencePatternTypedDict
    range: MSGraphRecurrenceRangeTypedDict


def _is_valid_rrule_key(
    rrule: str,
) -> TypeGuard[
    Literal[
        "FREQ",
        "INTERVAL",
        "COUNT",
        "UNTIL",
        "BYDAY",
        "BYMONTHDAY",
        "BYMONTH",
        "BYYEARDAY",
        "BYWEEKNO",
        "BYSETPOS",
    ]
]:
    """Check if the RRULE key is valid."""
    return rrule in [
        "FREQ",
        "INTERVAL",
        "COUNT",
        "UNTIL",
        "BYDAY",
        "BYMONTHDAY",
        "BYMONTH",
        "BYYEARDAY",
        "BYWEEKNO",
        "BYSETPOS",
    ]


class MSOutlookCalendarAdapter(CalendarAdapter):
    provider = "microsoft"
    RSVP_STATUS_MAPPING: ClassVar[dict[str, Literal["pending", "accepted", "declined"]]] = {
        "none": "pending",
        "organizer": "accepted",
        "tentativelyAccepted": "pending",
        "accepted": "accepted",
        "declined": "declined",
        "notResponded": "pending",
    }

    def __init__(self, credentials_dict: MSOutlookCredentialTypedDict):
        ms_client_id = getattr(settings, "MS_CLIENT_ID", None)
        ms_client_secret = getattr(settings, "MS_CLIENT_SECRET", None)

        if not ms_client_id or not ms_client_secret:
            raise ImproperlyConfigured(
                "Microsoft Calendar integration requires MS_CLIENT_ID and MS_CLIENT_SECRET settings."
            )

        # Initialize the API client with the access token
        self.client = MSOutlookCalendarAPIClient(access_token=credentials_dict["token"])

        # Store refresh token for potential token refresh
        self.refresh_token = credentials_dict["refresh_token"]

        # Test the connection
        if not self.client.test_connection():
            raise ValueError("Invalid or expired Microsoft Graph credentials provided.")

    @staticmethod
    def parse_webhook_headers(headers: HttpHeaders) -> dict[str, str]:
        return {}

    @staticmethod
    def extract_calendar_external_id_from_webhook_request(request: HttpRequest) -> str:
        return ""

    def _convert_ms_event_to_calendar_event_data(
        self, ms_event: MSGraphEvent, calendar_id: str
    ) -> CalendarEventAdapterOutputData:
        """Convert MSGraphEvent to CalendarEventData."""
        # Extract attendees information
        attendees = []
        for attendee in ms_event.attendees:
            email_address = attendee.get("emailAddress", {})
            status = attendee.get("status", {}).get("response", "none")

            attendees.append(
                EventAttendeeData(
                    email=email_address.get("address", ""),
                    name=email_address.get("name", ""),
                    status=self.RSVP_STATUS_MAPPING.get(status, "pending"),
                )
            )

        # Determine event status
        status = "cancelled" if ms_event.is_cancelled else "confirmed"

        # Extract recurrence information if present
        recurrence_rule = None
        if hasattr(ms_event, "recurrence_pattern") and ms_event.recurrence_pattern:
            recurrence_rule = self._convert_ms_recurrence_to_rrule(ms_event.recurrence_pattern)

        return CalendarEventAdapterOutputData(
            calendar_external_id=calendar_id,
            external_id=ms_event.id,
            title=ms_event.subject,
            description=ms_event.body_content,
            start_time=ms_event.start_time,
            end_time=ms_event.end_time,
            timezone=ms_event.timezone,
            attendees=attendees,
            status=status,
            original_payload=ms_event.original_payload,
            recurrence_rule=recurrence_rule,
            recurring_event_id=getattr(ms_event, "seriesMasterId", None),
        )

    def _convert_ms_recurrence_to_rrule(self, ms_recurrence: dict) -> str:
        """Convert Microsoft Graph recurrence to RRULE string."""
        pattern = ms_recurrence.get("pattern", {})
        recurrence_range = ms_recurrence.get("range", {})

        rrule_parts = []

        # Convert frequency
        pattern_type = pattern.get("type", "daily")
        if pattern_type == "daily":
            rrule_parts.append("FREQ=DAILY")
        elif pattern_type == "weekly":
            rrule_parts.append("FREQ=WEEKLY")
        elif pattern_type in ["absoluteMonthly", "relativeMonthly"]:
            rrule_parts.append("FREQ=MONTHLY")
        elif pattern_type in ["absoluteYearly", "relativeYearly"]:
            rrule_parts.append("FREQ=YEARLY")

        # Convert interval (only if not 1)
        if "interval" in pattern and pattern["interval"] != 1:
            rrule_parts.append(f"INTERVAL={pattern['interval']}")

        # Convert weekdays
        if "daysOfWeek" in pattern:
            weekday_map = {
                "monday": "MO",
                "tuesday": "TU",
                "wednesday": "WE",
                "thursday": "TH",
                "friday": "FR",
                "saturday": "SA",
                "sunday": "SU",
            }
            weekdays = [weekday_map.get(day) for day in pattern["daysOfWeek"]]
            if weekdays:
                rrule_parts.append(f"BYDAY={','.join(filter(None, weekdays))}")

        # Convert month day
        if "dayOfMonth" in pattern:
            rrule_parts.append(f"BYMONTHDAY={pattern['dayOfMonth']}")

        # Convert range
        range_type = recurrence_range.get("type", "noEnd")
        if range_type == "numbered" and "numberOfOccurrences" in recurrence_range:
            rrule_parts.append(f"COUNT={recurrence_range['numberOfOccurrences']}")
        elif range_type == "endDate" and "endDate" in recurrence_range:
            # Convert endDate to UNTIL format (YYYYMMDDTHHMMSSZ)
            import datetime

            end_date = datetime.datetime.strptime(recurrence_range["endDate"], "%Y-%m-%d")
            rrule_parts.append(f"UNTIL={end_date.strftime('%Y%m%dT%H%M%SZ')}")

        return "RRULE:" + ";".join(rrule_parts)

    def _convert_rrule_to_ms_format(self, rrule_string: str) -> MSGraphRecurrenceTypedDict:
        """Convert RRULE string to Microsoft Graph recurrence format."""
        # Parse RRULE string into components
        if rrule_string.startswith("RRULE:"):
            rrule_string = rrule_string[6:]

        parts = rrule_string.split(";")
        rule_data: RRuleComponentsTypedDict = {}

        for part in parts:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            if not _is_valid_rrule_key(key):
                raise ValueError(f"Unsupported RRULE component: {key}")
            rule_data[key] = value

        # Convert to Microsoft Graph format
        pattern: MSGraphRecurrencePatternTypedDict = {}
        recurrence_range: MSGraphRecurrenceRangeTypedDict = {}

        # Handle frequency
        freq = rule_data.get("FREQ", "DAILY")
        if freq == "DAILY":
            pattern["type"] = "daily"
        elif freq == "WEEKLY":
            pattern["type"] = "weekly"
        elif freq == "MONTHLY":
            pattern["type"] = "absoluteMonthly"  # Can also be "relativeMonthly"
        elif freq == "YEARLY":
            pattern["type"] = "absoluteYearly"  # Can also be "relativeYearly"

        # Handle interval
        if "INTERVAL" in rule_data:
            pattern["interval"] = int(rule_data["INTERVAL"])

        # Handle weekdays
        if "BYDAY" in rule_data:
            weekday_map = {
                "MO": "monday",
                "TU": "tuesday",
                "WE": "wednesday",
                "TH": "thursday",
                "FR": "friday",
                "SA": "saturday",
                "SU": "sunday",
            }
            weekdays = [weekday_map.get(day.strip()) for day in rule_data["BYDAY"].split(",")]
            pattern["daysOfWeek"] = [day for day in weekdays if day]

        # Handle month day
        if "BYMONTHDAY" in rule_data:
            pattern["dayOfMonth"] = int(rule_data["BYMONTHDAY"].split(",")[0])

        # Handle count vs until
        if "COUNT" in rule_data:
            recurrence_range["type"] = "numbered"
            recurrence_range["numberOfOccurrences"] = int(rule_data["COUNT"])
        elif "UNTIL" in rule_data:
            recurrence_range["type"] = "endDate"
            # Parse UNTIL date (YYYYMMDDTHHMMSSZ format)
            until_str = rule_data["UNTIL"]
            if until_str.endswith("Z"):
                import datetime

                until_dt = datetime.datetime.strptime(until_str, "%Y%m%dT%H%M%SZ")
                recurrence_range["endDate"] = until_dt.strftime("%Y-%m-%d")
        else:
            recurrence_range["type"] = "noEnd"

        return {
            "pattern": pattern,
            "range": recurrence_range,
        }

    def _convert_calendar_event_input_to_ms_format(
        self, event_data: CalendarEventAdapterInputData
    ) -> dict:
        """Convert CalendarEventAdapterInputData to Microsoft Graph format."""
        ms_event_data = {
            "subject": event_data.title,
            "body": {
                "contentType": "HTML" if event_data.description else "Text",
                "content": event_data.description or "",
            },
            "start": {
                "dateTime": event_data.start_time.isoformat(),
                "timeZone": event_data.start_time.tzinfo.tzname
                if event_data.start_time.tzinfo
                else "UTC",
            },
            "end": {
                "dateTime": event_data.end_time.isoformat(),
                "timeZone": event_data.end_time.tzinfo.tzname
                if event_data.end_time.tzinfo
                else "UTC",
            },
            "attendees": [
                {
                    "emailAddress": {
                        "address": attendee.email,
                        "name": attendee.name,
                    },
                    "type": "required",
                }
                for attendee in event_data.attendees
            ]
            + [
                {
                    "emailAddress": {
                        "address": resource.email,
                        "name": resource.title,
                    },
                    "type": "resource",
                }
                for resource in event_data.resources
            ],
        }

        # Add recurrence pattern if provided
        if event_data.recurrence_rule and not event_data.is_recurring_instance:
            ms_event_data["recurrence"] = self._convert_rrule_to_ms_format(
                event_data.recurrence_rule
            )

        return ms_event_data

    def get_account_calendars(self) -> Iterable[CalendarResourceData]:
        calendars_data = self.client.list_calendars()
        calendars = (
            CalendarResourceData(
                external_id=c.id,
                name=c.name,
                description="",
                email=c.email_address,
                capacity=None,  # MS Graph calendars don't have capacity
                is_default=c.is_default,
                provider=self.provider,
                original_payload=c.original_payload,
            )
            for c in calendars_data
        )
        return calendars

    def create_application_calendar(self, name: str) -> ApplicationCalendarData:
        ms_calendar = self.client.create_application_calendar(name)

        return ApplicationCalendarData(
            id=None,
            external_id=ms_calendar.id,
            name=ms_calendar.name,
            description="",
            email=ms_calendar.email_address,
            original_payload=ms_calendar.original_payload,
            provider=CalendarProvider(self.provider),
            organization_id=None,  # will be set later in the sync process
        )

    def create_event(
        self, event_data: CalendarEventAdapterInputData
    ) -> CalendarEventAdapterOutputData:
        """Create a new event in the calendar."""
        try:
            ms_format_data = self._convert_calendar_event_input_to_ms_format(event_data)

            ms_event = self.client.create_event(
                calendar_id=event_data.calendar_external_id, **ms_format_data
            )

            return self._convert_ms_event_to_calendar_event_data(
                ms_event, event_data.calendar_external_id
            )

        except MSGraphAPIError as e:
            raise ValueError(f"Failed to create event: {e}") from e

    def _get_events_iterator(
        self, ms_events: list[MSGraphEvent], calendar_id: str
    ) -> Iterable[CalendarEventAdapterOutputData]:
        """Generator that yields CalendarEventData objects from MS Graph events."""
        for ms_event in ms_events:
            yield self._convert_ms_event_to_calendar_event_data(ms_event, calendar_id)

    def get_events(
        self,
        calendar_id: str,
        calendar_is_resource: bool,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        sync_token: str | None = None,
        max_results_per_page: int = 250,
    ) -> CalendarEventsSyncTypedDict:
        """Get events from the calendar with optional sync token for incremental sync."""
        try:
            if calendar_is_resource:
                return self._get_room_events_sync_result(
                    calendar_id, start_date, end_date, sync_token, max_results_per_page
                )

            events_iterator = self._create_calendar_events_iterator(
                calendar_id, start_date, end_date, sync_token, max_results_per_page
            )

            return CalendarEventsSyncTypedDict(
                events=events_iterator,
                next_sync_token=getattr(self, "_next_sync_token", None),
            )

        except MSGraphAPIError as e:
            raise ValueError(f"Failed to get events: {e}") from e

    def _get_room_events_sync_result(
        self,
        calendar_id: str,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        sync_token: str | None,
        max_results_per_page: int,
    ) -> CalendarEventsSyncTypedDict:
        """Handle room events and return sync result."""
        room_events = self._get_room_events(
            room_email=calendar_id,
            start_time=start_date,
            end_time=end_date,
            sync_token=sync_token,
            max_results_per_page=max_results_per_page,
        )
        return CalendarEventsSyncTypedDict(
            events=iter(room_events),
            next_sync_token=None,
        )

    def _create_calendar_events_iterator(
        self,
        calendar_id: str,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        sync_token: str | None,
        max_results_per_page: int,
    ):
        """Create a memory-efficient iterator that paginates through calendar events."""
        # This will be set by the iterator methods
        self._next_sync_token: str | None = None

        if sync_token:
            return self._create_delta_events_iterator(
                calendar_id, start_date, end_date, sync_token, max_results_per_page
            )
        else:
            iterator = self._create_initial_sync_events_iterator(
                calendar_id, start_date, end_date, max_results_per_page
            )
            # For initial sync, try to get a delta token for future syncs
            self._set_initial_sync_token(calendar_id, start_date, end_date)
            return iterator

    def _set_initial_sync_token(
        self,
        calendar_id: str,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> None:
        """Set the sync token for initial sync."""
        try:
            initial_delta = self.client.get_events_delta(
                start_time=start_date, end_time=end_date, calendar_id=calendar_id
            )
            self._next_sync_token = self._extract_next_sync_token(initial_delta)
        except MSGraphAPIError:
            # Delta queries might not be available for all calendars
            self._next_sync_token = None

    def _create_delta_events_iterator(
        self,
        calendar_id: str,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        sync_token: str,
        max_results_per_page: int,
    ):
        """Create iterator for delta query (incremental sync) events."""
        page_token: str | None = sync_token

        while page_token:
            # Get delta result based on token type
            if "$deltatoken=" in page_token:
                delta_result = self.client.get_events_delta(
                    start_time=start_date,
                    end_time=end_date,
                    delta_token=page_token,
                    calendar_id=calendar_id,
                    max_page_size=max_results_per_page,
                )
            else:
                delta_result = self.client.get_events_delta(
                    start_time=start_date,
                    end_time=end_date,
                    skip_token=page_token,
                    calendar_id=calendar_id,
                    max_page_size=max_results_per_page,
                )

            # Yield events from current page
            for event in delta_result["events"]:
                yield self._convert_ms_event_to_calendar_event_data(event, calendar_id)

            # Check for next page
            next_page_token = self._extract_next_page_token(delta_result)
            if next_page_token:
                page_token = next_page_token
            else:
                # No more pages, extract final sync token
                self._next_sync_token = self._extract_next_sync_token(delta_result)
                page_token = None

    def _create_initial_sync_events_iterator(
        self,
        calendar_id: str,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        max_results_per_page: int,
    ):
        """Create iterator for initial sync events using manual pagination."""
        skip = 0

        while True:
            # Use the low-level list_events method which supports pagination
            page_events = self.client.list_events(
                calendar_id=calendar_id,
                start_time=start_date,
                end_time=end_date,
                top=max_results_per_page,
                skip=skip,
            )

            if not page_events:
                break

            items_in_page = 0
            # Yield events from current page
            for i, event in enumerate(page_events):
                yield self._convert_ms_event_to_calendar_event_data(event, calendar_id)
                items_in_page = i

            # If we got fewer events than requested, we've reached the end
            if items_in_page < max_results_per_page:
                break

            skip += max_results_per_page

    def _extract_next_page_token(self, delta_result: dict[str, Any]) -> str | None:
        """Extract the next page token from delta query result."""
        if delta_result.get("next_link"):
            next_link = delta_result["next_link"]
            if "$skiptoken=" in next_link:
                return next_link.split("$skiptoken=")[1].split("&")[0]
        return None

    def _extract_next_sync_token(self, delta_result: dict[str, Any]) -> str | None:
        """Extract the next sync token from delta query result."""
        # Check for delta link (indicates end of sync)
        if delta_result.get("delta_link"):
            delta_link = delta_result["delta_link"]
            # Extract deltatoken parameter from the URL
            if "$deltatoken=" in delta_link:
                return delta_link.split("$deltatoken=")[1].split("&")[0]

        # Check for next link (indicates more pages)
        if delta_result.get("next_link"):
            next_link = delta_result["next_link"]
            # Extract skiptoken parameter from the URL
            if "$skiptoken=" in next_link:
                return next_link.split("$skiptoken=")[1].split("&")[0]

        return None

    def get_event(self, calendar_id: str, event_id: str) -> CalendarEventAdapterOutputData:
        """Get a specific event by its ID."""
        try:
            ms_event = self.client.get_event(event_id, calendar_id)
            return self._convert_ms_event_to_calendar_event_data(ms_event, calendar_id)

        except MSGraphAPIError as e:
            raise ValueError(f"Failed to get event: {e}") from e

    def update_event(
        self, calendar_id: str, event_id: str, event_data: CalendarEventAdapterInputData
    ) -> CalendarEventAdapterOutputData:
        """Update an existing event."""
        try:
            # Convert attendees back to the format expected by the API client
            attendees = []
            for attendee in event_data.attendees:
                attendees.append(
                    {
                        "email": attendee.email,
                        "name": attendee.name,
                        "type": "required",
                        "status": attendee.status,
                    }
                )

            ms_event = self.client.update_event(
                event_id=event_id,
                calendar_id=calendar_id,
                subject=event_data.title,
                body=event_data.description,
                start_time=event_data.start_time,
                end_time=event_data.end_time,
                attendees=attendees,
            )

            return self._convert_ms_event_to_calendar_event_data(ms_event, calendar_id)

        except MSGraphAPIError as e:
            raise ValueError(f"Failed to update event: {e}") from e

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        """Delete an event from the calendar."""
        try:
            self.client.delete_event(event_id, calendar_id)
        except MSGraphAPIError as e:
            raise ValueError(f"Failed to delete event: {e}") from e

    def get_calendar_resources(self) -> Iterable[CalendarResourceData]:
        """Get all calendar resources (calendars)."""
        try:
            calendars = self.client.list_calendars()

            for calendar in calendars:
                yield CalendarResourceData(
                    external_id=calendar.id,
                    name=calendar.name,
                    description="",  # MS Graph calendars don't have descriptions in the same way
                    email=calendar.email_address,
                    capacity=None,  # Calendars don't have capacity
                    original_payload=calendar.original_payload,
                    provider=self.provider,
                )

        except MSGraphAPIError as e:
            raise ValueError(f"Failed to get calendar resources: {e}") from e

    def get_calendar_resource(self, resource_id: str) -> CalendarResourceData:
        """Get a specific calendar resource by ID."""
        try:
            calendar = self.client.get_calendar(resource_id)

            return CalendarResourceData(
                external_id=calendar.id,
                name=calendar.name,
                description="",
                email=calendar.email_address,
                capacity=None,
                original_payload=calendar.original_payload,
                provider=self.provider,
            )

        except MSGraphAPIError as e:
            raise ValueError(f"Failed to get calendar resource: {e}") from e

    def get_available_calendar_resources(
        self, start_time: datetime.datetime, end_time: datetime.datetime
    ) -> Iterable[CalendarResourceData]:
        """Get available calendar resources (meeting rooms) for a given time period."""
        try:
            # Get all rooms
            rooms = self.client.list_rooms()

            if not rooms:
                return

            # Get free/busy information for all rooms
            room_emails = [room.email_address for room in rooms if room.email_address]

            if not room_emails:
                return

            free_busy_info = self.client.get_free_busy_schedule(
                schedules=room_emails, start_time=start_time, end_time=end_time
            )

            # Convert rooms to calendar resources and filter available ones
            rooms_by_email = {room.email_address: room for room in rooms if room.email_address}

            for schedule_info in free_busy_info.get("value", []):
                schedule_email = schedule_info.get("scheduleId", "")
                busy_times = schedule_info.get("busyViewTimes", [])

                # If no busy times, the room is available
                if not busy_times and schedule_email in rooms_by_email:
                    room = rooms_by_email[schedule_email]
                    yield CalendarResourceData(
                        external_id=room.id,
                        name=room.display_name,
                        description=f"Building: {room.building}, Floor: {room.floor_number}"
                        if room.building
                        else "",
                        email=room.email_address,
                        capacity=room.capacity,
                        original_payload=room.original_payload,
                        provider=self.provider,
                    )

        except MSGraphAPIError as e:
            raise ValueError(f"Failed to get available calendar resources: {e}") from e

    def subscribe_to_calendar_events(self, resource_id: str, callback_url: str) -> None:
        """
        Subscribe to calendar events for a specific resource.
        Creates a Microsoft Graph webhook subscription for calendar events.

        Args:
            resource_id: The calendar ID to subscribe to
            callback_url: The URL where notifications will be sent
        """
        try:
            # Use the API client to create a subscription
            subscription = self.client.subscribe_to_calendar_events(
                calendar_id=resource_id,
                notification_url=callback_url,
                change_types=["created", "updated", "deleted"],
            )

            logger.info(
                "Successfully subscribed to calendar events for resource %s. Subscription ID: %s",
                resource_id,
                subscription.get("id"),
            )

        except MSGraphAPIError as e:
            raise ValueError(f"Failed to subscribe to calendar events: {e}") from e

    def unsubscribe_from_calendar_events(self, resource_id: str) -> None:
        """
        Unsubscribe from calendar events for a specific resource.
        This method finds and deletes webhook subscriptions for the given resource.

        Args:
            resource_id: The calendar ID to unsubscribe from
        """
        try:
            # List all subscriptions to find the ones for this resource
            subscriptions = self.client.list_subscriptions()

            # Find subscriptions for this calendar
            calendar_subscriptions = []
            for subscription in subscriptions:
                resource = subscription.get("resource", "")
                # Check if this subscription is for the specified calendar
                if (
                    resource == "/me/events" and resource_id == "primary"
                ) or resource == f"/me/calendars/{resource_id}/events":
                    calendar_subscriptions.append(subscription)

            if not calendar_subscriptions:
                logger.warning(
                    "No subscriptions found for calendar resource %s",
                    resource_id,
                )
                return

            # Delete all subscriptions for this calendar
            for subscription in calendar_subscriptions:
                subscription_id = subscription.get("id")
                if subscription_id:
                    self.client.unsubscribe_from_calendar_events(subscription_id)
                    logger.info(
                        "Successfully unsubscribed from calendar events. Subscription ID: %s",
                        subscription_id,
                    )

        except MSGraphAPIError as e:
            raise ValueError(f"Failed to unsubscribe from calendar events: {e}") from e

    def subscribe_to_room_events(self, room_email: str, callback_url: str) -> None:
        """
        Subscribe to room booking events for a specific room resource.
        Creates a Microsoft Graph webhook subscription for room calendar events.

        Args:
            room_email: The email address of the room to subscribe to
            callback_url: The URL where notifications will be sent
        """
        try:
            # Use the API client to create a subscription for the room
            subscription = self.client.subscribe_to_room_events(
                room_email=room_email,
                notification_url=callback_url,
                change_types=["created", "updated", "deleted"],
            )

            logger.info(
                "Successfully subscribed to room events for %s. Subscription ID: %s",
                room_email,
                subscription.get("id"),
            )

        except MSGraphAPIError as e:
            raise ValueError(f"Failed to subscribe to room events: {e}") from e

    def unsubscribe_from_room_events(self, room_email: str) -> None:
        """
        Unsubscribe from room booking events for a specific room resource.
        This method finds and deletes webhook subscriptions for the given room.

        Args:
            room_email: The email address of the room to unsubscribe from
        """
        try:
            self.client.unsubscribe_from_room_events(room_email)

        except MSGraphAPIError as e:
            raise ValueError(f"Failed to unsubscribe from room events: {e}") from e

    def _get_all_rooms(self) -> list[CalendarResourceData]:
        """
        Get all available rooms in the organization.
        This method retrieves all room resources from Microsoft Graph.

        Returns:
            List of CalendarResourceData objects representing rooms
        """
        try:
            rooms = self.client.list_rooms()

            if not rooms:
                logger.warning("No rooms found in the organization")
                return []

            return [
                CalendarResourceData(
                    external_id=room.id,
                    name=room.display_name,
                    description=f"Building: {room.building}, Floor: {room.floor_number}"
                    if room.building
                    else "",
                    email=room.email_address,
                    capacity=room.capacity,
                    original_payload=room.original_payload,
                    provider=self.provider,
                )
                for room in rooms
            ]

        except MSGraphAPIError as e:
            raise ValueError(f"Failed to get available rooms: {e}") from e

    def subscribe_to_all_available_rooms(self, callback_url: str) -> dict[str, Any]:
        """
        Subscribe to events for all available rooms in the organization.

        Args:
            callback_url: The URL where notifications will be sent

        Returns:
            Dictionary with subscription results and any errors
        """
        try:
            # Get all available rooms
            rooms = self._get_all_rooms()
            room_emails = [room.email for room in rooms if room.email]

            if not room_emails:
                logger.warning("No rooms found to subscribe to")
                return {"subscriptions": [], "errors": [], "message": "No rooms found"}

            # Subscribe to multiple rooms
            subscriptions = self.client.subscribe_to_multiple_room_events(
                room_emails=room_emails,
                notification_url=callback_url,
                change_types=["created", "updated", "deleted"],
            )

            logger.info(
                "Successfully subscribed to %d room calendars out of %d available rooms",
                len(subscriptions),
                len(room_emails),
            )

            return {
                "subscriptions": subscriptions,
                "total_rooms": len(room_emails),
                "successful_subscriptions": len(subscriptions),
            }

        except MSGraphAPIError as e:
            raise ValueError(f"Failed to subscribe to room events: {e}") from e

    def _convert_ms_graph_event_to_calendar_event_data(
        self, ms_event: MSGraphEvent
    ) -> CalendarEventAdapterOutputData:
        """
        Convert a Microsoft Graph event to CalendarEventData format.

        Args:
            ms_event: The MSGraphEvent object to convert

        Returns:
            CalendarEventData object
        """
        return CalendarEventAdapterOutputData(
            calendar_external_id=ms_event.calendar_id,
            external_id=ms_event.id,
            title=ms_event.subject,
            description=ms_event.body_content,
            start_time=ms_event.start_time,
            end_time=ms_event.end_time,
            timezone=ms_event.timezone,
            attendees=[
                EventAttendeeData(
                    email=attendee.get("email_address", {}).get("address", ""),
                    name=attendee.get("email_address", {}).get("name", ""),
                    status=self.RSVP_STATUS_MAPPING.get(
                        attendee.get("status", {}).get("response"), "pending"
                    ),
                )
                for attendee in ms_event.attendees
            ],
            status="cancelled" if ms_event.is_cancelled else "confirmed",
            original_payload=ms_event.original_payload,
        )

    def _get_room_events(
        self,
        room_email: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        sync_token: str | None = None,
        max_results_per_page: int = 250,
    ) -> list[CalendarEventAdapterOutputData]:
        """
        Get calendar events for a specific room with pagination support.

        Args:
            room_email: The email address of the room
            start_time: Start time for events
            end_time: End time for events
            sync_token: Optional sync token for incremental sync
            max_results_per_page: Maximum events per page

        Returns:
            List of CalendarEventData objects
        """
        try:
            calendar_events = []

            if sync_token:
                # Use delta query for incremental sync - handles its own pagination
                page_token = sync_token
                while True:
                    if "$deltatoken=" in page_token:
                        delta_result = self.client.get_room_events_delta(
                            room_email=room_email,
                            start_time=start_time,
                            end_time=end_time,
                            delta_token=page_token,
                            max_page_size=max_results_per_page,
                        )
                    else:
                        delta_result = self.client.get_room_events_delta(
                            room_email=room_email,
                            start_time=start_time,
                            end_time=end_time,
                            skip_token=page_token,
                            max_page_size=max_results_per_page,
                        )

                    # Process current page
                    for event in delta_result["events"]:
                        calendar_event = self._convert_ms_graph_event_to_calendar_event_data(event)
                        calendar_events.append(calendar_event)

                    # Check for next page
                    if delta_result.get("next_link"):
                        next_link = delta_result["next_link"]
                        if "$skiptoken=" in next_link:
                            page_token = next_link.split("$skiptoken=")[1].split("&")[0]
                        else:
                            break
                    else:
                        break
            else:
                # For room events without sync token, we need to implement pagination manually
                # Since get_room_events doesn't support pagination parameters directly,
                # we'll use a reasonable approach with batching
                all_events = self.client.get_room_events(
                    room_email=room_email,
                    start_time=start_time,
                    end_time=end_time,
                )

                # Convert to CalendarEventData format
                for event in all_events:
                    calendar_event = self._convert_ms_graph_event_to_calendar_event_data(event)
                    calendar_events.append(calendar_event)

            return calendar_events

        except MSGraphAPIError as e:
            raise ValueError(f"Failed to get room events: {e}") from e

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

        # TODO: Implement tracking parameters in subscription creation if supported
        return {}

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

        # TODO: Implement webhook validation logic
        return {}

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

        # TODO: Implement static webhook validation logic
        return {}
