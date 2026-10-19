"""What a video meeting link must look like to be stored on an event."""

from urllib.parse import urlsplit

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from django.utils.crypto import salted_hmac


#: The longest meeting link ``CalendarEvent.meeting_url`` holds.
MEETING_URL_MAX_LENGTH = 2048

#: Salt that gives the audit hash of a meeting link its own key, derived from SECRET_KEY.
MEETING_URL_AUDIT_KEY_SALT = "calendar_integration.meeting_url_audit"


def is_meeting_url(url: str) -> bool:
    """Whether ``url`` can be saved as a meeting link: https, and short enough to store.

    Clients draw the link as one people click, so no other scheme is kept.
    """
    if not url or len(url) > MEETING_URL_MAX_LENGTH:
        return False
    try:
        URLValidator(schemes=["https"])(url)
    except DjangoValidationError:
        return False
    return True


def meeting_url_audit_fields(url: str) -> dict[str, str | None]:
    """What the audit trail records of a meeting link: its host and a keyed hash.

    The link itself is never recorded: a custom link often carries the meeting's
    passcode (Zoom's ``?pwd=``), and anyone reading the trail could join with it. The
    hash changes whenever the link does, so swapping one link for another on the same
    host still shows in the audit diff.
    """
    if not url:
        return {"meeting_host": None, "meeting_url_hash": None}
    return {
        "meeting_host": urlsplit(url).hostname,
        "meeting_url_hash": salted_hmac(
            MEETING_URL_AUDIT_KEY_SALT, url, algorithm="sha256"
        ).hexdigest(),
    }
