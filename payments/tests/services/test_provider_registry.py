"""Adapter conformance suite.

This phase's acceptance criterion is explicit: "no interface method exists that
only one provider can implement." This suite proves that mechanically —
enumerating every ``@abstractmethod`` declared on ``BasePaymentAdapter`` /
``BaseSubscriptionAdapter`` and asserting every *registered* adapter actually
overrides it (as opposed to silently inheriting the base's
``raise NotImplementedError`` stub). It also runs the same
signature-verification + idempotency-key assertions, parametrized, against both
``mercadopago`` and ``stripe``, and confirms both are reachable through the DI
``payment_provider_registry`` / ``subscription_provider_registry`` used by the
webhook views.
"""

import hashlib
import hmac
import inspect
import json
import time
from collections.abc import Callable

import pytest

from payments.constants import PaymentProviders
from payments.services.payment_adapters.base import BasePaymentAdapter
from payments.services.payment_adapters.mercadopago_payment_adapter import MercadoPagoPaymentAdapter
from payments.services.payment_adapters.stripe_payment_adapter import StripePaymentAdapter
from payments.services.subscription_adapters.base import BaseSubscriptionAdapter
from payments.services.subscription_adapters.mercadopago_subscription_adapter import (
    MercadoPagoSubscriptionAdapter,
)
from payments.services.subscription_adapters.stripe_subscription_adapter import (
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


def _abstract_method_names(base_class: type) -> list[str]:
    """Every method on *base_class* still decorated ``@abstractmethod`` — i.e.
    the ones a concrete subclass is required to override to be usable."""
    return sorted(
        name
        for name, member in inspect.getmembers(base_class, predicate=inspect.isfunction)
        if getattr(member, "__isabstractmethod__", False)
    )


PAYMENT_ABSTRACT_METHODS = _abstract_method_names(BasePaymentAdapter)
SUBSCRIPTION_ABSTRACT_METHODS = _abstract_method_names(BaseSubscriptionAdapter)


class TestPaymentAdapterConformance:
    """Runs the same structural assertions against every registered payment adapter."""

    @pytest.mark.parametrize("adapter_class", PAYMENT_ADAPTER_CLASSES, ids=lambda c: c.__name__)
    @pytest.mark.parametrize("method_name", PAYMENT_ABSTRACT_METHODS)
    def test_every_abstract_method_is_overridden(
        self, adapter_class: type[BasePaymentAdapter], method_name: str
    ) -> None:
        """If a method is only ever overridden by one provider, this fails for
        the other — inheriting the base's ``raise NotImplementedError`` stub is
        not "supporting" the interface, it is silently not implementing it."""
        base_method = getattr(BasePaymentAdapter, method_name)
        adapter_method = getattr(adapter_class, method_name)
        assert adapter_method is not base_method, (
            f"{adapter_class.__name__} does not override {method_name!r} — calling "
            "it would raise NotImplementedError, meaning the base interface "
            "declared a method only some providers can actually implement."
        )

    @pytest.mark.parametrize("adapter_class", PAYMENT_ADAPTER_CLASSES, ids=lambda c: c.__name__)
    def test_declares_a_registered_provider_slug(
        self, adapter_class: type[BasePaymentAdapter]
    ) -> None:
        assert adapter_class.provider in PaymentProviders.values

    @pytest.mark.parametrize("adapter_class", PAYMENT_ADAPTER_CLASSES, ids=lambda c: c.__name__)
    def test_declares_verifies_full_body_explicitly(
        self, adapter_class: type[BasePaymentAdapter]
    ) -> None:
        """See ``BasePaymentAdapter.verifies_full_body``'s docstring — every
        concrete adapter must set this itself rather than lean on a base
        default, so the answer to "is the body trustworthy?" is never left to
        tribal knowledge about a specific provider's signing scheme."""
        assert isinstance(adapter_class.verifies_full_body, bool)

    def test_verifies_full_body_actually_differs_by_provider(self) -> None:
        """Sanity check that the flag isn't a copy-pasted constant: MercadoPago's
        HMAC covers a narrow manifest, Stripe's covers the whole body."""
        assert MercadoPagoPaymentAdapter.verifies_full_body is False
        assert StripePaymentAdapter.verifies_full_body is True


class TestSubscriptionAdapterConformance:
    """Runs the same structural assertions against every registered subscription adapter."""

    @pytest.mark.parametrize(
        "adapter_class", SUBSCRIPTION_ADAPTER_CLASSES, ids=lambda c: c.__name__
    )
    @pytest.mark.parametrize("method_name", SUBSCRIPTION_ABSTRACT_METHODS)
    def test_every_abstract_method_is_overridden(
        self, adapter_class: type[BaseSubscriptionAdapter], method_name: str
    ) -> None:
        base_method = getattr(BaseSubscriptionAdapter, method_name)
        adapter_method = getattr(adapter_class, method_name)
        assert adapter_method is not base_method, (
            f"{adapter_class.__name__} does not override {method_name!r} — calling "
            "it would raise NotImplementedError, meaning the base interface "
            "declared a method only some providers can actually implement."
        )

    @pytest.mark.parametrize(
        "adapter_class", SUBSCRIPTION_ADAPTER_CLASSES, ids=lambda c: c.__name__
    )
    def test_declares_a_registered_provider_slug(
        self, adapter_class: type[BaseSubscriptionAdapter]
    ) -> None:
        assert adapter_class.provider in PaymentProviders.values

    @pytest.mark.parametrize(
        "adapter_class", SUBSCRIPTION_ADAPTER_CLASSES, ids=lambda c: c.__name__
    )
    def test_declares_verifies_full_body_explicitly(
        self, adapter_class: type[BaseSubscriptionAdapter]
    ) -> None:
        assert isinstance(adapter_class.verifies_full_body, bool)


# --- Signature verification + idempotency key, parametrized across providers ---

WEBHOOK_SECRET = "shared-test-webhook-secret"


def _sign_mercadopago(data_id: str, ts: str) -> tuple[bytes, dict[str, str]]:
    raw_body = json.dumps(
        {"type": "payment", "action": "payment.update", "data": {"id": data_id}}
    ).encode()
    request_id = "req-123"
    manifest = f"id:{data_id.lower()};request-id:{request_id};ts:{ts};"
    signature = hmac.new(WEBHOOK_SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    headers = {"x-signature": f"ts={ts},v1={signature}", "x-request-id": request_id}
    return raw_body, headers


def _sign_stripe(data_id: str, ts: str) -> tuple[bytes, dict[str, str]]:
    raw_body = json.dumps(
        {
            "id": "evt_123",
            "object": "event",
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": data_id, "object": "payment_intent"}},
        }
    ).encode()
    signed_payload = f"{ts}.".encode() + raw_body
    signature = hmac.new(WEBHOOK_SECRET.encode(), signed_payload, hashlib.sha256).hexdigest()
    headers = {"stripe-signature": f"t={ts},v1={signature}"}
    return raw_body, headers


PAYMENT_CONFORMANCE_CASES: list[tuple[BasePaymentAdapter, Callable[[str, str], tuple]]] = [
    (MercadoPagoPaymentAdapter("access-token", webhook_secret=WEBHOOK_SECRET), _sign_mercadopago),
    (StripePaymentAdapter("sk_test", webhook_secret=WEBHOOK_SECRET), _sign_stripe),
]


@pytest.mark.parametrize(
    "adapter,signer",
    PAYMENT_CONFORMANCE_CASES,
    ids=[c[0].provider for c in PAYMENT_CONFORMANCE_CASES],
)
def test_valid_signature_is_accepted_and_yields_a_stable_event_id(adapter, signer):
    """Same assertions, both providers: a correctly-signed request verifies, and
    calling `get_event_id` twice on the identical bytes is deterministic — the
    property the idempotency ledger's uniqueness constraint depends on."""
    ts = str(int(time.time()))
    raw_body, headers = signer("data-id-123", ts)

    assert adapter.verify_signature(raw_body, headers) is True
    event_id_first = adapter.get_event_id(raw_body, headers, payload={"id": "irrelevant"})
    event_id_second = adapter.get_event_id(raw_body, headers, payload={"id": "different-too"})
    assert event_id_first == event_id_second


@pytest.mark.parametrize(
    "adapter,signer",
    PAYMENT_CONFORMANCE_CASES,
    ids=[c[0].provider for c in PAYMENT_CONFORMANCE_CASES],
)
def test_tampered_body_is_rejected(adapter, signer):
    ts = str(int(time.time()))
    raw_body, headers = signer("data-id-123", ts)
    tampered_body = raw_body.replace(b"data-id-123", b"data-id-999")

    assert adapter.verify_signature(tampered_body, headers) is False


@pytest.mark.parametrize(
    "adapter,signer",
    PAYMENT_CONFORMANCE_CASES,
    ids=[c[0].provider for c in PAYMENT_CONFORMANCE_CASES],
)
def test_stale_signature_is_rejected(adapter, signer):
    stale_ts = str(int(time.time()) - 3600)
    raw_body, headers = signer("data-id-123", stale_ts)

    assert adapter.verify_signature(raw_body, headers) is False


@pytest.mark.parametrize(
    "adapter,signer",
    PAYMENT_CONFORMANCE_CASES,
    ids=[c[0].provider for c in PAYMENT_CONFORMANCE_CASES],
)
def test_missing_secret_fails_closed(adapter, signer):
    """An unconfigured webhook secret must reject every request, never skip
    verification — regardless of which provider."""
    ts = str(int(time.time()))
    raw_body, headers = signer("data-id-123", ts)
    original_secret = adapter.webhook_secret
    try:
        adapter.webhook_secret = ""
        assert adapter.verify_signature(raw_body, headers) is False
    finally:
        adapter.webhook_secret = original_secret


# --- DI registry wiring ---


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

    def test_subscription_provider_registry_contains_both_providers(self, di_container):
        registry = di_container.subscription_provider_registry()

        assert set(registry.keys()) == {PaymentProviders.MERCADOPAGO, PaymentProviders.STRIPE}
        assert isinstance(registry[PaymentProviders.MERCADOPAGO], MercadoPagoSubscriptionAdapter)
        assert isinstance(registry[PaymentProviders.STRIPE], StripeSubscriptionAdapter)
