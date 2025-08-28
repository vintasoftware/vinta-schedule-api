from common.types import RouteDict

from .views import CalendarEventViewSet, CalendarViewSet


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
]
