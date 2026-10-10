from dependency_injector import providers

from audit_integration.containers import AuditContainer
from calendar_integration.services.appointment_type_service import AppointmentTypeService
from calendar_integration.services.bookable_slots_service import BookableSlotsService
from calendar_integration.services.booking_policy_permission_service import (
    BookingPolicyPermissionService,
)
from calendar_integration.services.booking_policy_service import BookingPolicyService
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.calendar_side_effects_service import CalendarSideEffectsService
from calendar_integration.services.external_client_identifier_service import (
    ExternalClientIdentifierService,
)
from calendar_integration.services.external_event_change_request_service import (
    ExternalEventChangeRequestService,
)
from notifications.containers import NotificationsContainer
from payments.containers import BillingContainer
from webhooks.containers import WebhooksContainer


class CalendarContainer(
    WebhooksContainer,
    BillingContainer,
    NotificationsContainer,
    AuditContainer,
):
    """Providers for the calendar integration services."""

    calendar_side_effects_service = providers.Factory(
        CalendarSideEffectsService,
        # providers.List, not a plain tuple. dependency_injector only resolves a
        # provider passed as a direct kwarg value; one nested inside a tuple is
        # handed to the constructor as the Provider object itself. The pipeline
        # then held a Factory instead of a handler, every
        # ``isinstance(handler, On*Handler)`` check in CalendarSideEffectsService
        # returned False, and no calendar event webhook ever dispatched.
        side_effects_pipeline=providers.List(
            WebhooksContainer.webhook_calendar_side_effects_service
        ),
    )

    calendar_permission_service = providers.Factory(
        CalendarPermissionService,
        audit_service=AuditContainer.audit_service,
    )

    external_event_change_request_service = providers.Factory(
        ExternalEventChangeRequestService,
        audit_service=AuditContainer.audit_service,
        notification_service=NotificationsContainer.notification_service,
    )

    booking_policy_service = providers.Factory(
        BookingPolicyService,
        audit_service=AuditContainer.audit_service,
    )

    booking_policy_permission_service = providers.Factory(
        BookingPolicyPermissionService,
    )

    external_client_identifier_service = providers.Factory(
        ExternalClientIdentifierService,
    )

    calendar_service = providers.Factory(
        CalendarService,
        calendar_side_effects_service=calendar_side_effects_service,
        calendar_permission_service=calendar_permission_service,
        audit_service=AuditContainer.audit_service,
        external_event_change_request_service=external_event_change_request_service,
        booking_policy_service=booking_policy_service,
        entitlement_service=BillingContainer.entitlement_service,
        external_client_identifier_service=external_client_identifier_service,
    )

    bookable_slots_service = providers.Factory(
        BookableSlotsService,
        booking_policy_service=booking_policy_service,
    )

    appointment_type_service = providers.Factory(
        AppointmentTypeService,
        calendar_service=calendar_service,
        calendar_permission_service=calendar_permission_service,
        audit_service=AuditContainer.audit_service,
        booking_policy_service=booking_policy_service,
        entitlement_service=BillingContainer.entitlement_service,
    )
