"""Tests for public_api.containers."""

from audit_integration.containers import AuditContainer
from di_core.containers import AppContainer
from payments.containers import BillingContainer
from public_api.containers import PublicApiContainer


def test_app_container_alias_is_public_api_container_provider() -> None:
    assert AppContainer.public_api_auth_service is PublicApiContainer.public_api_auth_service


def test_public_api_auth_service_is_wired_with_audit_and_entitlement_services() -> None:
    assert PublicApiContainer.public_api_auth_service.kwargs == {
        "audit_service": AuditContainer.audit_service,
        "entitlement_service": BillingContainer.entitlement_service,
    }


def test_public_api_auth_service_receives_the_container_services() -> None:
    container = AppContainer()

    service = container.public_api_auth_service()

    assert service.audit_service.repository is container.audit_repository()
    assert isinstance(service.entitlement_service, type(container.entitlement_service()))
