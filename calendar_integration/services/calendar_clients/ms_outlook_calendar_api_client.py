"""
Microsoft Outlook Calendar Integration - Webhook Subscriptions Implementation

This module implements Microsoft Graph webhook subscriptions for real-time calendar event notifications.

Webhook Methods Implemented:

API Client (MSOutlookCalendarAPIClient):
- create_subscription(): Create a webhook subscription for any Microsoft Graph resource
- list_subscriptions(): List all active subscriptions
- get_subscription(): Get details of a specific subscription
- update_subscription(): Update subscription expiration time
- delete_subscription(): Delete a subscription
- subscribe_to_calendar_events(): High-level method to subscribe to calendar events
- unsubscribe_from_calendar_events(): High-level method to unsubscribe from calendar events

Adapter (MSOutlookCalendarAdapter):
- subscribe_to_calendar_events(): Subscribe to calendar events with error handling
- unsubscribe_from_calendar_events(): Unsubscribe by finding and deleting relevant subscriptions

Key Features:
- Support for multiple change types (created, updated, deleted)
- Automatic expiration handling (3-day maximum for user resources)
- Comprehensive error handling and logging
- Resource-specific subscription management
- Client state validation support

Microsoft Graph Requirements:
- Webhook endpoint must be publicly accessible via HTTPS
- Microsoft validates the endpoint before creating subscriptions
- Subscriptions expire after 3 days for user resources (can be renewed)
- Requires appropriate OAuth scopes (Calendars.Read minimum)

Usage:
```python
# Via adapter (recommended)
adapter.subscribe_to_calendar_events("calendar_id", "https://app.com/webhook")
adapter.unsubscribe_from_calendar_events("calendar_id")

# Via API client (low-level)
subscription = client.subscribe_to_calendar_events(
    calendar_id="primary",
    notification_url="https://app.com/webhook"
)
client.unsubscribe_from_calendar_events(subscription["id"])
```
"""

import datetime
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import requests
from pyrate_limiter import Duration, Limiter, Rate, RedisBucket

from common.redis import redis_connection


logger = logging.getLogger(__name__)


quote_limiter = Limiter(
    RedisBucket.init(
        [
            Rate(10000, Duration.MINUTE * 10),  # 10000 requests every 10 minutes
            Rate(130000, Duration.SECOND * 10),  # 1000 requests every minute
        ],
        redis=redis_connection,
        bucket_key="ms_outlook_calendar_limiter",
    ),
    raise_when_fail=False,
    max_delay=1000,  # Allow a maximum delay of 1 second for read operations
)


RETRIES_ON_ERROR = 5
STATUS_TO_RETRY = {500, 502, 503, 504}  # HTTP status codes to retry on


@dataclass
@dataclass
class MSGraphEvent:
    """Microsoft Graph Event representation"""

    id: str  # noqa: A003
    calendar_id: str
    subject: str
    body_content: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str
    location: str
    attendees: list[dict[str, Any]]
    organizer: dict[str, Any]
    is_cancelled: bool = False
    recurrence_pattern: dict[str, Any] | None = None
    original_payload: dict[str, Any] | None = None


@dataclass
class MSGraphCalendar:
    """Microsoft Graph Calendar representation"""

    id: str  # noqa: A003
    name: str
    email_address: str | None
    can_edit: bool
    is_default: bool
    original_payload: dict[str, Any] | None = None


@dataclass
class MSGraphRoom:
    """Microsoft Graph Room/Place representation"""

    id: str  # noqa: A003
    display_name: str
    email_address: str
    capacity: int | None
    building: str | None
    floor_number: int | None
    phone: str | None
    is_wheelchair_accessible: bool
    original_payload: dict[str, Any] | None = None


