import pytest

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import Calendar


ROOM_EMAIL = "c_1882room@resource.calendar.google.com"


@pytest.mark.parametrize(
    ("provider", "calendar_type", "email", "expected"),
    [
        # A Google room is addressed by its resourceEmail, not its Directory resourceId.
        (CalendarProvider.GOOGLE, CalendarType.RESOURCE, ROOM_EMAIL, ROOM_EMAIL),
        # Without an email there is nothing better to send than the external id.
        (CalendarProvider.GOOGLE, CalendarType.RESOURCE, "", "c_1882room"),
        (CalendarProvider.GOOGLE, CalendarType.PERSONAL, "someone@example.com", "c_1882room"),
        # Microsoft rooms have their own adapter path keyed by external id.
        (CalendarProvider.MICROSOFT, CalendarType.RESOURCE, ROOM_EMAIL, "c_1882room"),
    ],
)
def test_provider_calendar_id(
    provider: CalendarProvider, calendar_type: CalendarType, email: str, expected: str
) -> None:
    calendar = Calendar(
        external_id="c_1882room", provider=provider, calendar_type=calendar_type, email=email
    )

    assert calendar.provider_calendar_id == expected
