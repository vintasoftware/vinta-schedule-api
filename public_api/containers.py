from audit_integration.containers import AuditContainer
from payments.containers import BillingContainer


class PublicApiContainer(BillingContainer, AuditContainer):
    """Providers for the public API."""
