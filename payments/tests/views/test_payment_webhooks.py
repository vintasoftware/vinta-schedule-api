"""Integration tests for the inbound payment-provider webhook endpoints.

These exercise the full request/response path through DRF routing, not just the
service layer: signature verification, idempotency (`ProviderWebhookEvent`), and
the resulting `PaymentStatusUpdate`.
"""

import datetime
import hashlib
import hmac
import json
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.urls import reverse
from django.utils import timezone

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient
from vinta_billing.constants import BillingState, PaymentProviders, PaymentStatuses
from vinta_billing.models import Payment, ProviderWebhookEvent
from vinta_billing.services.payment_adapters.mercadopago_payment_adapter import (
    MercadoPagoPaymentAdapter,
)
from vinta_billing.services.subscription_adapters.mercadopago_subscription_adapter import (
    MercadoPagoSubscriptionAdapter,
)
from vinta_billing.services.subscription_adapters.stripe_subscription_adapter import (
    StripeSubscriptionAdapter,
)
from vinta_billing.services.subscription_service import SubscriptionService

from organizations.models import Organization
from payments.tests.provider_settings import use_providers


WEBHOOK_SECRET = "test-webhook-secret"


def sign(data_id: str, request_id: str = "req-123", ts: str | None = None) -> dict[str, str]:
    """``ts`` defaults to "now" — the signature tolerance window rejects a stale
    ``ts``, so tests that aren't specifically exercising that behavior must sign
    with a fresh timestamp."""
    if ts is None:
        ts = str(int(time.time()))
    manifest = f"id:{data_id.lower()};request-id:{request_id};ts:{ts};"
    signature = hmac.new(WEBHOOK_SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return {
        "HTTP_X_SIGNATURE": f"ts={ts},v1={signature}",
        "HTTP_X_REQUEST_ID": request_id,
    }


def build_signed_request(
    event_id: str = "evt_123",
    event_type: str = "invoice.paid",
    object_payload: dict | None = None,
    secret: str = WEBHOOK_SECRET,
    ts: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    """Build a raw body + headers pair signed the way Stripe signs webhooks.

    Moved here (from the now-deleted
    ``test_stripe_subscription_adapter.py``, whose signature-verification
    coverage moved to the package) since this is its only remaining caller.

    ``object_payload`` defaults to the ``2026-06-24.dahlia``-shaped invoice: the
    subscription id lives at ``parent.subscription_details.subscription``, not
    the removed ``subscription`` field (see
    ``StripeSubscriptionAdapter.get_subscription_external_id_from_update``'s
    docstring). Derived from introspecting the installed `stripe==15.3.1` SDK's
    ``Invoice``/``Invoice.Parent``/``Invoice.Parent.SubscriptionDetails``
    ``__annotations__``.
    """
    if ts is None:
        ts = str(int(time.time()))
    if object_payload is None:
        object_payload = {
            "id": "in_123",
            "object": "invoice",
            "parent": {
                "type": "subscription_details",
                "subscription_details": {"subscription": "sub_123"},
            },
        }
    raw_body = json.dumps(
        {"id": event_id, "object": "event", "type": event_type, "data": {"object": object_payload}}
    ).encode()
    signed_payload = f"{ts}.".encode() + raw_body
    signature = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    headers = {"stripe-signature": f"t={ts},v1={signature}"}
    return raw_body, headers


@pytest.fixture
def mercadopago_payment_adapter():
    with patch(
        "vinta_billing.services.payment_adapters.mercadopago_payment_adapter.mercadopago.SDK"
    ) as mock_sdk:
        adapter = MercadoPagoPaymentAdapter("test-access-token", webhook_secret=WEBHOOK_SECRET)
        adapter.sdk = mock_sdk.return_value
        yield adapter


@pytest.fixture
def mercadopago_subscription_adapter():
    with patch(
        "vinta_billing.services.subscription_adapters.mercadopago_subscription_adapter.mercadopago.SDK"
    ) as mock_sdk:
        adapter = MercadoPagoSubscriptionAdapter("test-access-token", webhook_secret=WEBHOOK_SECRET)
        adapter.sdk = mock_sdk.return_value
        # Default to "no linked payment yet" (real MercadoPago shape: a preapproval
        # payload with no `last_payment_id`) so tests that don't care about the
        # downstream payment write get a clean no-op instead of the SDK mock's
        # auto-generated (and un-persistable) MagicMock attributes flowing into
        # `PaymentModel.objects.create(...)`.
        adapter.sdk.preapproval().get.return_value = {"response": {}}
        yield adapter


@pytest.fixture
def webhook_client(di_container, mercadopago_payment_adapter, mercadopago_subscription_adapter):
    """An unauthenticated client wired to signature-verifiable, SDK-mocked adapters.

    Overriding `payment_gateway` / `subscription_gateway` also changes what
    `payment_provider_registry` / `subscription_provider_registry` resolve to for
    ``mercadopago`` — both `Dict` providers reference the gateway providers by
    reference, so an override on the gateway provider propagates through them.
    """
    with (
        di_container.payment_gateway.override(mercadopago_payment_adapter),
        di_container.subscription_gateway.override(mercadopago_subscription_adapter),
    ):
        yield APIClient()


@pytest.fixture
def organization():
    return baker.make(Organization)


@pytest.fixture
def billing_address():
    return baker.make(
        "vinta_billing.BillingAddress",
        street_name="Test Street",
        street_number="123",
        city="Test City",
        state="Test State",
        country="Test Country",
        zip_code="12345",
    )


@pytest.fixture
def billing_profile(organization, billing_address):
    return baker.make(
        "vinta_billing.BillingProfile",
        organization=organization,
        document_type="CPF",
        document_number="12345678900",
        billing_address=billing_address,
    )


@pytest.fixture
def payment(billing_profile):
    return baker.make(
        "vinta_billing.Payment",
        billing_profile=billing_profile,
        value=Decimal("100"),
        currency="USD",
        payment_provider=PaymentProviders.MERCADOPAGO,
        status=PaymentStatuses.PENDING,
        payment_method="credit_card",
        external_id="mp-payment-456",
    )


def payment_update_url(pk: int | str = 1, provider: str = PaymentProviders.MERCADOPAGO) -> str:
    return reverse("Payments-payment-update", kwargs={"pk": pk, "provider": provider})


def subscription_payment_update_url(
    pk: int | str = 1, provider: str = PaymentProviders.MERCADOPAGO
) -> str:
    return reverse("Payments-subscription-payment-update", kwargs={"pk": pk, "provider": provider})


@pytest.mark.django_db
class TestPaymentUpdateWebhook:
    def _payload(self, notification_id: str = "notif-1", data_id: str = "mp-payment-456") -> bytes:
        return json.dumps(
            {
                "type": "payment",
                "action": "payment.update",
                "id": notification_id,
                "data": {"id": data_id},
            }
        ).encode()

    def test_unsigned_post_is_rejected(self, webhook_client, payment):
        response = webhook_client.post(
            payment_update_url(),
            data=self._payload(),
            content_type="application/json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert ProviderWebhookEvent.objects.count() == 0

    def test_valid_signature_processes_the_event(
        self, webhook_client, mercadopago_payment_adapter, payment
    ):
        mercadopago_payment_adapter.sdk.payment().get.return_value = {
            "response": {
                "id": "mp-payment-456",
                "status": "approved",
                "status_detail": "accredited",
            }
        }
        ts = str(int(time.time()))

        response = webhook_client.post(
            payment_update_url(),
            data=self._payload(),
            content_type="application/json",
            **sign("mp-payment-456", ts=ts),
        )

        assert response.status_code == status.HTTP_200_OK
        assert ProviderWebhookEvent.objects.count() == 1
        event = ProviderWebhookEvent.objects.get()
        assert event.provider == PaymentProviders.MERCADOPAGO
        # The ledger key is derived entirely from signed material (`data.id` +
        # `x-request-id` + `ts`) — never the payload's unsigned top-level "id"
        # ("notif-1" here), which an attacker can vary freely across replays of one
        # captured valid signature.
        assert event.external_event_id == f"mp-payment-456:req-123:{ts}"
        assert event.processed_at is not None

        payment.refresh_from_db()
        assert payment.status_updates.count() == 1
        assert payment.status_updates.get().status == PaymentStatuses.APPROVED

    def test_processes_with_no_public_key_configured(
        self, settings, webhook_client, mercadopago_payment_adapter, payment
    ):
        """Regression: the inbound webhook path must not be gated on any
        *outbound* or browser-facing credential.

        A delivery is a notification the provider pushed at us, authenticated by
        the webhook secret. Resolving its adapter through a check on
        ``MERCADOPAGO_PUBLIC_KEY`` (which ships empty in both env examples and is
        ``sync: false`` on Render) raised ``PaymentProviderNotConfiguredError``
        from ``verify_payment_webhook_signature`` -- an *uncaught* 500, since
        ``PaymentsViewSet.payment_update`` catches only
        ``UnknownPaymentProviderError``. MercadoPago would then retry forever
        while every payment confirmation, ``record_payment_method`` write, add-on
        activation, and dunning resolution silently stopped. See
        ``PaymentService.get_payment_adapter`` vs
        ``get_configured_payment_adapter``.
        """
        use_providers(settings, MERCADOPAGO_PUBLIC_KEY="")
        mercadopago_payment_adapter.sdk.payment().get.return_value = {
            "response": {
                "id": "mp-payment-456",
                "status": "approved",
                "status_detail": "accredited",
            }
        }

        response = webhook_client.post(
            payment_update_url(),
            data=self._payload(),
            content_type="application/json",
            **sign("mp-payment-456"),
        )

        assert response.status_code == status.HTTP_200_OK
        assert ProviderWebhookEvent.objects.count() == 1
        assert ProviderWebhookEvent.objects.get().processed_at is not None
        payment.refresh_from_db()
        assert payment.status_updates.get().status == PaymentStatuses.APPROVED

    def test_webhook_resolves_off_the_payment_row_not_the_organizations_current_pin(
        self, webhook_client, mercadopago_payment_adapter, payment, billing_profile
    ):
        """Rule A: a webhook delivery for a
        payment made at MercadoPago must be processed through MercadoPago even
        when the organization's *current* pin has since moved to Stripe. The
        webhook route already resolves its adapter off the `provider` URL kwarg
        (matching MercadoPago's own `notification_url`, not any pin), and
        `PaymentService.handle_payment_webhook` -> `receive_payment_update` looks
        the `Payment` row up by its own `external_id` -- neither step ever reads
        `billing_profile.payment_provider`. This proves the whole path end to
        end against a deliberately mismatched pin."""
        billing_profile.payment_provider = PaymentProviders.STRIPE
        billing_profile.save(update_fields=["payment_provider"])
        assert payment.payment_provider == PaymentProviders.MERCADOPAGO

        mercadopago_payment_adapter.sdk.payment().get.return_value = {
            "response": {
                "id": "mp-payment-456",
                "status": "approved",
                "status_detail": "accredited",
            }
        }
        ts = str(int(time.time()))

        response = webhook_client.post(
            payment_update_url(provider=PaymentProviders.MERCADOPAGO),
            data=self._payload(),
            content_type="application/json",
            **sign("mp-payment-456", ts=ts),
        )

        assert response.status_code == status.HTTP_200_OK
        payment.refresh_from_db()
        assert payment.status_updates.count() == 1
        assert payment.status_updates.get().status == PaymentStatuses.APPROVED
        # The payment row's own provider stays MercadoPago -- the webhook never
        # repoints it to the org's current (Stripe) pin.
        assert payment.payment_provider == PaymentProviders.MERCADOPAGO

    def test_duplicate_delivery_is_idempotent(
        self, webhook_client, mercadopago_payment_adapter, payment
    ):
        mercadopago_payment_adapter.sdk.payment().get.return_value = {
            "response": {"status": "approved", "status_detail": "accredited"}
        }
        payload = self._payload()
        headers = sign("mp-payment-456")

        first = webhook_client.post(
            payment_update_url(), data=payload, content_type="application/json", **headers
        )
        second = webhook_client.post(
            payment_update_url(), data=payload, content_type="application/json", **headers
        )

        assert first.status_code == status.HTTP_200_OK
        assert second.status_code == status.HTTP_200_OK
        assert ProviderWebhookEvent.objects.count() == 1
        payment.refresh_from_db()
        assert payment.status_updates.count() == 1

    def test_tampered_body_with_stale_signature_is_rejected(
        self, webhook_client, mercadopago_payment_adapter, payment
    ):
        """The signature is computed over the real bytes; swapping `data.id` after
        signing must be caught even though the tampered body still parses cleanly."""
        headers = sign("mp-payment-456")
        tampered_payload = self._payload(data_id="mp-payment-999")

        response = webhook_client.post(
            payment_update_url(),
            data=tampered_payload,
            content_type="application/json",
            **headers,
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert ProviderWebhookEvent.objects.count() == 0

    def test_unknown_provider_returns_404(self, webhook_client, payment):
        response = webhook_client.post(
            payment_update_url(provider="unknown-provider"),
            data=self._payload(),
            content_type="application/json",
            **sign("mp-payment-456"),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert ProviderWebhookEvent.objects.count() == 0

    def test_missing_top_level_notification_id_still_processes(
        self, webhook_client, mercadopago_payment_adapter, payment
    ):
        """The idempotency ledger key no longer depends on the payload's top-level
        "id" at all — a notification missing it entirely must still be accepted and
        processed, as long as `data.id` (the field the signature actually covers)
        is present."""
        mercadopago_payment_adapter.sdk.payment().get.return_value = {
            "response": {
                "id": "mp-payment-456",
                "status": "approved",
                "status_detail": "accredited",
            }
        }
        payload = json.dumps(
            {"type": "payment", "action": "payment.update", "data": {"id": "mp-payment-456"}}
        ).encode()

        response = webhook_client.post(
            payment_update_url(),
            data=payload,
            content_type="application/json",
            **sign("mp-payment-456"),
        )

        assert response.status_code == status.HTTP_200_OK
        assert ProviderWebhookEvent.objects.count() == 1
        assert ProviderWebhookEvent.objects.get().processed_at is not None

    def test_replayed_signature_with_mutated_notification_id_is_rejected(
        self, webhook_client, mercadopago_payment_adapter, payment
    ):
        """Regression test for the top-level-`id`-as-ledger-key vulnerability: an
        attacker who captures one valid `(x-signature, x-request-id)` pair can keep
        `data.id` fixed (so the HMAC still verifies) and vary the payload's
        unsigned top-level `id` on every replay. If the ledger key were still
        derived from that field, each replay would look like a distinct "new"
        event and the handler would re-run unbounded. With the key derived only
        from signed material, every replay collapses onto the same ledger row."""
        mercadopago_payment_adapter.sdk.payment().get.return_value = {
            "response": {
                "id": "mp-payment-456",
                "status": "approved",
                "status_detail": "accredited",
            }
        }
        headers = sign("mp-payment-456")

        first = webhook_client.post(
            payment_update_url(),
            data=self._payload(notification_id="notif-1"),
            content_type="application/json",
            **headers,
        )
        second = webhook_client.post(
            payment_update_url(),
            data=self._payload(notification_id="notif-2-mutated-by-attacker"),
            content_type="application/json",
            **headers,
        )

        assert first.status_code == status.HTTP_200_OK
        assert second.status_code == status.HTTP_200_OK
        assert ProviderWebhookEvent.objects.count() == 1
        payment.refresh_from_db()
        assert payment.status_updates.count() == 1


@pytest.mark.django_db
class TestSubscriptionPaymentUpdateWebhook:
    def _payload(self, notification_id: str = "notif-1", data_id: str = "sub-123") -> bytes:
        return json.dumps(
            {
                "type": "subscription_authorized_payment",
                "id": notification_id,
                "data": {"id": data_id},
            }
        ).encode()

    def test_unsigned_post_is_rejected(self, webhook_client):
        response = webhook_client.post(
            subscription_payment_update_url(),
            data=self._payload(),
            content_type="application/json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert ProviderWebhookEvent.objects.count() == 0

    def test_valid_signature_with_no_linked_payment_is_a_no_op(self, webhook_client):
        """The preapproval payload has no linked payment yet — the handler no-ops,
        but the delivery must still be authenticated and recorded. It is not
        marked `processed_at` — `receive_payment_update` returning `None` must
        not permanently burn the ledger row (see
        `PaymentService.handle_subscription_payment_webhook`'s docstring): a
        provider redelivery of the same event is safe to retry rather than
        being silently dropped forever."""
        response = webhook_client.post(
            subscription_payment_update_url(),
            data=self._payload(),
            content_type="application/json",
            **sign("sub-123"),
        )

        assert response.status_code == status.HTTP_200_OK
        assert ProviderWebhookEvent.objects.count() == 1
        assert ProviderWebhookEvent.objects.get().processed_at is None

    def test_duplicate_delivery_is_idempotent(self, webhook_client):
        payload = self._payload()
        headers = sign("sub-123")

        first = webhook_client.post(
            subscription_payment_update_url(),
            data=payload,
            content_type="application/json",
            **headers,
        )
        second = webhook_client.post(
            subscription_payment_update_url(),
            data=payload,
            content_type="application/json",
            **headers,
        )

        assert first.status_code == status.HTTP_200_OK
        assert second.status_code == status.HTTP_200_OK
        assert ProviderWebhookEvent.objects.count() == 1

    def test_replayed_signature_with_mutated_notification_id_is_rejected(self, webhook_client):
        """Same regression as the payment-update endpoint's equivalent test: the
        ledger key must be derived from signed material only, so replaying one
        valid signature with a mutated (unsigned) top-level notification id must
        still collapse onto a single `ProviderWebhookEvent` row."""
        headers = sign("sub-123")

        first = webhook_client.post(
            subscription_payment_update_url(),
            data=self._payload(notification_id="notif-1"),
            content_type="application/json",
            **headers,
        )
        second = webhook_client.post(
            subscription_payment_update_url(),
            data=self._payload(notification_id="notif-2-mutated-by-attacker"),
            content_type="application/json",
            **headers,
        )

        assert first.status_code == status.HTTP_200_OK
        assert second.status_code == status.HTTP_200_OK
        assert ProviderWebhookEvent.objects.count() == 1

    def test_tampered_body_with_stale_signature_is_rejected(self, webhook_client):
        headers = sign("sub-123")
        tampered_payload = self._payload(data_id="sub-999")

        response = webhook_client.post(
            subscription_payment_update_url(),
            data=tampered_payload,
            content_type="application/json",
            **headers,
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert ProviderWebhookEvent.objects.count() == 0

    def _preapproval_response(self, payment_id: str = "mp-sub-payment-1") -> dict:
        return {"response": {"last_payment_id": payment_id}}

    def _payment_response(self, payment_id: str = "mp-sub-payment-1") -> dict:
        return {
            "response": {
                "id": payment_id,
                "status": "approved",
                "status_detail": "accredited",
                "transaction_amount": "50.00",
                "currency_id": "BRL",
                "payment_method_id": "visa",
                "description": "Subscription charge",
                "payer": {
                    "email": "billing@example.com",
                    "first_name": "Test",
                    "last_name": "Payer",
                    "identification": {"type": "CPF", "number": "12345678900"},
                    "address": {
                        "street_name": "Test Street",
                        "street_number": "123",
                        "neighborhood": "Centro",
                        "city": "Test City",
                        "federal_unit": "TS",
                        "country": "BR",
                        "zip_code": "12345",
                    },
                },
            }
        }

    @pytest.mark.no_auto_subscription
    def test_subscription_charge_payment_row_carries_a_provider(
        self, webhook_client, mercadopago_subscription_adapter, billing_profile
    ):
        """Every recurring subscription
        charge creates a ``Payment`` row here, and that row must carry a
        provider. Stamped `""` (as it was before this fix) it is unroutable for
        the rest of its life -- ``check_payment_status``/``create_refund``
        resolve their adapter from this column under Rule A and `""` raises
        ``UnknownPaymentProviderError``.

        Opts out of conftest's autouse ``provision_default_subscription``: this
        test builds its own ``Subscription`` (``OneToOne`` with ``Organization``)
        via ``create_subscription_for_organization`` below."""
        subscription = SubscriptionService().create_subscription_for_organization(
            billing_profile.organization
        )
        assert subscription is not None
        subscription.external_id = "sub-123"
        subscription.payment_provider = PaymentProviders.MERCADOPAGO
        subscription.save(update_fields=["external_id", "payment_provider"])
        mercadopago_subscription_adapter.sdk.preapproval().get.return_value = (
            self._preapproval_response()
        )
        mercadopago_subscription_adapter.sdk.payment().get.return_value = self._payment_response()

        response = webhook_client.post(
            subscription_payment_update_url(),
            data=self._payload(),
            content_type="application/json",
            **sign("sub-123"),
        )

        assert response.status_code == status.HTTP_200_OK
        payment = Payment.objects.get(external_id="mp-sub-payment-1")
        assert payment.subscription_id == subscription.pk
        assert payment.payment_provider == PaymentProviders.MERCADOPAGO

    @pytest.mark.no_auto_subscription
    def test_approved_charge_pins_the_subscriptions_own_provider_not_a_hardcoded_one(
        self, webhook_client, mercadopago_subscription_adapter, billing_profile
    ):
        """The BLOCKER-4 regression: the pin
        written on an organization's first confirmed subscription charge comes
        from ``Subscription.payment_provider``, which
        ``create_subscription_for_organization`` now resolves from the
        organization (Rule B). While that column was hardcoded ``mercadopago``,
        this write permanently stamped ``mercadopago`` onto every previously
        unpinned organization -- making ``DEFAULT_PAYMENT_PROVIDER=stripe``
        unreachable for anyone who ever paid.

        Driven here through a MercadoPago delivery against a subscription the
        service resolved onto MercadoPago, with the assertion stated as the
        subscription's provider being what lands in the pin.

        Opts out of conftest's autouse ``provision_default_subscription``: this
        test builds its own ``Subscription`` via
        ``create_subscription_for_organization`` below.
        """
        assert billing_profile.payment_provider == ""
        billing_profile.organization.refresh_from_db()
        subscription = SubscriptionService().create_subscription_for_organization(
            billing_profile.organization
        )
        assert subscription is not None
        subscription.payment_provider = PaymentProviders.MERCADOPAGO
        subscription.external_id = "sub-123"
        subscription.save(update_fields=["payment_provider", "external_id"])
        mercadopago_subscription_adapter.sdk.preapproval().get.return_value = (
            self._preapproval_response()
        )
        mercadopago_subscription_adapter.sdk.payment().get.return_value = self._payment_response()

        response = webhook_client.post(
            subscription_payment_update_url(),
            data=self._payload(),
            content_type="application/json",
            **sign("sub-123"),
        )

        assert response.status_code == status.HTTP_200_OK
        billing_profile.refresh_from_db()
        assert billing_profile.payment_provider == subscription.payment_provider
        assert billing_profile.payment_provider == PaymentProviders.MERCADOPAGO

    @pytest.mark.no_auto_subscription
    def test_stripe_default_org_is_not_pinned_to_mercadopago_by_its_first_charge(
        self, settings, di_container, billing_profile
    ):
        """The other half of the same regression, and the one that actually
        broke: an unpinned organization under ``DEFAULT_PAYMENT_PROVIDER=stripe``
        gets a ``stripe``-resolved subscription, so its first confirmed charge
        pins it to ``stripe`` -- never to ``mercadopago``.

        Opts out of conftest's autouse ``provision_default_subscription``: this
        test builds its own ``Subscription`` via
        ``create_subscription_for_organization`` below."""
        use_providers(settings, default_provider=PaymentProviders.STRIPE)
        assert billing_profile.payment_provider == ""
        subscription = SubscriptionService().create_subscription_for_organization(
            billing_profile.organization
        )
        assert subscription is not None
        assert subscription.payment_provider == PaymentProviders.STRIPE

        SubscriptionService().record_payment_method(
            billing_profile.organization, subscription.payment_provider, "stripe-sub-ext-1"
        )

        billing_profile.refresh_from_db()
        assert billing_profile.payment_provider == PaymentProviders.STRIPE


@pytest.mark.django_db
class TestZeroAmountSubscriptionPaymentDoesNotResolveDunning:
    """The zero-amount guard on subscription payments.

    Driven through the real inbound webhook path -- signature verification,
    ``ProviderWebhookEvent`` idempotency, ``PaymentsViewSet
    ._apply_subscription_payment_side_effects`` -- rather than by calling
    ``DunningService.resolve_payment_success`` (or the guard) directly. Calling
    the guard directly would only prove the guard's own logic is correct, not
    that it actually sits on the path a real webhook delivery takes; the
    defect this guard closes (a $0.00 proration invoice's `invoice.paid`
    reaching `resolve_payment_success` on Stripe -- see
    ``SubscriptionService.retry_payment``'s docstring for the probe numbers)
    is specifically about what happens *at that path*.

    Uses the MercadoPago webhook fixtures already wired in this module --
    the guard is provider-agnostic (it reads ``PaymentStatuses.APPROVED`` +
    ``payment.value``, never anything provider-specific), so exercising it
    through MercadoPago's webhook shape is sufficient; nothing here depends on
    Stripe's payload format.
    """

    def _payload(self, notification_id: str = "notif-1", data_id: str = "sub-123") -> bytes:
        return json.dumps(
            {
                "type": "subscription_authorized_payment",
                "id": notification_id,
                "data": {"id": data_id},
            }
        ).encode()

    def _preapproval_response(self, payment_id: str = "mp-sub-payment-1") -> dict:
        return {"response": {"last_payment_id": payment_id}}

    def _payment_response(self, transaction_amount: str, payment_id: str = "mp-sub-payment-1"):
        return {
            "response": {
                "id": payment_id,
                "status": "approved",
                "status_detail": "accredited",
                "transaction_amount": transaction_amount,
                "currency_id": "BRL",
                "payment_method_id": "visa",
                "description": "Subscription charge",
                "payer": {
                    "email": "billing@example.com",
                    "first_name": "Test",
                    "last_name": "Payer",
                    "identification": {"type": "CPF", "number": "12345678900"},
                    "address": {
                        "street_name": "Test Street",
                        "street_number": "123",
                        "neighborhood": "Centro",
                        "city": "Test City",
                        "federal_unit": "TS",
                        "country": "BR",
                        "zip_code": "12345",
                    },
                },
            }
        }

    def _grace_subscription(self, billing_profile):
        subscription = SubscriptionService().create_subscription_for_organization(
            billing_profile.organization
        )
        assert subscription is not None
        subscription.payment_provider = PaymentProviders.MERCADOPAGO
        subscription.external_id = "sub-123"
        subscription.billing_state = BillingState.GRACE
        subscription.grace_period_ends_at = timezone.now() + datetime.timedelta(days=5)
        subscription.save(
            update_fields=[
                "payment_provider",
                "external_id",
                "billing_state",
                "grace_period_ends_at",
            ]
        )
        return subscription

    @pytest.mark.no_auto_subscription
    def test_zero_amount_approved_payment_leaves_grace_subscription_in_grace(
        self, webhook_client, mercadopago_subscription_adapter, billing_profile
    ):
        """The regression this guard closes: a $0 approved subscription
        payment (e.g. an offsetting-proration invoice) must never flip a
        GRACE subscription to ACTIVE -- that would be a false recovery, the
        payer marked healthy with the real balance still uncollected.

        Also pins reviewer finding SHOULD-FIX 8: `record_payment_method` must
        not fire either -- a $0 approved payment is not proof the instrument
        is actually chargeable, and that call both grants `has_payment_method`
        (which gates overage accrual) and permanently pins
        `BillingProfile.payment_provider`.
        """
        subscription = self._grace_subscription(billing_profile)
        mercadopago_subscription_adapter.sdk.preapproval().get.return_value = (
            self._preapproval_response()
        )
        mercadopago_subscription_adapter.sdk.payment().get.return_value = self._payment_response(
            "0.00"
        )
        assert billing_profile.payment_provider == ""

        response = webhook_client.post(
            subscription_payment_update_url(),
            data=self._payload(),
            content_type="application/json",
            **sign("sub-123"),
        )

        assert response.status_code == status.HTTP_200_OK
        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.GRACE
        assert subscription.grace_period_ends_at is not None
        billing_profile.refresh_from_db()
        assert billing_profile.payment_provider == ""

    @pytest.mark.no_auto_subscription
    def test_nonzero_amount_approved_payment_resolves_grace_to_active(
        self, webhook_client, mercadopago_subscription_adapter, billing_profile
    ):
        """Control for the test above: the guard must not simply block every
        recovery -- a genuine, non-zero approved payment still resolves GRACE
        to ACTIVE through this exact same webhook path."""
        subscription = self._grace_subscription(billing_profile)
        mercadopago_subscription_adapter.sdk.preapproval().get.return_value = (
            self._preapproval_response()
        )
        mercadopago_subscription_adapter.sdk.payment().get.return_value = self._payment_response(
            "50.00"
        )

        response = webhook_client.post(
            subscription_payment_update_url(),
            data=self._payload(),
            content_type="application/json",
            **sign("sub-123"),
        )

        assert response.status_code == status.HTTP_200_OK
        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.ACTIVE
        assert subscription.grace_period_ends_at is None


# The Stripe `Invoice.billing`-vs-`Invoice.payments` defect this class's
# lone `xfail(strict=True)` used to guard against is fixed in
# `vinta-django-billing` 0.5.0 -- see that package's own
# `tests/services/subscription_adapters/test_stripe_subscription_adapter.py`
# for the full story and the same marker removal.
@pytest.mark.django_db
class TestStripeInvoicePaidResolvesOffTheEventsOwnInvoice:
    """Reviewer finding BLOCKER 1.

    Before the fix, `receive_payment_update` resolved the payment off
    `Subscription.latest_invoice` for *every* `invoice.*` event, regardless of
    which invoice the event was actually about. The dunning ladder mints a
    fresh $0 proration invoice on every grace tick, so by the time a payer
    recovers through `retry-payment`, `latest_invoice` is that $0 invoice --
    which has no PaymentIntent, so this webhook silently no-oped even though
    `Invoice.pay` had genuinely collected the real balance on the invoice the
    event actually named. GRACE never resolved to ACTIVE: $49 moved, no
    `Payment` row, no `PaymentStatusUpdate`, the subscription rode GRACE
    straight to RESTRICTED.

    Driven through the real HTTP webhook path -- signature verification,
    `ProviderWebhookEvent` idempotency, and
    `PaymentsViewSet._apply_subscription_payment_side_effects` -- so the
    assertion is "the subscription actually reaches ACTIVE", not merely that
    the adapter method returns the right tuple in isolation (that unit-level
    proof now lives in the package's own
    `tests/services/subscription_adapters/test_stripe_subscription_adapter.py`).
    `stripe.Invoice`/
    `stripe.PaymentIntent` are mocked at the adapter module's own boundary,
    same pattern as `test_billing_views.py`'s Stripe override; `stripe
    .Subscription` is deliberately left unmocked -- the fixed code must never
    call `Subscription.retrieve` for an `invoice.*` event at all.
    """

    STRIPE_WEBHOOK_SECRET = "whsec_test_secret"

    def _grace_subscription(self, billing_profile, external_id: str = "sub_stripe_1"):
        subscription = SubscriptionService().create_subscription_for_organization(
            billing_profile.organization
        )
        assert subscription is not None
        subscription.payment_provider = PaymentProviders.STRIPE
        subscription.external_id = external_id
        subscription.billing_state = BillingState.GRACE
        subscription.grace_period_ends_at = timezone.now() + datetime.timedelta(days=5)
        subscription.save(
            update_fields=[
                "payment_provider",
                "external_id",
                "billing_state",
                "grace_period_ends_at",
            ]
        )
        return subscription

    def _invoice_paid_event(self, invoice_id: str, subscription_external_id: str):
        object_payload = {
            "id": invoice_id,
            "object": "invoice",
            "parent": {
                "type": "subscription_details",
                "subscription_details": {"subscription": subscription_external_id},
            },
        }
        return build_signed_request(
            event_id="evt_invoice_paid_1",
            event_type="invoice.paid",
            object_payload=object_payload,
            secret=self.STRIPE_WEBHOOK_SECRET,
        )

    @pytest.mark.no_auto_subscription
    def test_invoice_paid_for_a_non_latest_invoice_resolves_grace_to_active(
        self, di_container, billing_profile
    ):
        subscription = self._grace_subscription(billing_profile)
        stripe_adapter = StripeSubscriptionAdapter(
            api_key="sk_test_123", webhook_secret=self.STRIPE_WEBHOOK_SECRET
        )
        raw_body, headers = self._invoice_paid_event("in_past_due_49", "sub_stripe_1")

        with (
            patch(
                "vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Invoice"
            ) as mock_invoice,
            patch(
                "vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe"
                ".PaymentIntent"
            ) as mock_payment_intent,
            di_container.stripe_subscription_gateway.override(stripe_adapter),
        ):
            # The invoice the event fired for carries both the dead card's
            # failed attempt (status "open") and the new card's successful
            # one (status "paid") -- never `latest_invoice`, the unrelated $0
            # proration invoice the last dunning tick minted, which the fixed
            # code never even retrieves for an `invoice.*` event.
            mock_invoice.retrieve.return_value = MagicMock(
                to_dict=lambda: {
                    "id": "in_past_due_49",
                    "payments": {
                        "object": "list",
                        "data": [
                            {
                                "id": "inpay_failed",
                                "status": "open",
                                "created": 1000,
                                "payment": {
                                    "type": "payment_intent",
                                    "payment_intent": "pi_dead_card_failed",
                                },
                            },
                            {
                                "id": "inpay_success",
                                "status": "paid",
                                "created": 2000,
                                "payment": {
                                    "type": "payment_intent",
                                    "payment_intent": "pi_new_card_success",
                                },
                            },
                        ],
                    },
                }
            )
            mock_payment_intent.retrieve.return_value = MagicMock(
                to_dict=lambda: {
                    "id": "pi_new_card_success",
                    "amount": 4900,
                    "currency": "usd",
                    "status": "succeeded",
                    "payment_method_types": ["card"],
                    "description": "Past-due renewal",
                }
            )

            response = APIClient().post(
                subscription_payment_update_url(
                    pk="sub_stripe_1", provider=PaymentProviders.STRIPE
                ),
                data=raw_body,
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE=headers["stripe-signature"],
            )

        assert response.status_code == status.HTTP_200_OK
        mock_invoice.retrieve.assert_called_once_with(
            "in_past_due_49", expand=["payments"], api_key="sk_test_123"
        )
        mock_payment_intent.retrieve.assert_called_once_with(
            "pi_new_card_success", api_key="sk_test_123"
        )
        payment = Payment.objects.get(external_id="pi_new_card_success")
        assert payment.subscription_id == subscription.pk
        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.ACTIVE
        assert subscription.grace_period_ends_at is None
