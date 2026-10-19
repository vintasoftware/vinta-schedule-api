"""The Google event a blocked time with decline options is written as."""

import datetime
from typing import Any

from calendar_integration.constants import (
    BLOCKED_TIME_BUSY_TITLE,
    BLOCKED_TIME_OUT_OF_OFFICE_TITLE,
    BlockedTimeDeclineScope,
)


#: Google's ``autoDeclineMode`` for each decline scope of a blocked time.
AUTO_DECLINE_MODES: dict[BlockedTimeDeclineScope, str] = {
    BlockedTimeDeclineScope.NEW: "declineOnlyNewConflictingInvitations",
    BlockedTimeDeclineScope.NEW_AND_EXISTING: "declineAllConflictingInvitations",
}


def blocked_time_event_body(
    event_id: str,
    start_time: datetime.datetime,
    end_time: datetime.datetime,
    timezone: str,
    out_of_office: BlockedTimeDeclineScope | None,
) -> dict[str, Any]:
    """The Google event body for a blocked time.

    With ``out_of_office`` the event is an Out of office event titled "Out of office",
    which declines conflicting invitations per that scope with Google's default reply.
    Without it the event is a plain busy event titled "Busy", which declines nothing.
    The title is fixed and neutral: no text of the block's is sent.
    """
    body: dict[str, Any] = {
        "id": event_id,
        "summary": BLOCKED_TIME_BUSY_TITLE,
        "transparency": "opaque",
        "start": {"dateTime": start_time.isoformat(), "timeZone": timezone},
        "end": {"dateTime": end_time.isoformat(), "timeZone": timezone},
    }
    if out_of_office is not None:
        body["summary"] = BLOCKED_TIME_OUT_OF_OFFICE_TITLE
        body["eventType"] = "outOfOffice"
        body["outOfOfficeProperties"] = {"autoDeclineMode": AUTO_DECLINE_MODES[out_of_office]}
    return body
