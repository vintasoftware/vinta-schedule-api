from common.types import RouteDict

from .views import (
    AvailableTimeViewSet,
    BlockedTimeViewSet,
    CalendarEventViewSet,
    CalendarViewSet,
)


routes: list[RouteDict] = [
    {
        "regex": r"calendar-events",
        "viewset": CalendarEventViewSet,
        "basename": "CalendarEvents",
    },
    {
        "regex": r"calendar",
        "viewset": CalendarViewSet,
        "basename": "Calendars",
    },
    {
        "regex": r"blocked-times",
        "viewset": BlockedTimeViewSet,
        "basename": "BlockedTimes",
    },
    {
        "regex": r"available-times",
        "viewset": AvailableTimeViewSet,
        "basename": "AvailableTimes",
    },
]
