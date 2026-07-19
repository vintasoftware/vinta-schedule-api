"""Adapter conformance suite.

This phase's acceptance criterion is explicit: "no interface method exists that
only one provider can implement." This suite proves that mechanically —
enumerating every ``@abstractmethod`` declared on ``BasePaymentAdapter`` /
``BaseSubscriptionAdapter`` and asserting every *registered* adapter actually
overrides it (as opposed to silently inheriting the base's
``raise NotImplementedError`` stub), and separately asserting the set of
*public* methods (not just the formally-abstract ones) is identical across
adapters of the same kind — a method only one provider implements is exactly
as much of an interface violation whether or not it happens to be marked
``@abstractmethod``. It also runs the same signature-verification +
idempotency-key assertions, parametrized, against both ``mercadopago`` and
``stripe`` payment *and* subscription adapters, and confirms all four are
reachable through the DI ``payment_provider_registry`` /
``subscription_provider_registry`` used by the webhook views.

``PAYMENT_ADAPTER_CLASSES``/``SUBSCRIPTION_ADAPTER_CLASSES`` below are still a
hand-maintained list — *not* derived from the DI registries at module scope.
That was tried and reverted: ``di_core.apps.DICoreConfig.ready()`` calls
``container.wire(packages=INTERNAL_INSTALLED_APPS)`` *before* reassigning the
module-level ``di_core.containers.container`` global, and ``wire()`` imports
every module under those packages (including this one) to scan for
``@inject``-decorated callables — so any module-level statement here that
reads ``di_core.containers.container`` executes while it is still ``None``,
raising ``AttributeError`` during ``django.setup()`` itself, before pytest
even begins collection. Reading it lazily inside a fixture (as the
``di_container`` fixture in ``conftest.py`` already does) sidesteps this, but
a bare module-level list — which is what a ``@pytest.mark.parametrize``
decorator needs — cannot be lazy. Instead,
``test_adapter_class_lists_match_di_container_registrations`` below asserts
these hand-maintained lists exactly match what the DI registries expose, so a
third adapter registered there without being added here fails loudly instead
of silently going unexercised.
"""

import hashlib
import hmac
import inspect
import json
import time
from collections.abc import Callable
from typing import cast

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


