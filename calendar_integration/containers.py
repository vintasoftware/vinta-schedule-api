from audit_integration.containers import AuditContainer
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
