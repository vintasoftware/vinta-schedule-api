"""An in-memory Microsoft Graph room calendar, for the app-only room event sync tests.

It answers the requests the room sync makes through the real
``MSOutlookCalendarAPIClient``: ``GET /places/{id}`` (to find the room's mailbox) and
``GET /users/{mailbox}/calendarView/delta`` with Graph's paging and delta tokens. The
fixtures in this module put the real client back into the adapter module (the root
conftest replaces it with a MagicMock) and route its HTTP session here, so a test runs
everything from the task down to the HTTP call. The fixtures are ``ms_room_graph`` and
``ms_room_token_provider`` in ``calendar_integration/tests/conftest.py``.
"""

import datetime
import json
from typing import Any
from unittest.mock import Mock

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import Calendar, MicrosoftOrganizationConnection
from common.feature_flags import RESOURCE_CALENDAR_PROVIDER_SYNC
from organizations.models import Organization, OrganizationFeatureFlag


GRAPH_URL = "https://graph.microsoft.com/v1.0"
ROOM_PLACE_ID = "place-room-a"
ROOM_EMAIL = "room-a@contoso.com"
TENANT_ID = "11111111-2222-3333-4444-555555555555"


def graph_event(event_id: str, subject: str, start: datetime.datetime, hours: int = 1) -> dict:
    """A room calendar event as Graph's calendarView returns it, in UTC."""
    end = start + datetime.timedelta(hours=hours)
    return {
        "id": event_id,
        "subject": subject,
        "body": {"content": ""},
        "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "UTC"},
        "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "UTC"},
        "attendees": [],
        "organizer": {},
        "isCancelled": False,
    }


def _response(status_code: int, body: dict) -> Mock:
    response = Mock(status_code=status_code, ok=status_code < 400, headers={})
    response.json.return_value = body
    response.content = json.dumps(body).encode()
    return response


class FakeRoomCalendarGraph:
    """One room's calendar on Graph, with delta rounds.

    The initial round returns every event, ``page_size`` per page, then a delta link.
    A round started from a delta token returns what changed since that token was issued
    (edits in full, deletions as ``@removed``) and a new delta link.
    """

    def __init__(self, events: list[dict], page_size: int = 1):
        self.events = {event["id"]: event for event in events}
        self.page_size = page_size
        self.requests: list[tuple[str, str, dict | None]] = []
        self._rounds = 0
        self._changes: dict[str, dict] = {}

    def edit(self, event_id: str, **fields: Any) -> None:
        self.events[event_id].update(fields)
        self._changes[event_id] = self.events[event_id]

    def delete(self, event_id: str) -> None:
        self.events.pop(event_id, None)
        self._changes[event_id] = {"id": event_id, "@removed": {"reason": "deleted"}}

    def delta_requests(self) -> list[dict | None]:
        """The query parameters of every delta request, page requests included."""
        return [params for _, path, params in self.requests if "calendarView/delta" in path]

    def __call__(self, method, url, params=None, json=None, headers=None, timeout=None):
        path = url.removeprefix(GRAPH_URL)
        self.requests.append((method, path, params))
        if (method, path) == ("GET", f"/places/{ROOM_PLACE_ID}"):
            return _response(200, {"id": ROOM_PLACE_ID, "emailAddress": ROOM_EMAIL})
        delta_path = f"/users/{ROOM_EMAIL}/calendarView/delta"
        if method == "GET" and path.split("?")[0] == delta_path:
            if "?" in path:  # a next page: the skip token is the offset
                return self._page(int(path.split("$skiptoken=")[1]))
            if params and "$deltatoken" in params:
                changes = list(self._changes.values())
                self._changes = {}
                return _response(200, {"value": changes, **self._delta_link()})
            self._changes = {}
            return self._page(0)
        return _response(404, {"error": {"code": "ErrorItemNotFound", "message": path}})

    def _page(self, offset: int) -> Mock:
        events = list(self.events.values())
        page = events[offset : offset + self.page_size]
        if offset + self.page_size < len(events):
            next_link = (
                f"{GRAPH_URL}/users/{ROOM_EMAIL}/calendarView/delta"
                f"?$skiptoken={offset + self.page_size}"
            )
            return _response(200, {"value": page, "@odata.nextLink": next_link})
        return _response(200, {"value": page, **self._delta_link()})

    def _delta_link(self) -> dict:
        self._rounds += 1
        return {
            "@odata.deltaLink": (
                f"{GRAPH_URL}/users/{ROOM_EMAIL}/calendarView/delta"
                f"?$deltatoken=token-{self._rounds}"
            )
        }


def make_room(
    organization: Organization,
    *,
    flag_on: bool = True,
    write_enabled: bool = True,
    email: str = ROOM_EMAIL,
):
    """A Microsoft room calendar in ``organization``, with its connection and flag."""
    OrganizationFeatureFlag.objects.create(
        organization=organization, key=RESOURCE_CALENDAR_PROVIDER_SYNC, enabled=flag_on
    )
    MicrosoftOrganizationConnection.objects.create(
        organization=organization, tenant_id=TENANT_ID, write_enabled=write_enabled
    )
    return Calendar.objects.create(
        organization=organization,
        name="Room A",
        external_id=ROOM_PLACE_ID,
        email=email,
        provider=CalendarProvider.MICROSOFT,
        calendar_type=CalendarType.RESOURCE,
    )