class MSGraphAPIError(Exception):
    """Exception raised for Microsoft Graph API errors"""

    def __init__(
        self, message: str, status_code: int | None = None, response_data: dict | None = None
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data


class MSOutlookCalendarAPIClient:
    """
    Microsoft Graph Calendar API Client for Microsoft Outlook integration.

    This client provides methods to interact with Microsoft Graph Calendar API
    for creating, reading, updating, and deleting calendar events, as well as
    managing calendar resources.
    """

    BASE_URL = "https://graph.microsoft.com/v1.0"

    def __init__(self, access_token: str, user_id: str | None = None):
        """
        Initialize the MS Outlook Calendar API client.

        Args:
            access_token: OAuth2 access token for Microsoft Graph API
            user_id: Optional user ID. If not provided, 'me' will be used
        """
        self.access_token = access_token
        self.user_id = user_id or "me"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def _make_request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Make HTTP request to Microsoft Graph API with retry logic.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE, PATCH)
            endpoint: API endpoint (without base URL)
            params: Query parameters
            data: Request body data
            headers: Additional headers

        Returns:
            Response data as dictionary

        Raises:
            MSGraphAPIError: If the API request fails after all retries
        """
        url = f"{self.BASE_URL}/{endpoint.lstrip('/')}"

        request_headers = dict(self.session.headers)
        if headers:
            request_headers.update(headers)

        last_exception = None

        for attempt in range(RETRIES_ON_ERROR + 1):  # +1 for the initial attempt
            try:
                quote_limiter.try_acquire("ms_outlook_calendar")
                response = self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=data,
                    headers=request_headers,
                    timeout=30,
                )

                if response.status_code == 204:  # No Content
                    return {}

                response_data = response.json() if response.content else {}

                if not response.ok:
                    # Check if this is a retryable status code and we have attempts left
                    if response.status_code in STATUS_TO_RETRY and attempt < RETRIES_ON_ERROR:
                        wait_time = 2**attempt  # Exponential backoff: 1s, 2s, 4s
                        logger.warning(
                            "MS Graph API returned %s (attempt %d/%d). Retrying in %ds...",
                            response.status_code,
                            attempt + 1,
                            RETRIES_ON_ERROR + 1,
                            wait_time,
                        )
                        time.sleep(wait_time)
                        continue

                    # Not retryable or out of attempts
                    error_msg = f"MS Graph API error: {response.status_code}"
                    if "error" in response_data:
                        error_msg += f" - {response_data['error'].get('message', 'Unknown error')}"

                    logger.error("%s. Response: %s", error_msg, response_data)
                    raise MSGraphAPIError(error_msg, response.status_code, response_data)

                return response_data

            except requests.RequestException as e:
                last_exception = e
                # Retry on request exceptions if we have attempts left
                if attempt < RETRIES_ON_ERROR:
                    wait_time = 2**attempt  # Exponential backoff: 1s, 2s, 4s
                    logger.warning(
                        "Request exception occurred (attempt %d/%d): %s. Retrying in %ds...",
                        attempt + 1,
                        RETRIES_ON_ERROR + 1,
                        e,
                        wait_time,
                    )
                    time.sleep(wait_time)
                    continue

                # Out of attempts, raise the exception
                logger.error("Request error after %d attempts: %s", RETRIES_ON_ERROR + 1, e)
                raise MSGraphAPIError(
                    f"Request failed after {RETRIES_ON_ERROR + 1} attempts: {e!s}"
                ) from e

        # This should not be reached, but just in case
        if last_exception:
            raise MSGraphAPIError(
                f"Request failed after {RETRIES_ON_ERROR + 1} attempts: {last_exception!s}"
            ) from last_exception

        # Fallback return - should never be reached
        raise MSGraphAPIError("Unexpected error: request loop completed without returning")

    def _parse_datetime(self, dt_dict: dict[str, str]) -> datetime.datetime:
        """
        Parse Microsoft Graph datetime format to Python datetime.

        Args:
            dt_dict: Dictionary with 'dateTime' and 'timeZone' keys

        Returns:
            Python datetime object
        """
        dt_str = dt_dict["dateTime"]
        # Parse ISO format datetime
        if dt_str.endswith("Z"):
            dt_str = dt_str[:-1] + "+00:00"
        elif "." in dt_str and not dt_str.endswith(("Z", "+", "-")):
            dt_str += "+00:00"

        return datetime.datetime.fromisoformat(dt_str.replace("Z", "+00:00"))

    def _format_datetime(self, dt: datetime.datetime, timezone: str = "UTC") -> dict[str, str]:
        """
        Format Python datetime to Microsoft Graph datetime format.

        Args:
            dt: Python datetime object
            timezone: Timezone name

        Returns:
            Dictionary with 'dateTime' and 'timeZone' keys
        """
        return {"dateTime": dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3], "timeZone": timezone}

    # Calendar Management Methods

    def create_application_calendar(self, name: str) -> MSGraphCalendar:
        """
        Create a new application calendar.

        Args:
            name: Name of the calendar

        Returns:
            Created MSGraphCalendar object
        """
        endpoint = f"/users/{self.user_id}/calendars"
        data = {"name": f"_virtual_{name}"}

        response = self._make_request("POST", endpoint, data=data)

        return MSGraphCalendar(
            id=response["id"],
            name=response["name"],
            email_address=response.get("owner", {}).get("address"),
            can_edit=True,
            is_default=False,
            original_payload=response,
        )

    def list_calendars(self) -> Iterable[MSGraphCalendar]:
        """
        List all calendars for the user.

        Returns:
            List of MSGraphCalendar objects
        """
        response = self._make_request("GET", f"/users/{self.user_id}/calendars")

        for cal_data in response.get("value", []):
            calendar = MSGraphCalendar(
                id=cal_data["id"],
                name=cal_data["name"],
                email_address=cal_data.get("owner", {}).get("address"),
                can_edit=cal_data.get("canEdit", False),
                is_default=cal_data.get("isDefaultCalendar", False),
                original_payload=cal_data,
            )
            yield calendar

    def get_calendar(self, calendar_id: str) -> MSGraphCalendar:
        """
        Get a specific calendar by ID.

        Args:
            calendar_id: Calendar ID

        Returns:
            MSGraphCalendar object
        """
        response = self._make_request("GET", f"/users/{self.user_id}/calendars/{calendar_id}")

        return MSGraphCalendar(
            id=response["id"],
            name=response["name"],
            email_address=response.get("owner", {}).get("address"),
            can_edit=response.get("canEdit", False),
            is_default=response.get("isDefaultCalendar", False),
            original_payload=response,
        )

    # Event Management Methods

    def list_events(
        self,
        calendar_id: str | None = None,
        start_time: datetime.datetime | None = None,
        end_time: datetime.datetime | None = None,
        top: int | None = None,
        skip: int | None = None,
        select: list[str] | None = None,
        filter_query: str | None = None,
        timezone: str = "UTC",
    ) -> Iterable[MSGraphEvent]:
        """
        List events from a calendar.

        Args:
            calendar_id: Calendar ID. If None, uses default calendar
            start_time: Filter events starting after this time
            end_time: Filter events ending before this time
            top: Maximum number of events to return
            skip: Number of events to skip
            select: List of properties to select
            filter_query: OData filter query
            timezone: Timezone for response times

        Returns:
            List of MSGraphEvent objects
        """
        if calendar_id:
            endpoint = f"/users/{self.user_id}/calendars/{calendar_id}/events"
        else:
            endpoint = f"/users/{self.user_id}/events"

        params: dict[str, Any] = {}
        if top:
            params["$top"] = top
        if skip:
            params["$skip"] = skip
        if select:
            params["$select"] = ",".join(select)
        if filter_query:
            params["$filter"] = filter_query

        headers = {}
        if timezone != "UTC":
            headers["Prefer"] = f'outlook.timezone="{timezone}"'

        response = self._make_request("GET", endpoint, params=params, headers=headers)

        for event_data in response.get("value", []):
            event = self._parse_event(event_data)
            # Apply time filtering if specified
            if start_time and event.end_time <= start_time:
                continue
            if end_time and event.start_time >= end_time:
                continue
            yield event

    def list_calendar_view(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        calendar_id: str | None = None,
        top: int | None = None,
        timezone: str = "UTC",
    ) -> Iterable[MSGraphEvent]:
        """
        Get calendar view (events within a specific date range).

        Args:
            start_time: Start of date range
            end_time: End of date range
            calendar_id: Calendar ID. If None, uses default calendar
            top: Maximum number of events to return
            timezone: Timezone for response times

        Returns:
            List of MSGraphEvent objects
        """
        if calendar_id:
            endpoint = f"/users/{self.user_id}/calendars/{calendar_id}/calendarView"
        else:
            endpoint = f"/users/{self.user_id}/calendarView"

        params: dict[str, Any] = {
            "startDateTime": start_time.isoformat(),
            "endDateTime": end_time.isoformat(),
        }
        if top:
            params["$top"] = top

        headers = {}
        if timezone != "UTC":
            headers["Prefer"] = f'outlook.timezone="{timezone}"'

        response = self._make_request("GET", endpoint, params=params, headers=headers)

        return (self._parse_event(event_data) for event_data in response.get("value", []))

    def get_event(self, event_id: str, calendar_id: str | None = None) -> MSGraphEvent:
        """
        Get a specific event by ID.

        Args:
            event_id: Event ID
            calendar_id: Calendar ID. If None, uses default calendar

        Returns:
            MSGraphEvent object
        """
        if calendar_id:
            endpoint = f"/users/{self.user_id}/calendars/{calendar_id}/events/{event_id}"
        else:
            endpoint = f"/users/{self.user_id}/events/{event_id}"

        response = self._make_request("GET", endpoint)
        return self._parse_event(response)

    def create_event(
        self,
        subject: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        body: str | None = None,
        location: str | None = None,
        attendees: list[dict[str, Any]] | None = None,
        calendar_id: str | None = None,
        timezone: str = "UTC",
        is_online_meeting: bool = False,
        recurrence_pattern: dict[str, Any] | None = None,
        **kwargs,
    ) -> MSGraphEvent:
        """
        Create a new event.

        Args:
            subject: Event title/subject
            start_time: Event start time
            end_time: Event end time
            body: Event description/body
            location: Event location
            attendees: List of attendee dictionaries
            calendar_id: Calendar ID. If None, uses default calendar
            timezone: Timezone for event times
            is_online_meeting: Whether to enable as online meeting
            recurrence_pattern: Recurrence pattern for recurring events
            **kwargs: Additional event properties

        Returns:
            Created MSGraphEvent object
        """
        if calendar_id:
            endpoint = f"/users/{self.user_id}/calendars/{calendar_id}/events"
        else:
            endpoint = f"/users/{self.user_id}/events"

        event_data: dict[str, Any] = {
            "subject": subject,
            "start": self._format_datetime(start_time, timezone),
            "end": self._format_datetime(end_time, timezone),
        }

        if body:
            event_data["body"] = {"contentType": "html", "content": body}

        if location:
            event_data["location"] = {"displayName": location}

        if attendees:
            event_data["attendees"] = self._format_attendees(attendees)

        if is_online_meeting:
            event_data["isOnlineMeeting"] = True
            event_data["onlineMeetingProvider"] = "teamsForBusiness"

        if recurrence_pattern:
            event_data["recurrence"] = recurrence_pattern

        # Add any additional properties
        event_data.update(kwargs)

        response = self._make_request("POST", endpoint, data=event_data)
        return self._parse_event(response)

    def update_event(
        self,
        event_id: str,
        subject: str | None = None,
        start_time: datetime.datetime | None = None,
        end_time: datetime.datetime | None = None,
        body: str | None = None,
        location: str | None = None,
        attendees: list[dict[str, Any]] | None = None,
        calendar_id: str | None = None,
        timezone: str = "UTC",
        **kwargs,
    ) -> MSGraphEvent:
        """
        Update an existing event.

        Args:
            event_id: Event ID to update
            subject: New event title/subject
            start_time: New event start time
            end_time: New event end time
            body: New event description/body
            location: New event location
            attendees: New list of attendee dictionaries
            calendar_id: Calendar ID. If None, uses default calendar
            timezone: Timezone for event times
            **kwargs: Additional event properties to update

        Returns:
            Updated MSGraphEvent object
        """
        if calendar_id:
            endpoint = f"/users/{self.user_id}/calendars/{calendar_id}/events/{event_id}"
        else:
            endpoint = f"/users/{self.user_id}/events/{event_id}"

        event_data: dict[str, Any] = {}

        if subject is not None:
            event_data["subject"] = subject

        if start_time is not None:
            event_data["start"] = self._format_datetime(start_time, timezone)

        if end_time is not None:
            event_data["end"] = self._format_datetime(end_time, timezone)

        if body is not None:
            event_data["body"] = {"contentType": "html", "content": body}

        if location is not None:
            event_data["location"] = {"displayName": location}

        if attendees is not None:
            event_data["attendees"] = self._format_attendees(attendees)

        # Add any additional properties
        event_data.update(kwargs)

        response = self._make_request("PATCH", endpoint, data=event_data)
        return self._parse_event(response)

    def delete_event(self, event_id: str, calendar_id: str | None = None) -> None:
        """
        Delete an event.

        Args:
            event_id: Event ID to delete
            calendar_id: Calendar ID. If None, uses default calendar
        """
        if calendar_id:
            endpoint = f"/users/{self.user_id}/calendars/{calendar_id}/events/{event_id}"
        else:
            endpoint = f"/users/{self.user_id}/events/{event_id}"

        self._make_request("DELETE", endpoint)

    def cancel_event(
        self, event_id: str, comment: str | None = None, calendar_id: str | None = None
    ) -> None:
        """
        Cancel an event (sends cancellation to attendees).

        Args:
            event_id: Event ID to cancel
            comment: Optional cancellation comment
            calendar_id: Calendar ID. If None, uses default calendar
        """
        if calendar_id:
            endpoint = f"/users/{self.user_id}/calendars/{calendar_id}/events/{event_id}/cancel"
        else:
            endpoint = f"/users/{self.user_id}/events/{event_id}/cancel"

        data = {}
        if comment:
            data["comment"] = comment

        self._make_request("POST", endpoint, data=data)

    # Delta/Sync Methods

    def get_events_delta(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        delta_token: str | None = None,
        skip_token: str | None = None,
        calendar_id: str | None = None,
        max_page_size: int | None = None,
    ) -> dict[str, Any]:
        """
        Get incremental changes to events using delta query.

        Args:
            start_time: Start of date range
            end_time: End of date range
            delta_token: Token from previous delta call (for new round)
            skip_token: Token from previous delta call (continuation)
            calendar_id: Calendar ID. If None, uses default calendar
            max_page_size: Maximum number of events per page

        Returns:
            Dictionary with 'events', 'next_link', 'delta_link'
        """
        if calendar_id:
            endpoint = f"/users/{self.user_id}/calendars/{calendar_id}/calendarView/delta"
        else:
            endpoint = f"/users/{self.user_id}/calendarView/delta"

        params: dict[str, Any] = {}

        if delta_token:
            params["$deltatoken"] = delta_token
        elif skip_token:
            params["$skiptoken"] = skip_token
        else:
            # Initial request
            params["startDateTime"] = start_time.isoformat()
            params["endDateTime"] = end_time.isoformat()

        headers = {}
        if max_page_size:
            headers["Prefer"] = f"odata.maxpagesize={max_page_size}"

        response = self._make_request("GET", endpoint, params=params, headers=headers)

        events = [self._parse_event(event_data) for event_data in response.get("value", [])]

        result = {
            "events": events,
            "next_link": response.get("@odata.nextLink"),
            "delta_link": response.get("@odata.deltaLink"),
        }

        return result

    def get_room_events_delta(
        self,
        room_email: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        delta_token: str | None = None,
        skip_token: str | None = None,
        max_page_size: int | None = None,
    ):
        """
        Get incremental changes to room events using delta query.

        Args:
            room_email: Email address of the room/resource
            start_time: Start of date range
            end_time: End of date range
            delta_token: Token from previous delta call (for new round)
            skip_token: Token from previous delta call (continuation)
            max_page_size: Maximum number of events per page

        Returns:
            Dictionary with 'events', 'next_link', 'delta_link'
        """
        endpoint = f"/places/{room_email}/calendarView/delta"

        params: dict[str, Any] = {}

        if delta_token:
            params["$deltatoken"] = delta_token
        elif skip_token:
            params["$skiptoken"] = skip_token
        else:
            # Initial request
            params["startDateTime"] = start_time.isoformat()
            params["endDateTime"] = end_time.isoformat()

        headers = {}
        if max_page_size:
            headers["Prefer"] = f"odata.maxpagesize={max_page_size}"

        response = self._make_request("GET", endpoint, params=params, headers=headers)

        events = [self._parse_event(event_data) for event_data in response.get("value", [])]

        result = {
            "events": events,
            "next_link": response.get("@odata.nextLink"),
            "delta_link": response.get("@odata.deltaLink"),
        }

        return result

    # Room/Resource Methods

    def list_rooms(self, page_size: int = 100) -> Iterable[MSGraphRoom]:
        """
        List all rooms in the tenant with pagination support.

        Args:
            page_size: Number of rooms to fetch per page (default: 100)

        Returns:
            Iterator of MSGraphRoom objects
        """
        return self._paginated_rooms_iterator(page_size)

    def _paginated_rooms_iterator(self, page_size: int) -> Iterable[MSGraphRoom]:
        """
        Create an iterator that paginates through all rooms.

        Args:
            page_size: Number of rooms to fetch per page

        Yields:
            MSGraphRoom objects one at a time
        """
        skip = 0

        while True:
            params = {
                "$top": page_size,
                "$skip": skip,
            }

            response = self._make_request("GET", "/places/microsoft.graph.room", params=params)
            room_data_list = response.get("value", [])

            if not room_data_list:
                break

            # Yield rooms from current page
            for room_data in room_data_list:
                yield MSGraphRoom(
                    id=room_data["id"],
                    display_name=room_data["displayName"],
                    email_address=room_data["emailAddress"],
                    capacity=room_data.get("capacity"),
                    building=room_data.get("building"),
                    floor_number=room_data.get("floorNumber"),
                    phone=room_data.get("phone"),
                    is_wheelchair_accessible=room_data.get("isWheelChairAccessible", False),
                    original_payload=room_data,
                )

            # If we got fewer rooms than requested, we've reached the end
            if len(room_data_list) < page_size:
                break

            skip += page_size

    def list_rooms_as_list(self, page_size: int = 100) -> list[MSGraphRoom]:
        """
        List all rooms in the tenant and return as a list (for backward compatibility).

        Args:
            page_size: Number of rooms to fetch per page (default: 100)

        Returns:
            List of MSGraphRoom objects
        """
        return list(self.list_rooms(page_size))

    def list_room_lists(self) -> list[dict[str, Any]]:
        """
        List all room lists in the tenant.

        Returns:
            List of room list dictionaries
        """
        response = self._make_request("GET", "/places/microsoft.graph.roomlist")
        return response.get("value", [])

    def list_rooms_in_room_list(self, room_list_email: str) -> list[MSGraphRoom]:
        """
        List rooms in a specific room list.

        Args:
            room_list_email: Email address of the room list

        Returns:
            List of MSGraphRoom objects
        """
        endpoint = f"/places/{room_list_email}/microsoft.graph.roomlist/rooms"
        response = self._make_request("GET", endpoint)

        rooms = []
        for room_data in response.get("value", []):
            room = MSGraphRoom(
                id=room_data["id"],
                display_name=room_data["displayName"],
                email_address=room_data["emailAddress"],
                capacity=room_data.get("capacity"),
                building=room_data.get("building"),
                floor_number=room_data.get("floorNumber"),
                phone=room_data.get("phone"),
                is_wheelchair_accessible=room_data.get("isWheelChairAccessible", False),
                original_payload=room_data,
            )
            rooms.append(room)

        return rooms

    def get_room(self, room_id: str) -> MSGraphRoom:
        """
        Get a specific room by ID.

        Args:
            room_id: Room ID

        Returns:
            MSGraphRoom object
        """
        response = self._make_request("GET", f"/places/{room_id}")

        return MSGraphRoom(
            id=response["id"],
            display_name=response["displayName"],
            email_address=response["emailAddress"],
            capacity=response.get("capacity"),
            building=response.get("building"),
            floor_number=response.get("floorNumber"),
            phone=response.get("phone"),
            is_wheelchair_accessible=response.get("isWheelChairAccessible", False),
            original_payload=response,
        )

    def find_meeting_times(
        self,
        attendees: list[str],
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        max_candidates: int = 20,
        meeting_duration: int = 60,  # minutes
        minimum_attendee_percentage: float = 100.0,
    ) -> dict[str, Any]:
        """
        Find optimal meeting times based on attendee availability.

        Args:
            attendees: List of attendee email addresses
            start_time: Earliest possible start time
            end_time: Latest possible end time
            max_candidates: Maximum number of suggestions
            meeting_duration: Meeting duration in minutes
            minimum_attendee_percentage: Minimum percentage of attendees required

        Returns:
            Meeting time suggestions response
        """
        data = {
            "attendees": [{"emailAddress": {"address": email}} for email in attendees],
            "timeConstraint": {
                "timeslots": [
                    {
                        "start": self._format_datetime(start_time),
                        "end": self._format_datetime(end_time),
                    }
                ]
            },
            "meetingDuration": f"PT{meeting_duration}M",
            "maxCandidates": max_candidates,
            "minimumAttendeePercentage": minimum_attendee_percentage,
        }

        return self._make_request("POST", f"/users/{self.user_id}/calendar/getSchedule", data=data)

    def get_free_busy_schedule(
        self,
        schedules: list[str],
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        interval: int = 60,  # minutes
    ) -> dict[str, Any]:
        """
        Get free/busy schedule information for users and resources.

        Args:
            schedules: List of email addresses to check
            start_time: Start time for the schedule query
            end_time: End time for the schedule query
            interval: Time interval in minutes

        Returns:
            Free/busy schedule information
        """
        data = {
            "schedules": schedules,
            "startTime": self._format_datetime(start_time),
            "endTime": self._format_datetime(end_time),
            "availabilityViewInterval": interval,
        }

        return self._make_request("POST", "/me/calendar/getSchedule", data=data)

    # Helper Methods

    def _parse_event(self, event_data: dict[str, Any]) -> MSGraphEvent:
        """
        Parse Microsoft Graph event data into MSGraphEvent object.

        Args:
            event_data: Raw event data from API

        Returns:
            MSGraphEvent object
        """
        return MSGraphEvent(
            id=event_data["id"],
            calendar_id=event_data.get("calendarId", "primary"),
            subject=event_data.get("subject", ""),
            body_content=event_data.get("body", {}).get("content", ""),
            start_time=self._parse_datetime(event_data["start"]),
            end_time=self._parse_datetime(event_data["end"]),
            timezone=event_data["start"].get("timeZone", "UTC"),
            location=event_data.get("location", {}).get("displayName", ""),
            attendees=event_data.get("attendees", []),
            organizer=event_data.get("organizer", {}),
            is_cancelled=event_data.get("isCancelled", False),
            recurrence_pattern=event_data.get("recurrence"),
            original_payload=event_data,
        )

    def _format_attendees(self, attendees: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Format attendees for Microsoft Graph API.

        Args:
            attendees: List of attendee dictionaries

        Returns:
            Formatted attendees list
        """
        formatted = []
        for attendee in attendees:
            formatted_attendee = {
                "emailAddress": {
                    "address": attendee["email"],
                    "name": attendee.get("name", attendee["email"]),
                },
                "type": attendee.get("type", "required"),
            }
            if "status" in attendee:
                formatted_attendee["status"] = {
                    "response": attendee["status"],
                    "time": "0001-01-01T00:00:00Z",
                }
            formatted.append(formatted_attendee)

        return formatted

    def get_user_info(self) -> dict[str, Any]:
        """
        Get current user information.

        Returns:
            User information dictionary
        """
        return self._make_request("GET", f"/users/{self.user_id}")

    def test_connection(self) -> bool:
        """
        Test the API connection.

        Returns:
            True if connection is successful, False otherwise
        """
        try:
            self.get_user_info()
            return True
        except MSGraphAPIError:
            return False

    def create_subscription(
        self,
        resource: str,
        change_type: str,
        notification_url: str,
        expiration_datetime: datetime.datetime | None = None,
        client_state: str | None = None,
    ) -> dict[str, Any]:
        """
        Create a webhook subscription for Microsoft Graph resources.

        Args:
            resource: The resource to subscribe to (e.g., '/me/calendars/{calendarId}/events')
            change_type: The type of changes to monitor ('created', 'updated', 'deleted')
            notification_url: The URL where notifications will be sent
            expiration_datetime: When the subscription expires (max 3 days for user resources)
            client_state: Optional client state value to validate notifications

        Returns:
            Created subscription data
        """
        if expiration_datetime is None:
            # Default to 3 days from now (maximum for user resources)
            expiration_datetime = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=3)

        subscription_data = {
            "changeType": change_type,
            "notificationUrl": notification_url,
            "resource": resource,
            "expirationDateTime": expiration_datetime.isoformat(),
        }

        if client_state:
            subscription_data["clientState"] = client_state

        return self._make_request("POST", "/subscriptions", data=subscription_data)

    def list_subscriptions(self) -> list[dict[str, Any]]:
        """
        List all active subscriptions.

        Returns:
            List of subscription data
        """
        response = self._make_request("GET", "/subscriptions")
        return response.get("value", [])

    def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        """
        Get details of a specific subscription.

        Args:
            subscription_id: The ID of the subscription

        Returns:
            Subscription data
        """
        return self._make_request("GET", f"/subscriptions/{subscription_id}")

    def update_subscription(
        self,
        subscription_id: str,
        expiration_datetime: datetime.datetime,
    ) -> dict[str, Any]:
        """
        Update a subscription's expiration time.

        Args:
            subscription_id: The ID of the subscription
            expiration_datetime: New expiration datetime

        Returns:
            Updated subscription data
        """
        subscription_data = {
            "expirationDateTime": expiration_datetime.isoformat(),
        }

        return self._make_request(
            "PATCH", f"/subscriptions/{subscription_id}", data=subscription_data
        )

    def delete_subscription(self, subscription_id: str) -> None:
        """
        Delete a subscription.

        Args:
            subscription_id: The ID of the subscription to delete
        """
        self._make_request("DELETE", f"/subscriptions/{subscription_id}")

    def subscribe_to_calendar_events(
        self,
        calendar_id: str | None = None,
        notification_url: str | None = None,
        change_types: list[str] | None = None,
        expiration_datetime: datetime.datetime | None = None,
        client_state: str | None = None,
    ) -> dict[str, Any]:
        """
        Subscribe to calendar event changes.

        Args:
            calendar_id: The calendar ID to subscribe to (defaults to primary calendar)
            notification_url: The URL where notifications will be sent
            change_types: List of change types to monitor (defaults to ['created', 'updated', 'deleted'])
            expiration_datetime: When the subscription expires
            client_state: Optional client state value

        Returns:
            Created subscription data
        """
        if calendar_id is None:
            calendar_id = "primary"

        if change_types is None:
            change_types = ["created", "updated", "deleted"]

        if notification_url is None:
            raise ValueError("notification_url is required for webhook subscriptions")

        # Microsoft Graph requires comma-separated change types
        change_type = ",".join(change_types)

        # Resource path for calendar events
        if calendar_id == "primary":
            resource = "/me/events"
        else:
            resource = f"/me/calendars/{calendar_id}/events"

        return self.create_subscription(
            resource=resource,
            change_type=change_type,
            notification_url=notification_url,
            expiration_datetime=expiration_datetime,
            client_state=client_state,
        )

    def unsubscribe_from_calendar_events(self, subscription_id: str) -> None:
        """
        Unsubscribe from calendar event changes.

        Args:
            subscription_id: The ID of the subscription to delete
        """
        self.delete_subscription(subscription_id)

    def subscribe_to_room_events(
        self,
        room_email: str,
        notification_url: str,
        change_types: list[str] | None = None,
        expiration_datetime: datetime.datetime | None = None,
        client_state: str | None = None,
    ) -> dict[str, Any]:
        """
        Subscribe to room booking/calendar event changes.

        Args:
            room_email: The email address of the room/resource
            notification_url: The URL where notifications will be sent
            change_types: List of change types to monitor (defaults to ['created', 'updated', 'deleted'])
            expiration_datetime: When the subscription expires
            client_state: Optional client state value

        Returns:
            Created subscription data
        """
        if change_types is None:
            change_types = ["created", "updated", "deleted"]

        if not room_email:
            raise ValueError("room_email is required for room event subscriptions")

        # Microsoft Graph requires comma-separated change types
        change_type = ",".join(change_types)

        # Resource path for room events - rooms are treated as users with calendars
        resource = f"/users/{room_email}/events"

        return self.create_subscription(
            resource=resource,
            change_type=change_type,
            notification_url=notification_url,
            expiration_datetime=expiration_datetime,
            client_state=client_state,
        )

    def subscribe_to_multiple_room_events(
        self,
        room_emails: list[str],
        notification_url: str,
        change_types: list[str] | None = None,
        expiration_datetime: datetime.datetime | None = None,
        client_state: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Subscribe to multiple room booking/calendar event changes.

        Args:
            room_emails: List of room email addresses
            notification_url: The URL where notifications will be sent
            change_types: List of change types to monitor (defaults to ['created', 'updated', 'deleted'])
            expiration_datetime: When the subscription expires
            client_state: Optional client state value

        Returns:
            List of created subscription data
        """
        subscriptions = []
        errors = []

        for room_email in room_emails:
            try:
                subscription = self.subscribe_to_room_events(
                    room_email=room_email,
                    notification_url=notification_url,
                    change_types=change_types,
                    expiration_datetime=expiration_datetime,
                    client_state=client_state,
                )
                subscriptions.append(subscription)
            except MSGraphAPIError as e:
                errors.append({"room_email": room_email, "error": str(e)})

        if errors:
            logger.warning("Failed to create subscriptions for some rooms: %s", errors)

        return subscriptions

    def get_room_events(
        self,
        room_email: str,
        start_time: datetime.datetime | None = None,
        end_time: datetime.datetime | None = None,
        timezone: str = "UTC",
    ) -> list[MSGraphEvent]:
        """
        Get calendar events for a specific room.

        Args:
            room_email: The email address of the room
            start_time: Start time for events (optional)
            end_time: End time for events (optional)
            timezone: Timezone for the query

        Returns:
            List of MSGraphEvent objects
        """
        params = {"$orderby": "start/dateTime"}

        if start_time and end_time:
            # Use calendar view for time-bounded queries
            params.update(
                {
                    "startDateTime": self._format_datetime(start_time, timezone)["dateTime"],
                    "endDateTime": self._format_datetime(end_time, timezone)["dateTime"],
                }
            )
            endpoint = f"/users/{room_email}/calendarView"
        else:
            endpoint = f"/users/{room_email}/events"

        response = self._make_request("GET", endpoint, params=params)

        events = []
        for event_data in response.get("value", []):
            event = self._parse_event(event_data)
            events.append(event)

        return events

    def unsubscribe_from_room_events(self, room_email: str) -> None:
        """
        Unsubscribe from room booking/calendar event changes.
        Finds and deletes all subscriptions for the specified room.

        Args:
            room_email: The email address of the room
        """
        try:
            # List all subscriptions to find the ones for this room
            subscriptions = self.list_subscriptions()

            # Find subscriptions for this room
            room_subscriptions = []
            for subscription in subscriptions:
                resource = subscription.get("resource", "")
                if resource == f"/users/{room_email}/events":
                    room_subscriptions.append(subscription)

            if not room_subscriptions:
                logger.warning(
                    "No subscriptions found for room %s",
                    room_email,
                )
                return

            # Delete all subscriptions for this room
            for subscription in room_subscriptions:
                subscription_id = subscription.get("id")
                if subscription_id:
                    self.delete_subscription(subscription_id)
                    logger.info(
                        "Successfully unsubscribed from room events. Room: %s, Subscription ID: %s",
                        room_email,
                        subscription_id,
                    )

        except MSGraphAPIError as e:
            raise MSGraphAPIError(f"Failed to unsubscribe from room events: {e}") from e
