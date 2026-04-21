from common.types import RouteDict

from .views import (
    AvailableTimeViewSet,
    BlockedTimeViewSet,
    CalendarEventViewSet,
    CalendarGroupViewSet,
    CalendarViewSet,
)


routes: list[RouteDict] = [
    {
        "regex": r"calendar-events",
        "viewset": CalendarEventViewSet,
        "basename": "CalendarEvents",
    },
    {
        "regex": r"calendar-groups",
        "viewset": CalendarGroupViewSet,
        "basename": "CalendarGroups",
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
