import hashlib
import hmac
import json
import time
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from payments.constants import PaymentProviders, PaymentStatuses, RefundStatuses
from payments.exceptions import ProviderWebhookEventIdMissingError
from payments.services.dataclasses import Refund, RefundResult
from payments.services.payment_adapters.base import Payment, PaymentStatusUpdate
from payments.services.payment_adapters.stripe_payment_adapter import (
    PAYMENT_INTENT_STATUS_MAPPING,
    REFUND_STATUS_MAPPING,
    StripePaymentAdapter,
    to_stripe_amount,
)


WEBHOOK_SECRET = "whsec_test_secret"


def build_signed_request(
    event_id: str = "evt_123",
    event_type: str = "payment_intent.succeeded",
    object_id: str = "pi_123",
    object_type: str = "payment_intent",
    secret: str = WEBHOOK_SECRET,
    ts: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    """Build a raw body + headers pair signed the way Stripe signs webhooks."""
    if ts is None:
        ts = str(int(time.time()))
    raw_body = json.dumps(
        {
            "id": event_id,
            "object": "event",
            "type": event_type,
            "data": {"object": {"id": object_id, "object": object_type}},
        }
    ).encode()
    signed_payload = f"{ts}.".encode() + raw_body
    signature = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    headers = {"stripe-signature": f"t={ts},v1={signature}"}
    return raw_body, headers


@pytest.fixture
def mock_billing_profile():
    profile = Mock()
    profile.email = "test@example.com"
    return profile


@pytest.fixture
def mock_payment(mock_billing_profile):
    payment = Mock(spec=Payment)
    payment.id = "payment-123"
    payment.value = Decimal("100.50")
    payment.currency = "USD"
    payment.description = "Test payment"
    payment.billing_profile = mock_billing_profile
    payment.external_id = "pi_456"
    return payment


@pytest.fixture
def mock_refund(mock_payment):
    refund = Mock(spec=Refund)
    refund.id = "refund-123"
    refund.value = Decimal("50.25")
    refund.currency = "USD"
    refund.payment = mock_payment
    refund.external_id = None
    return refund


@pytest.fixture
def adapter():
    return StripePaymentAdapter("sk_test_123", webhook_secret=WEBHOOK_SECRET)


def test_provider_and_verifies_full_body():
    adapter = StripePaymentAdapter("sk_test_123")
    assert adapter.provider == PaymentProviders.STRIPE
    # Unlike MercadoPago, Stripe's `Stripe-Signature` covers the entire body.
    assert adapter.verifies_full_body is True


def test_to_stripe_amount_regular_currency():
    assert to_stripe_amount(Decimal("100.50"), "USD") == 10050


def test_to_stripe_amount_zero_decimal_currency():
    assert to_stripe_amount(Decimal("1500"), "JPY") == 1500


@patch("payments.services.payment_adapters.stripe_payment_adapter.stripe.PaymentIntent")
def test_process_success(mock_payment_intent, adapter, mock_payment):
    mock_payment_intent.create.return_value = Mock(id="pi_created_123")

    result = adapter.process(mock_payment, "pm_test_token")

    assert result == "pi_created_123"
    mock_payment_intent.create.assert_called_once()
    call_kwargs = mock_payment_intent.create.call_args.kwargs
    assert call_kwargs["amount"] == 10050
    assert call_kwargs["currency"] == "usd"
    assert call_kwargs["payment_method"] == "pm_test_token"
    assert call_kwargs["confirm"] is True
    assert call_kwargs["api_key"] == "sk_test_123"
    # No idempotency key supplied -> none forwarded to Stripe.
    assert "idempotency_key" not in call_kwargs


@patch("payments.services.payment_adapters.stripe_payment_adapter.stripe.PaymentIntent")
def test_process_forwards_idempotency_key_to_stripe(mock_payment_intent, adapter, mock_payment):
    """The client-supplied key must reach Stripe as its `Idempotency-Key` so a
    retried charge (e.g. after a rolled-back local transaction) resolves to the
    same PaymentIntent instead of charging twice. No live Stripe here -- assert
    the key is passed into the SDK call."""
    mock_payment_intent.create.return_value = Mock(id="pi_created_123")

    adapter.process(mock_payment, "pm_test_token", idempotency_key="idem-key-1")

    call_kwargs = mock_payment_intent.create.call_args.kwargs
    assert call_kwargs["idempotency_key"] == "idem-key-1"


@patch("payments.services.payment_adapters.stripe_payment_adapter.stripe.PaymentIntent")
def test_check_status_maps_known_status(mock_payment_intent, adapter):
    """`intent` is a bare `Mock()` (no `spec`) — setting `.get` does nothing to
    `getattr(intent, "last_payment_error", None)`, the attribute the adapter
    actually reads. Without `last_payment_error` explicitly set, an unspecced
    `Mock`'s attribute access returns a truthy child `Mock`, silently taking the
    "has an error" branch and never asserting `description`."""
    intent = Mock()
    intent.status = "succeeded"
    intent.id = "pi_456"
    intent.last_payment_error = None
    mock_payment_intent.retrieve.return_value = intent

    result = adapter.check_status("pi_456")

    assert isinstance(result, PaymentStatusUpdate)
    assert result.status == PaymentStatuses.APPROVED
    assert result.update_external_id == "pi_456"
    assert result.description == "succeeded"
    mock_payment_intent.retrieve.assert_called_once_with("pi_456", api_key="sk_test_123")


@patch("payments.services.payment_adapters.stripe_payment_adapter.logger")
@patch("payments.services.payment_adapters.stripe_payment_adapter.stripe.PaymentIntent")
def test_check_status_maps_unknown_status_and_logs(mock_payment_intent, mock_logger, adapter):
    intent = Mock()
    intent.status = "some_new_stripe_status"
    intent.id = "pi_456"
    intent.last_payment_error = None
    mock_payment_intent.retrieve.return_value = intent

    result = adapter.check_status("pi_456")

    assert result.status == PaymentStatuses.UNKNOWN
    mock_logger.error.assert_called_once_with(
        "Unknown payment status: payment_external_id=%s original_status=%s",
        "pi_456",
        "some_new_stripe_status",
    )


@patch("payments.services.payment_adapters.stripe_payment_adapter.stripe.PaymentIntent")
def test_check_status_uses_last_payment_error_message_as_description(mock_payment_intent, adapter):
    """When `last_payment_error` is present, its `message` — not the raw
    status — becomes `description`."""
    intent = Mock()
    intent.status = "requires_payment_method"
    intent.id = "pi_456"
    intent.last_payment_error = {"message": "card_declined"}
    mock_payment_intent.retrieve.return_value = intent

    result = adapter.check_status("pi_456")

    assert result.status == PaymentStatuses.PENDING
    assert result.description == "card_declined"


@patch("payments.services.payment_adapters.stripe_payment_adapter.stripe.Refund")
def test_refund_success(mock_refund_resource, adapter, mock_refund):
    """Stripe's create-refund response already carries the refund's status
    synchronously — no forced second round trip through `check_refund_status`."""
    mock_refund_resource.create.return_value = Mock(id="re_456", status="succeeded")

    result = adapter.refund(mock_refund)

    assert result == RefundResult(external_id="re_456", status=RefundStatuses.APPROVED)
    mock_refund_resource.create.assert_called_once_with(
        payment_intent="pi_456", amount=5025, api_key="sk_test_123"
    )


@patch("payments.services.payment_adapters.stripe_payment_adapter.stripe.Refund")
def test_check_refund_status_success(mock_refund_resource, adapter, mock_refund):
    mock_refund.external_id = "re_456"
    mock_refund_resource.retrieve.return_value = Mock(id="re_456", status="succeeded")

    result = adapter.check_refund_status(mock_refund)

    assert result == RefundStatuses.APPROVED
    mock_refund_resource.retrieve.assert_called_once_with("re_456", api_key="sk_test_123")


def test_check_refund_status_without_external_id(adapter, mock_refund):
    mock_refund.external_id = None

    result = adapter.check_refund_status(mock_refund)

    assert result == RefundStatuses.UNKNOWN


def test_get_payment_external_id_from_update():
    adapter = StripePaymentAdapter("sk_test_123")
    payload = {"data": {"object": {"id": "pi_456", "object": "payment_intent"}}}

    assert adapter.get_payment_external_id_from_update(payload) == "pi_456"


def test_get_payment_external_id_from_update_missing_data():
    adapter = StripePaymentAdapter("sk_test_123")
    assert adapter.get_payment_external_id_from_update({}) is None


def test_get_update_id():
    adapter = StripePaymentAdapter("sk_test_123")
    assert adapter.get_update_id({"id": "evt_123"}) == "evt_123"


@patch.object(StripePaymentAdapter, "check_status")
def test_receive_update_relevant_event(mock_check_status, adapter):
    payload = {
        "type": "payment_intent.succeeded",
        "data": {"object": {"id": "pi_456", "object": "payment_intent"}},
    }
    status_update = PaymentStatusUpdate(
        id=None, status="approved", description="ok", update_external_id="pi_456"
    )
    mock_check_status.return_value = status_update

    result = adapter.receive_update(payload)

    assert result == ("pi_456", status_update)
    mock_check_status.assert_called_once_with("pi_456")


def test_receive_update_irrelevant_event_type(adapter):
    payload = {
        "type": "customer.created",
        "data": {"object": {"id": "cus_456"}},
    }

    assert adapter.receive_update(payload) is None


def test_verify_signature_accepts_correctly_signed_body(adapter):
    raw_body, headers = build_signed_request()

    assert adapter.verify_signature(raw_body, headers) is True


def test_verify_signature_rejects_tampered_body(adapter):
    raw_body, headers = build_signed_request()
    tampered_body = raw_body.replace(b"pi_123", b"pi_999")

    assert adapter.verify_signature(tampered_body, headers) is False


def test_verify_signature_rejects_missing_signature_header(adapter):
    raw_body, _headers = build_signed_request()

    assert adapter.verify_signature(raw_body, {}) is False


def test_verify_signature_rejects_when_secret_not_configured():
    adapter = StripePaymentAdapter("sk_test_123", webhook_secret="")
    raw_body, headers = build_signed_request()

    assert adapter.verify_signature(raw_body, headers) is False


def test_verify_signature_is_case_insensitive_to_header_names(adapter):
    raw_body, headers = build_signed_request()
    upper_headers = {k.upper(): v for k, v in headers.items()}

    assert adapter.verify_signature(raw_body, upper_headers) is True


def test_verify_signature_rejects_stale_timestamp(adapter):
    stale_ts = str(int(time.time()) - 3600)
    raw_body, headers = build_signed_request(ts=stale_ts)

    assert adapter.verify_signature(raw_body, headers) is False


def test_get_event_id_derives_key_from_verified_event(adapter):
    """The ledger key comes from the verified `stripe.Event.id` — never from the
    caller's independently-parsed `payload` argument, even though Stripe's
    signature (unlike MercadoPago's) does cover the whole body."""
    raw_body, headers = build_signed_request(event_id="evt_real")

    event_id = adapter.get_event_id(raw_body, headers, payload={"id": "attacker-controlled"})

    assert event_id == "evt_real"


def test_get_event_id_raises_when_signature_invalid(adapter):
    raw_body, headers = build_signed_request()
    tampered_body = raw_body.replace(b"pi_123", b"pi_999")

    with pytest.raises(ProviderWebhookEventIdMissingError):
        adapter.get_event_id(tampered_body, headers, payload={"id": "evt_123"})


@pytest.mark.parametrize("mapped_status", PAYMENT_INTENT_STATUS_MAPPING.values())
def test_payment_intent_status_mapping_values_are_valid_payment_statuses(mapped_status):
    assert mapped_status in PaymentStatuses.values


@pytest.mark.parametrize("mapped_status", REFUND_STATUS_MAPPING.values())
def test_refund_status_mapping_values_are_valid_refund_statuses(mapped_status):
    assert mapped_status in RefundStatuses.values
