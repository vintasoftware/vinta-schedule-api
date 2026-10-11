from audit_integration.containers import AuditContainer
from calendar_integration.containers import CalendarContainer
from payments.containers import BillingContainer
from webhooks.containers import WebhooksContainer


class OrganizationsContainer(
    CalendarContainer,
    WebhooksContainer,
    BillingContainer,
    AuditContainer,
):
    """Providers for the organization services."""
