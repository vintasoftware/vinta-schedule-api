from dependency_injector import providers

from audit_integration.containers import AuditContainer
from calendar_integration.containers import CalendarContainer
from organizations.services import OrganizationService
from payments.containers import BillingContainer
from webhooks.containers import WebhooksContainer


class OrganizationsContainer(
    CalendarContainer,
    WebhooksContainer,
    BillingContainer,
    AuditContainer,
):
    """Providers for the organization services."""

    organization_service = providers.Factory(
        OrganizationService,
        calendar_service=CalendarContainer.calendar_service,
        webhook_membership_side_effects_service=(
            WebhooksContainer.webhook_membership_side_effects_service
        ),
        audit_service=AuditContainer.audit_service,
        subscription_service=BillingContainer.subscription_service,
        entitlement_service=BillingContainer.entitlement_service,
    )
