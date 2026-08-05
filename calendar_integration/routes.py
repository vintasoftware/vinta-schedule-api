from common.types import RouteDict

from .views import (
    AvailableTimeViewSet,
    BlockedTimeViewSet,
    BookingPolicyViewSet,
    CalendarEventViewSet,
    CalendarGroupViewSet,
    CalendarViewSet,
    ExternalEventChangeRequestViewSet,
    GroupScopedAvailabilityWindowViewSet,
    GroupScopedBlockedTimeViewSet,
    GroupScopedQuotaRuleViewSet,
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
        "regex": r"calendar-groups/<int:group_id>/slots/<int:slot_id>/availability-windows",
        "viewset": GroupScopedAvailabilityWindowViewSet,
        "basename": "GroupScopedAvailabilityWindows",
    },
    {
        "regex": r"calendar-groups/<int:group_id>/slots/<int:slot_id>/blocked-times",
        "viewset": GroupScopedBlockedTimeViewSet,
        "basename": "GroupScopedBlockedTimes",
    },
    {
        "regex": r"calendar-groups/<int:group_id>/slots/<int:slot_id>/quota-rules",
        "viewset": GroupScopedQuotaRuleViewSet,
        "basename": "GroupScopedQuotaRules",
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
    {
        "regex": r"change-requests",
        "viewset": ExternalEventChangeRequestViewSet,
        "basename": "ChangeRequests",
    },
    {
        "regex": r"booking-policies",
        "viewset": BookingPolicyViewSet,
        "basename": "BookingPolicies",
    },
]
