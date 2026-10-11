from dependency_injector import providers

from audit_integration.containers import AuditContainer
from payments.containers import BillingContainer
from public_api.services import PublicAPIAuthService


class PublicApiContainer(BillingContainer, AuditContainer):
    """Providers for the public API."""

    public_api_auth_service = providers.Factory(
        PublicAPIAuthService,
        audit_service=AuditContainer.audit_service,
        entitlement_service=BillingContainer.entitlement_service,
    )
