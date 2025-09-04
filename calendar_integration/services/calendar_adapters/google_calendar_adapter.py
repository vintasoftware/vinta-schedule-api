import datetime
import time
from collections.abc import Iterable
from typing import Any, Literal, TypedDict

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

import google.auth.crypt
import google.auth.jwt
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pyrate_limiter import Duration, Limiter, Rate, RedisBucket

from calendar_integration.constants import CalendarProvider
from calendar_integration.services.dataclasses import (
    ApplicationCalendarData,
    CalendarEventAdapterInputData,
    CalendarEventData,
    CalendarEventsSyncTypedDict,
    CalendarResourceData,
    EventAttendeeData,
)
from calendar_integration.services.protocols.calendar_adapter import CalendarAdapter
from common.redis import redis_connection


read_quote_limiter = Limiter(
    RedisBucket.init(
        [
            Rate(240, Duration.MINUTE),  # 240 requests per minute
        ],
        redis=redis_connection,
        bucket_key="google_calendar_read_limiter",
    ),
    raise_when_fail=False,
    max_delay=1000,  # Allow a maximum delay of 1 second for read operations
)

write_quote_limiter = Limiter(
    RedisBucket.init(
        [
            Rate(120, Duration.MINUTE),  # 120 requests per minute
        ],
        redis=redis_connection,
        bucket_key="google_calendar_write_limiter",
    ),
    raise_when_fail=False,
    max_delay=2000,  # Allow a maximum delay of 2 seconds for write operations
)


class GoogleCredentialTypedDict(TypedDict):
    token: str
    refresh_token: str
    account_id: str


class GoogleServiceAccountCredentialsTypedDict(TypedDict):
    account_id: str
    email: str
    audience: str
    public_key: str
    private_key_id: str
    private_key: str


