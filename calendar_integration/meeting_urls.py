"""What a video meeting link must look like to be stored on an event."""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator


#: The longest meeting link ``CalendarEvent.meeting_url`` holds.
MEETING_URL_MAX_LENGTH = 2048


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
