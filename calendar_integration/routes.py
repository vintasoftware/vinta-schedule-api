from common.types import RouteDict

from .views import CalendarAvailabilityViewSet, CalendarEventViewSet


routes: list[RouteDict] = [
    {
        "regex": r"calendar-events",
        "viewset": CalendarEventViewSet,
        "basename": "CalendarEvents",
    },
    {
        "regex": r"calendars",
        "viewset": CalendarAvailabilityViewSet,
        "basename": "CalendarAvailability",
    },
]