def _public_method_names(cls: type) -> set[str]:
    """Every public (non-underscore-prefixed) method *cls* exposes — including
    ones that were never marked ``@abstractmethod`` on the base. A method only
    one provider implements is an interface leak regardless of whether it was
    formally declared abstract; enumerating abstract methods alone (as
    ``_abstract_method_names`` does) misses exactly that case."""
    return {
        name
        for name, _member in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    }


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

    def test_no_adapter_declares_a_public_method_the_others_lack(self) -> None:
        """Every *public* method — not just the formally-``@abstractmethod``
        ones — must be shared by every registered payment adapter. A method
        only one provider implements is exactly the kind of interface leak
        this phase's acceptance criterion rules out, whether or not it was
        declared abstract on the base."""
        method_sets = {cls: _public_method_names(cls) for cls in PAYMENT_ADAPTER_CLASSES}
        common = set.intersection(*method_sets.values())
        for cls, methods in method_sets.items():
            extra = methods - common
            assert not extra, (
                f"{cls.__name__} declares public method(s) {sorted(extra)} that no "
                "other payment adapter implements."
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

    def test_verifies_full_body_false_forces_get_event_id_override(self) -> None:
        """``verifies_full_body`` is load-bearing, not decorative: a payment
        adapter that declares ``verifies_full_body = False`` and does not
        override ``get_event_id`` must fail loudly the moment it's called,
        rather than silently deriving the idempotency ledger key from
        unsigned ``payload`` material (see ``BasePaymentAdapter.get_event_id``'s
        docstring).

        Exercises the base implementation directly (unbound, called against a
        minimal duck-typed double) rather than subclassing ``BasePaymentAdapter``
        without implementing its other abstract methods — mypy statically
        enforces ``@abstractmethod`` regardless of ``BasePaymentAdapter`` not
        inheriting ``ABC`` at runtime (see this suite's ``verify_signature``
        docstring / the phase's NIT on leaving that pre-existing gap alone).
        """

        class _NarrowManifestAdapterDouble:
            verifies_full_body = False

            def get_update_id(self, update_payload: dict) -> str | None:
                return update_payload.get("id")

        fake_adapter = cast(BasePaymentAdapter, _NarrowManifestAdapterDouble())
        with pytest.raises(NotImplementedError):
            BasePaymentAdapter.get_event_id(fake_adapter, b"{}", {}, {"id": "attacker-controlled"})


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

    def test_no_adapter_declares_a_public_method_the_others_lack(self) -> None:
        """Same assertion as the payment-adapter suite, applied to subscription
        adapters. This is what previously missed
        ``MercadoPagoSubscriptionAdapter.update_subscription_payment_token`` —
        a public method only MercadoPago implemented, now promoted onto
        ``BaseSubscriptionAdapter`` with a Stripe implementation
        (``stripe.Subscription.modify(default_payment_method=...)``) instead."""
        method_sets = {cls: _public_method_names(cls) for cls in SUBSCRIPTION_ADAPTER_CLASSES}
        common = set.intersection(*method_sets.values())
        for cls, methods in method_sets.items():
            extra = methods - common
            assert not extra, (
                f"{cls.__name__} declares public method(s) {sorted(extra)} that no "
                "other subscription adapter implements."
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

    def test_verifies_full_body_false_forces_get_event_id_override(self) -> None:
        """See ``TestPaymentAdapterConformance``'s equivalent — same enforcement,
        same reasoning, same duck-typed-double technique, on
        ``BaseSubscriptionAdapter``."""

        class _NarrowManifestAdapterDouble:
            verifies_full_body = False

            def get_update_id(self, update_payload: dict) -> str | None:
                return update_payload.get("id")

        fake_adapter = cast(BaseSubscriptionAdapter, _NarrowManifestAdapterDouble())
        with pytest.raises(NotImplementedError):
            BaseSubscriptionAdapter.get_event_id(
                fake_adapter, b"{}", {}, {"id": "attacker-controlled"}
            )


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


def _sign_mercadopago_subscription(data_id: str, ts: str) -> tuple[bytes, dict[str, str]]:
    raw_body = json.dumps(
        {"type": "subscription_authorized_payment", "data": {"id": data_id}}
    ).encode()
    request_id = "req-123"
    manifest = f"id:{data_id.lower()};request-id:{request_id};ts:{ts};"
    signature = hmac.new(WEBHOOK_SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    headers = {"x-signature": f"ts={ts},v1={signature}", "x-request-id": request_id}
    return raw_body, headers


def _sign_stripe_subscription(data_id: str, ts: str) -> tuple[bytes, dict[str, str]]:
    raw_body = json.dumps(
        {
            "id": "evt_123",
            "object": "event",
            "type": "invoice.paid",
            "data": {
                "object": {
                    "id": data_id,
                    "object": "invoice",
                    "parent": {
                        "type": "subscription_details",
                        "subscription_details": {"subscription": "sub_456"},
                    },
                }
            },
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

SUBSCRIPTION_CONFORMANCE_CASES: list[
    tuple[BaseSubscriptionAdapter, Callable[[str, str], tuple]]
] = [
    (
        MercadoPagoSubscriptionAdapter("access-token", webhook_secret=WEBHOOK_SECRET),
        _sign_mercadopago_subscription,
    ),
    (
        StripeSubscriptionAdapter("sk_test", webhook_secret=WEBHOOK_SECRET),
        _sign_stripe_subscription,
    ),
]

ALL_CONFORMANCE_CASES = PAYMENT_CONFORMANCE_CASES + SUBSCRIPTION_CONFORMANCE_CASES
ALL_CONFORMANCE_CASE_IDS = [c[0].provider for c in PAYMENT_CONFORMANCE_CASES] + [
    f"{c[0].provider}-subscription" for c in SUBSCRIPTION_CONFORMANCE_CASES
]


@pytest.mark.parametrize("adapter,signer", ALL_CONFORMANCE_CASES, ids=ALL_CONFORMANCE_CASE_IDS)
def test_valid_signature_is_accepted_and_yields_a_stable_event_id(adapter, signer):
    """Same assertions, every payment *and* subscription adapter: a
    correctly-signed request verifies, and calling `get_event_id` twice on the
    identical bytes is deterministic — the property the idempotency ledger's
    uniqueness constraint depends on."""
    ts = str(int(time.time()))
    raw_body, headers = signer("data-id-123", ts)

    assert adapter.verify_signature(raw_body, headers) is True
    event_id_first = adapter.get_event_id(raw_body, headers, payload={"id": "irrelevant"})
    event_id_second = adapter.get_event_id(raw_body, headers, payload={"id": "different-too"})
    assert event_id_first == event_id_second


@pytest.mark.parametrize("adapter,signer", ALL_CONFORMANCE_CASES, ids=ALL_CONFORMANCE_CASE_IDS)
def test_tampered_body_is_rejected(adapter, signer):
    ts = str(int(time.time()))
    raw_body, headers = signer("data-id-123", ts)
    tampered_body = raw_body.replace(b"data-id-123", b"data-id-999")

    assert adapter.verify_signature(tampered_body, headers) is False


@pytest.mark.parametrize("adapter,signer", ALL_CONFORMANCE_CASES, ids=ALL_CONFORMANCE_CASE_IDS)
def test_stale_signature_is_rejected(adapter, signer):
    stale_ts = str(int(time.time()) - 3600)
    raw_body, headers = signer("data-id-123", stale_ts)

    assert adapter.verify_signature(raw_body, headers) is False


@pytest.mark.parametrize("adapter,signer", ALL_CONFORMANCE_CASES, ids=ALL_CONFORMANCE_CASE_IDS)
def test_missing_secret_fails_closed(adapter, signer):
    """An unconfigured webhook secret must reject every request, never skip
    verification — regardless of which provider or adapter kind."""
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

    def test_adapter_class_lists_match_di_container_registrations(self, di_container):
        """``PAYMENT_ADAPTER_CLASSES``/``SUBSCRIPTION_ADAPTER_CLASSES`` are
        hand-maintained (see this module's docstring for why they can't be
        derived at module scope) — this is what keeps them from silently
        drifting out of sync with the DI wiring: a third adapter registered in
        ``payment_provider_registry``/``subscription_provider_registry``
        without being added to the corresponding list here fails this test
        immediately, rather than just never being exercised by the
        conformance suite above."""
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
