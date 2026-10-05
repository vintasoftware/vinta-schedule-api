from django.urls import path

from common.types import RouteDict

from .microsoft_connection_views import (
    MicrosoftConnectionVerifyView,
    MicrosoftConsentCallbackView,
    MicrosoftConsentUrlView,
)
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

# Non-viewset routes. Included ahead of the router in the root URLconf: the router's
# `calendar/<path:pk>/` detail route would otherwise swallow these paths.
extra_patterns = [
    path(
        "calendar/microsoft-connection/consent-url/",
        MicrosoftConsentUrlView.as_view(),
        name="microsoft-connection-consent-url",
    ),
    path(
        "calendar/microsoft-connection/verify/",
        MicrosoftConnectionVerifyView.as_view(),
        name="microsoft-connection-verify",
    ),
    # Unauthenticated: Microsoft redirects the browser here after admin consent. The
    # organization comes only from the signed state. See MicrosoftConsentCallbackView.
    path(
        "calendar/microsoft-connection/callback/",
        MicrosoftConsentCallbackView.as_view(),
        name="microsoft-connection-callback",
    ),
]
