"""Tests for organizations.containers."""

from dependency_injector import providers

from di_core.containers import AppContainer
from organizations.containers import OrganizationsContainer
from organizations.services import OrganizationService


BILLING_CONFIG = {
    "STRIPE_SECRET_KEY": "sk",
    "STRIPE_WEBHOOK_SECRET": "wh",
    "MERCADOPAGO_ACCESS_TOKEN": "at",
    "MERCADOPAGO_WEBHOOK_SECRET": "mw",
}


def test_app_container_alias_is_organizations_container_provider() -> None:
    assert AppContainer.organization_service is OrganizationsContainer.organization_service


def test_organizations_container_resolves_on_its_own() -> None:
    container = OrganizationsContainer()
    container.config.from_dict(BILLING_CONFIG)

    assert isinstance(container.organization_service(), OrganizationService)


def test_organization_service_receives_upstream_providers() -> None:
    container = AppContainer()
    calendar, membership, audit, subscription, entitlement = (object() for _ in range(5))

    with (
        container.calendar_service.override(providers.Object(calendar)),
        container.webhook_membership_side_effects_service.override(providers.Object(membership)),
        container.audit_service.override(providers.Object(audit)),
        container.subscription_service.override(providers.Object(subscription)),
        container.entitlement_service.override(providers.Object(entitlement)),
    ):
        service = container.organization_service()

    assert service.calendar_service is calendar
    assert service.webhook_membership_side_effects_service is membership
    assert service.audit_service is audit
    assert service.subscription_service is subscription
    assert service.entitlement_service is entitlement
