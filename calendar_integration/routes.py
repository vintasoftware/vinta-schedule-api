from common.types import RouteDict

from .views import (
    AppointmentTypeScopedAvailabilityWindowViewSet,
    AppointmentTypeScopedBlockedTimeViewSet,
    AppointmentTypeScopedQuotaRuleViewSet,
    AppointmentTypeViewSet,
    AvailableTimeViewSet,
    BlockedTimeViewSet,
    BookingCodeViewSet,
    BookingPolicyViewSet,
    CalendarEventViewSet,
    CalendarPoolViewSet,
    CalendarViewSet,
    ExternalEventChangeRequestViewSet,
)


routes: list[RouteDict] = [
    {
        "regex": r"calendar-events",
        "viewset": CalendarEventViewSet,
        "basename": "CalendarEvents",
    },
    {
        "regex": r"appointment-types",
        "viewset": AppointmentTypeViewSet,
        "basename": "AppointmentTypes",
    },
    {
        "regex": r"appointment-types/<int:appointment_type_id>/slots/<int:slot_id>/availability-windows",
        "viewset": AppointmentTypeScopedAvailabilityWindowViewSet,
        "basename": "AppointmentTypeScopedAvailabilityWindows",
    },
    {
        "regex": r"appointment-types/<int:appointment_type_id>/slots/<int:slot_id>/blocked-times",
        "viewset": AppointmentTypeScopedBlockedTimeViewSet,
        "basename": "AppointmentTypeScopedBlockedTimes",
    },
    {
        "regex": r"appointment-types/<int:appointment_type_id>/slots/<int:slot_id>/quota-rules",
        "viewset": AppointmentTypeScopedQuotaRuleViewSet,
        "basename": "AppointmentTypeScopedQuotaRules",
    },
    {
        "regex": r"calendar-pools",
        "viewset": CalendarPoolViewSet,
        "basename": "CalendarPools",
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
    {
        "regex": r"booking-codes",
        "viewset": BookingCodeViewSet,
        "basename": "BookingCodes",
    },
]
