"""Tests for payments.containers."""

import pytest
from vinta_billing.constants import PaymentProviders

from di_core.containers import AppContainer
from payments.containers import BillingContainer


BILLING_PROVIDER_NAMES = [
    "payment_gateway",
    "subscription_gateway",
    "stripe_payment_gateway",
    "stripe_subscription_gateway",
    "payment_provider_registry",
    "subscription_provider_registry",
    "subscription_plan_factory",
    "payment_provider_resolver",
    "payment_service",
    "subscription_service",
    "entitlement_service",
    "metering_service",
    "dunning_service",
    "usage_warning_service",
    "cycle_close_service",
]

CONFIG = {
    "STRIPE_SECRET_KEY": "sk",
    "STRIPE_WEBHOOK_SECRET": "wh",
    "MERCADOPAGO_ACCESS_TOKEN": "at",
    "MERCADOPAGO_WEBHOOK_SECRET": "mw",
}


@pytest.mark.parametrize("name", BILLING_PROVIDER_NAMES)
def test_app_container_alias_is_billing_container_provider(name: str) -> None:
    assert getattr(AppContainer, name) is getattr(BillingContainer, name)


def test_payment_service_and_registry_use_config_values() -> None:
    container = AppContainer()
    container.config.from_dict(CONFIG)

    registry = container.payment_provider_registry()
    service = container.payment_service()

    stripe = registry[PaymentProviders.STRIPE]
    mercadopago = registry[PaymentProviders.MERCADOPAGO]
    assert (stripe.api_key, stripe.webhook_secret) == ("sk", "wh")
    assert (mercadopago.access_token, mercadopago.webhook_secret) == ("at", "mw")
    assert service.payment_provider_registry[PaymentProviders.STRIPE].api_key == "sk"
    assert service.subscription_provider_registry[PaymentProviders.MERCADOPAGO].access_token == "at"


def test_dunning_service_receives_the_shared_notification_service() -> None:
    container = AppContainer()
    container.config.from_dict(CONFIG)

    notification_service = container.notification_service()

    assert container.dunning_service().notification_service is notification_service
    assert container.usage_warning_service().notification_service is notification_service
