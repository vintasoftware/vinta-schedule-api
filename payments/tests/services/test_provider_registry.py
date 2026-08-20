"""DI registry wiring for the payment / subscription provider adapters.

The adapter conformance suite itself (every abstract method overridden, no
adapter-only public method, signature verification, idempotency-key stability)
now lives in ``vinta_billing``'s own ``tests/services/test_provider_registry.py``
-- the adapters it exercises there are the exact same classes this project
imports through the ``payments.services.payment_adapters``/
``subscription_adapters`` shims, so a regression on any of those properties
already turns the package's own suite red.

What stays here is what the package's suite cannot know: whether *this
project's* DI container actually wires both providers into the registries the
webhook views resolve a `provider` URL kwarg against.
"""

import pytest
from vinta_billing.constants import PaymentProviders
from vinta_billing.services.payment_adapters.base import BasePaymentAdapter
from vinta_billing.services.payment_adapters.mercadopago_payment_adapter import (
    MercadoPagoPaymentAdapter,
)
from vinta_billing.services.payment_adapters.stripe_payment_adapter import StripePaymentAdapter
from vinta_billing.services.subscription_adapters.base import BaseSubscriptionAdapter
from vinta_billing.services.subscription_adapters.mercadopago_subscription_adapter import (
    MercadoPagoSubscriptionAdapter,
)
from vinta_billing.services.subscription_adapters.stripe_subscription_adapter import (
    StripeSubscriptionAdapter,
)


PAYMENT_ADAPTER_CLASSES: list[type[BasePaymentAdapter]] = [
    MercadoPagoPaymentAdapter,
    StripePaymentAdapter,
]
SUBSCRIPTION_ADAPTER_CLASSES: list[type[BaseSubscriptionAdapter]] = [
    MercadoPagoSubscriptionAdapter,
    StripeSubscriptionAdapter,
]


@pytest.mark.django_db
class TestProviderRegistryDIWiring:
    """The `provider` URL kwarg on the webhook views selects an adapter out of
    these registries — both providers must actually be reachable through them,
    not just instantiable directly."""

    def test_payment_provider_registry_contains_both_providers(self, di_container):
        registry = di_container.payment_provider_registry()

        assert set(registry.keys()) == {PaymentProviders.MERCADOPAGO, PaymentProviders.STRIPE}
        assert isinstance(registry[PaymentProviders.MERCADOPAGO], MercadoPagoPaymentAdapter)
        assert isinstance(registry[PaymentProviders.STRIPE], StripePaymentAdapter)

    def test_adapter_class_lists_match_di_container_registrations(self, di_container):
        """``PAYMENT_ADAPTER_CLASSES``/``SUBSCRIPTION_ADAPTER_CLASSES`` are
        hand-maintained -- a lazily-read DI container global cannot back a bare
        module-level list a ``@pytest.mark.parametrize`` decorator needs (see the
        history of this module before this phase's trim) -- so this is what keeps
        them from silently drifting out of sync with the DI wiring: a third
        adapter registered in ``payment_provider_registry``/
        ``subscription_provider_registry`` without being added to the
        corresponding list here fails this test immediately."""
        payment_registry_classes = {
            type(adapter) for adapter in di_container.payment_provider_registry().values()
        }
        subscription_registry_classes = {
            type(adapter) for adapter in di_container.subscription_provider_registry().values()
        }

        assert payment_registry_classes == set(PAYMENT_ADAPTER_CLASSES)
        assert subscription_registry_classes == set(SUBSCRIPTION_ADAPTER_CLASSES)

    def test_subscription_provider_registry_contains_both_providers(self, di_container):
        registry = di_container.subscription_provider_registry()

        assert set(registry.keys()) == {PaymentProviders.MERCADOPAGO, PaymentProviders.STRIPE}
        assert isinstance(registry[PaymentProviders.MERCADOPAGO], MercadoPagoSubscriptionAdapter)
        assert isinstance(registry[PaymentProviders.STRIPE], StripeSubscriptionAdapter)

    def test_payment_service_still_resolves_both_registries_without_singular_gateways(
        self, di_container
    ):
        """``PaymentService`` no longer takes the singular
        ``payment_gateway``/``subscription_gateway`` DI providers --
        the two registries are its only adapter source. Proves the DI wiring
        still constructs a working ``PaymentService`` (auto-resolving
        ``payment_provider_resolver``/``payment_provider_registry``/
        ``subscription_provider_registry`` from the container) and that its
        ``get_payment_adapter``/``get_subscription_adapter`` reach the exact same
        registry-backed adapters the module-level tests above assert on."""
        payment_service = di_container.payment_service()

        assert not hasattr(payment_service, "payment_gateway")
        assert not hasattr(payment_service, "subscription_gateway")
        assert isinstance(
            payment_service.get_payment_adapter(PaymentProviders.MERCADOPAGO),
            MercadoPagoPaymentAdapter,
        )
        assert isinstance(
            payment_service.get_payment_adapter(PaymentProviders.STRIPE), StripePaymentAdapter
        )
        assert isinstance(
            payment_service.get_subscription_adapter(PaymentProviders.MERCADOPAGO),
            MercadoPagoSubscriptionAdapter,
        )
        assert isinstance(
            payment_service.get_subscription_adapter(PaymentProviders.STRIPE),
            StripeSubscriptionAdapter,
        )