class GoogleCalendarAdapter(CalendarAdapter):
    provider = "google"
    RSVP_STATUS_MAPPING: dict[str | None, Literal["pending", "accepted", "declined"]] = {  # noqa: RUF012
        "needsAction": "pending",
        "declined": "declined",
        "tentative": "pending",
        "accepted": "accepted",
        None: "pending",
    }
    RSVP_STATUS_INVERSE_MAPPING: dict[Literal["pending", "accepted", "declined"] | None, str] = {  # noqa: RUF012
        "pending": "needsAction",
        "accepted": "accepted",
        "declined": "declined",
        None: "needsAction",
    }

    def __init__(self, credentials_dict: GoogleCredentialTypedDict):
        self.account_id = credentials_dict["account_id"]
        GOOGLE_CLIENT_ID = getattr(settings, "GOOGLE_CLIENT_ID", None)  # noqa: N806
        GOOGLE_CLIENT_SECRET = getattr(settings, "GOOGLE_CLIENT_SECRET", None)  # noqa: N806
        if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
            raise ImproperlyConfigured(
                "Google Calendar integration requires GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET settings."
            )

        credentials = Credentials(
            token=credentials_dict["token"],
            refresh_token=credentials_dict["refresh_token"],
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
        )
        if (not credentials or not credentials.valid) and credentials.refresh_token:
            credentials.refresh(Request())
        elif not credentials or not credentials.valid:
            raise ValueError("Invalid or expired Google credentials provided.")

        self.client = build("calendar", "v3", credentials=credentials)

    @staticmethod
    def _generate_jwt(
        service_account_private_key_id: str,
        service_account_private_key: str,
        service_account_email: str,
        audience: str,
        expiry_length=3600,  # Default expiry length of 1 hour (3600 seconds
    ):
        """Generates a signed JSON Web Token using a Google API Service Account."""

        now = int(time.time())

        # build payload
        payload = {
            "iat": now,
            # expires after 'expiry_length' seconds.
            "exp": now + expiry_length,
            # iss must match 'issuer' in the security configuration in your
            # swagger spec (e.g. service account email). It can be any string.
            "iss": service_account_email,
            # aud must be either your Endpoints service name, or match the value
            # specified as the 'x-google-audience' in the OpenAPI document.
            "aud": audience,
            # sub and email should match the service account's email address
            "sub": service_account_email,
            "email": service_account_email,
        }

        # sign with keyfile
        signer = google.auth.crypt.RSASigner.from_service_account_info(
            {
                "private_key_id": service_account_private_key_id,
                "private_key": service_account_private_key,
            }
        )
        jwt = google.auth.jwt.encode(signer, payload)

        return jwt

    @classmethod
    def from_service_account_credentials(
        cls, service_account_credentials: GoogleServiceAccountCredentialsTypedDict
    ) -> "GoogleCalendarAdapter":
        """
        Creates an instance of GoogleCalendarAdapter using service account credentials.
        """
        GOOGLE_CLIENT_ID = getattr(settings, "GOOGLE_CLIENT_ID", None)  # noqa: N806
        GOOGLE_CLIENT_SECRET = getattr(settings, "GOOGLE_CLIENT_SECRET", None)  # noqa: N806
        if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
            raise ImproperlyConfigured(
                "Google Calendar integration requires GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET settings."
            )
        jwt_token = cls._generate_jwt(
            service_account_private_key_id=service_account_credentials["private_key_id"],
            service_account_private_key=service_account_credentials["private_key"],
            service_account_email=service_account_credentials["email"],
            audience=service_account_credentials["audience"],
        )
        credentials = Credentials(
            token=jwt_token,
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
        )
        if not credentials or not credentials.valid:
            raise ValueError("Invalid or expired Google service account credentials provided.")
        # Refresh the credentials if they are not valid
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        elif not credentials.valid:
            raise ValueError("Google service account credentials are not valid.")
        return cls(
            credentials_dict={
                "token": credentials.token,
                "refresh_token": credentials.refresh_token,
                "account_id": f"service-{service_account_credentials['account_id']}",
            }
        )

    def get_account_calendars(self) -> Iterable[CalendarResourceData]:
        read_quote_limiter.try_acquire(f"google_calendar_read_{self.account_id}")
        calendars_data = (
            self.client.calendars()
            .list(
                maxResults=250,  # Adjust as needed, Google API has a default limit
                showDeleted=False,
                minAccessRole="reader",  # Only fetch calendars where we have at least read access
            )
            .execute()
        )
        calendars = (
            CalendarResourceData(
                external_id=c["id"],
                name=c["summary"],
                description=c.get("description", ""),
                email=c.get("email", ""),
                is_default=c.get("primary", False),
                provider=self.provider,
                original_payload=c,
            )
            for c in calendars_data.get("items", [])
        )
        return calendars

    def create_application_calendar(self, name: str) -> ApplicationCalendarData:
        """
        Creates a new calendar for the application.
        """
        write_quote_limiter.try_acquire(f"google_calendar_write_{self.account_id}")
        calendar_result = (
            self.client.calendars()
            .insert(
                body={
                    "summary": f"_virtual_{name}",
                    "description": "Calendar created by Vinta Schedule for application use.",
                    "timeZone": "UTC",
                }
            )
            .execute()
        )
        return ApplicationCalendarData(
            id=None,
            external_id=calendar_result["id"],
            provider=CalendarProvider(self.provider),
            name=calendar_result["summary"],
            description=calendar_result.get("description", ""),
            email=calendar_result.get("email", ""),
            original_payload=calendar_result,
            organization_id=None,  # Will be set later in the sync process
        )

    def create_event(self, event_data: CalendarEventAdapterInputData) -> CalendarEventData:
        event = {
            "summary": event_data.title,
            "description": event_data.description,
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
                    "email": attendee.email,
                    "displayName": attendee.name,
                    "responseStatus": self.RSVP_STATUS_INVERSE_MAPPING.get(
                        attendee.status, "needsAction"
                    ),
                }
                for attendee in event_data.attendees
            ]
            + [
                {
                    "email": resource.email,
                    "displayName": resource.title,
                    "responseStatus": self.RSVP_STATUS_INVERSE_MAPPING.get(
                        resource.status, "needsAction"
                    ),
                }
                for resource in event_data.resources
            ],
        }

        # Add recurrence rule if provided
        if event_data.recurrence_rule and not event_data.is_recurring_instance:
            event["recurrence"] = [f"RRULE:{event_data.recurrence_rule}"]

        write_quote_limiter.try_acquire(f"google_calendar_write_{self.account_id}")
        created_event = (
            self.client.events()
            .insert(calendarId=event_data.calendar_external_id, body=event)
            .execute()
        )

        # Extract recurrence rule from response if present
        recurrence_rule = None
        if "recurrence" in created_event:
            for rule in created_event["recurrence"]:
                if rule.startswith("RRULE:"):
                    recurrence_rule = rule[6:]  # Remove "RRULE:" prefix
                    break

        return CalendarEventData(
            calendar_external_id=event_data.calendar_external_id,
            external_id=created_event["id"],
            title=created_event["summary"],
            description=created_event.get("description", ""),
            start_time=datetime.datetime.strptime(
                created_event["start"]["dateTime"], "%Y-%m-%dT%H:%M:%S"
            ).replace(tzinfo=datetime.UTC),
            end_time=datetime.datetime.strptime(
                created_event["end"]["dateTime"], "%Y-%m-%dT%H:%M:%S"
            ).replace(tzinfo=datetime.UTC),
            timezone=created_event.get("start", {}).get("timeZone"),
            original_payload=created_event,
            recurrence_rule=recurrence_rule,
            recurring_event_id=created_event.get("recurringEventId"),
            attendees=[
                EventAttendeeData(
                    email=attendee.get("email", ""),
                    name=attendee.get("displayName", ""),
                    status=self.RSVP_STATUS_MAPPING[attendee.get("responseStatus", "needsAction")],
                )
                for attendee in created_event.get("attendees", [])
            ],
        )

    def _convert_google_calendar_event_to_event_data(
        self, event: dict[str, Any], calendar_id: str
    ) -> CalendarEventData:
        # Extract recurrence rule if present
        recurrence_rule = None
        if "recurrence" in event:
            for rule in event["recurrence"]:
                if rule.startswith("RRULE:"):
                    recurrence_rule = rule[6:]  # Remove "RRULE:" prefix
                    break

        return CalendarEventData(
            calendar_external_id=calendar_id,
            external_id=event["id"],
            title=event["summary"],
            description=event.get("description", ""),
            start_time=datetime.datetime.strptime(
                event["start"]["dateTime"], "%Y-%m-%dT%H:%M:%S"
            ).replace(tzinfo=event.get("start", {}).get("timeZone")),
            end_time=datetime.datetime.strptime(
                event["end"]["dateTime"], "%Y-%m-%dT%H:%M:%S"
            ).replace(tzinfo=datetime.UTC),
            timezone=event.get("end", {}).get("timeZone"),
            original_payload=event,
            recurrence_rule=recurrence_rule,
            recurring_event_id=event.get("recurringEventId"),
            status="cancelled" if event.get("status") == "cancelled" else "confirmed",
            attendees=[
                EventAttendeeData(
                    email=attendee.get("email", ""),
                    name=attendee.get("displayName", ""),
                    status=self.RSVP_STATUS_MAPPING[attendee.get("responseStatus", "needsAction")],
                )
                for attendee in event.get("attendees", [])
            ],
        )

    def get_events(
        self,
        calendar_id: str,
        calendar_is_resource: bool,  # This parameter is not used in Google Calendar API, but kept for compatibility
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        sync_token: str | None = None,
        max_results_per_page: int = 250,
    ) -> CalendarEventsSyncTypedDict:
        extra_kwargs: dict[str, Any] = {"maxResults": max_results_per_page}

        if sync_token:
            extra_kwargs["syncToken"] = sync_token
            extra_kwargs["showDeleted"] = True

        # Create a generator that yields events one page at a time
        def events_iterator():
            page_token = None
            nonlocal next_sync_token
            next_sync_token = None

            while True:
                current_extra_kwargs = extra_kwargs.copy()
                if page_token:
                    current_extra_kwargs["pageToken"] = page_token

                read_quote_limiter.ratelimit(f"google_calendar_read_{self.account_id}", delay=True)
                events_result = (
                    self.client.events()
                    .list(
                        calendarId=calendar_id,
                        timeMin=start_date.isoformat(),
                        timeMax=end_date.isoformat(),
                        singleEvents=True,
                        orderBy="startTime",
                        **current_extra_kwargs,
                    )
                    .execute()
                )

                # Yield events from current page
                for event in events_result.get("items", []):
                    yield self._convert_google_calendar_event_to_event_data(event, calendar_id)

                page_token = events_result.get("nextPageToken")
                next_sync_token = events_result.get("nextSyncToken")

                if not page_token:
                    break

        next_sync_token = None

        return CalendarEventsSyncTypedDict(
            events=events_iterator(),
            next_sync_token=next_sync_token,
        )

    def get_event(self, calendar_id: str, event_id: str) -> CalendarEventData:
        read_quote_limiter.try_acquire(f"google_calendar_read_{self.account_id}")
        event = self.client.events().get(calendarId=calendar_id, eventId=event_id).execute()
        return CalendarEventData(
            calendar_external_id=calendar_id,
            external_id=event["id"],
            title=event["summary"],
            description=event.get("description", ""),
            start_time=datetime.datetime.strptime(
                event["start"]["dateTime"], "%Y-%m-%dT%H:%M:%S"
            ).replace(tzinfo=event.get("start", {}).get("timeZone")),
            end_time=datetime.datetime.strptime(
                event["end"]["dateTime"], "%Y-%m-%dT%H:%M:%S"
            ).replace(tzinfo=datetime.UTC),
            timezone=event.get("start", {}).get("timeZone"),
            original_payload=event,
            attendees=[
                EventAttendeeData(
                    email=attendee.get("email", ""),
                    name=attendee.get("displayName", ""),
                    status=self.RSVP_STATUS_MAPPING[attendee.get("responseStatus", "needsAction")],
                )
                for attendee in event.get("attendees", [])
            ],
            status=event.get("status", "confirmed"),
        )

    def update_event(self, calendar_id: str, event_id, event_data) -> CalendarEventData:
        event = {
            "summary": event_data.title,
            "description": event_data.description,
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
        }

        # Handle recurrence rule if present
        if hasattr(event_data, "recurrence_rule") and event_data.recurrence_rule:
            event["recurrence"] = [f"RRULE:{event_data.recurrence_rule}"]

        write_quote_limiter.try_acquire(f"google_calendar_write_{self.account_id}")
        updated_event = (
            self.client.events()
            .update(calendarId=calendar_id, eventId=event_id, body=event)
            .execute()
        )

        # Extract recurrence rule from response if present
        recurrence_rule = None
        if "recurrence" in updated_event:
            for rule in updated_event["recurrence"]:
                if rule.startswith("RRULE:"):
                    recurrence_rule = rule[6:]  # Remove "RRULE:" prefix
                    break

        return CalendarEventData(
            calendar_external_id=calendar_id,
            external_id=updated_event["id"],
            title=updated_event["summary"],
            description=updated_event.get("description", ""),
            start_time=datetime.datetime.strptime(
                updated_event["start"]["dateTime"], "%Y-%m-%dT%H:%M:%S"
            ).replace(tzinfo=datetime.UTC),
            end_time=datetime.datetime.strptime(
                updated_event["end"]["dateTime"], "%Y-%m-%dT%H:%M:%S"
            ).replace(tzinfo=datetime.UTC),
            timezone=updated_event.get("start", {}).get("timeZone"),
            original_payload=updated_event,
            recurrence_rule=recurrence_rule,
            recurring_event_id=updated_event.get("recurringEventId"),
            attendees=[
                EventAttendeeData(
                    email=attendee.get("email", ""),
                    name=attendee.get("displayName", ""),
                    status=self.RSVP_STATUS_MAPPING[attendee.get("responseStatus", "needsAction")],
                )
                for attendee in updated_event.get("attendees", [])
            ],
            status=updated_event.get("status", "confirmed"),
        )

    def delete_event(self, calendar_id: str, event_id: str):
        write_quote_limiter.try_acquire(f"google_calendar_write_{self.account_id}")
        self.client.events().delete(calendarId=calendar_id, eventId=event_id).execute()

    def get_calendar_resources(self) -> Iterable[CalendarResourceData]:
        read_quote_limiter.try_acquire(f"google_calendar_read_{self.account_id}")
        calendar_resources_result = self.client.calendarList().list().execute()
        calendar_resources = calendar_resources_result.get("items", [])
        for resource in calendar_resources:
            yield CalendarResourceData(
                external_id=resource["id"],
                name=resource["summary"],
                description=resource.get("description", ""),
                email=resource.get("email", ""),
                capacity=resource.get("capacity", 0),
                original_payload=resource,
                provider=self.provider,
            )

    def get_calendar_resource(self, resource_id: str) -> CalendarResourceData:
        read_quote_limiter.try_acquire(f"google_calendar_read_{self.account_id}")
        resource = self.client.calendarList().get(calendarId=resource_id).execute()
        return CalendarResourceData(
            external_id=resource["id"],
            name=resource["summary"],
            description=resource.get("description", ""),
            email=resource.get("email", ""),
            capacity=resource.get("capacity", 0),
            original_payload=resource,
            provider=self.provider,
        )

    def _get_paginated_resources(self, page_size=50) -> Iterable[list[CalendarResourceData]]:
        resources = self.get_calendar_resources()
        accumulated_resources = []
        for resource in resources:
            accumulated_resources.append(resource)
            if len(accumulated_resources) >= page_size:
                yield accumulated_resources
                accumulated_resources = []

        # If there are any remaining resources, yield them
        yield accumulated_resources

    def get_available_calendar_resources(
        self, start_time: datetime.datetime, end_time: datetime.datetime
    ) -> Iterable[CalendarResourceData]:
        # For very long date ranges, split into smaller chunks to avoid API limits
        max_days_per_query = 90  # Google's freebusy API works better with smaller ranges

        # Get all resources first
        all_resources = list(self.get_calendar_resources())
        if not all_resources:
            return

        # Track which resources are consistently available across all time chunks
        consistently_available_resources = set(
            resource.email for resource in all_resources if resource.email
        )

        # Split the date range into manageable chunks
        for time_chunk_start, time_chunk_end in self._split_date_range(
            start_time, end_time, max_days_per_query
        ):
            paginated_resources = self._get_paginated_resources()

            for resources_page in paginated_resources:
                resources_page_by_email = {
                    resource.email: resource for resource in resources_page if resource.email
                }

                if not resources_page_by_email:
                    continue

                # Only check resources that are still potentially available
                resources_to_check = {
                    email: resource
                    for email, resource in resources_page_by_email.items()
                    if email in consistently_available_resources
                }

                if not resources_to_check:
                    continue

                free_busy_page = (
                    self.client.freebusy()
                    .query(
                        body={
                            "timeMin": time_chunk_start.isoformat(),
                            "timeMax": time_chunk_end.isoformat(),
                            "items": [
                                {"id": resource_email}
                                for resource_email in resources_to_check.keys()
                            ],
                        }
                    )
                    .execute()
                )

                # Remove resources that are busy in this time chunk
                for resource_email, free_busy_data in free_busy_page.get("calendars", {}).items():
                    busy_times = free_busy_data.get("busy", [])
                    if busy_times:
                        # Resource is busy in this chunk, remove from available set
                        consistently_available_resources.discard(resource_email)

        # Yield resources that are available across the entire date range
        resources_by_email = {
            resource.email: resource for resource in all_resources if resource.email
        }
        for resource_email in consistently_available_resources:
            if resource_email in resources_by_email:
                yield resources_by_email[resource_email]

    def _split_date_range(
        self, start_time: datetime.datetime, end_time: datetime.datetime, max_days: int
    ) -> Iterable[tuple[datetime.datetime, datetime.datetime]]:
        """Split a large date range into smaller chunks for efficient API queries."""
        current_start = start_time

        while current_start < end_time:
            current_end = min(current_start + datetime.timedelta(days=max_days), end_time)
            yield current_start, current_end
            current_start = current_end

    def subscribe_to_calendar_events(self, resource_id: str, callback_url: str) -> None:
        """
        Subscribes to calendar events for a specific resource.
        This method sets up a push notification channel for the calendar events.
        """
        body = {
            "id": f"{resource_id}-subscription",
            "type": "web_hook",
            "address": callback_url,
            "params": {
                "ttl": 3600,  # Time to live for the subscription in seconds
            },
        }
        write_quote_limiter.try_acquire(f"google_calendar_write_{self.account_id}")
        self.client.events().watch(calendarId=resource_id, body=body).execute()

    def unsubscribe_from_calendar_events(self, resource_id: str) -> None:
        """
        Unsubscribes from calendar events for a specific resource.
        This method deletes the push notification channel for the calendar events.
        """
        write_quote_limiter.try_acquire(f"google_calendar_write_{self.account_id}")
        try:
            self.client.channels().stop(body={"id": f"{resource_id}-subscription"}).execute()
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"Failed to unsubscribe from calendar events: {e!s}") from e
