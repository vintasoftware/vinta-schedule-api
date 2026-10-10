from dependency_injector import providers

from audit_integration.containers import AuditContainer
from legal.services import ConsentService


class LegalContainer(AuditContainer):
    """Providers for legal consent."""

    consent_service = providers.Factory(
        ConsentService,
        audit_service=AuditContainer.audit_service,
    )
