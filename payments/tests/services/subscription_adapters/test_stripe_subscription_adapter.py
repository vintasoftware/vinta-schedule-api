import hashlib
import hmac
import json
import time
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from payments.billing_constants import BillingInterval
from payments.constants import PaymentProviders, PaymentStatuses
from payments.exceptions import ProviderWebhookEventIdMissingError
from payments.services.dataclasses import (
    BillingAddress,
    BillingProfile,
    CreatedPlan,
    Plan,
    Subscription,
)
from payments.services.subscription_adapters.stripe_subscription_adapter import (
    StripeSubscriptionAdapter,
)


WEBHOOK_SECRET = "whsec_test_secret"


def build_signed_request(
    event_id: str = "evt_123",
    event_type: str = "invoice.paid",
    object_payload: dict | None = None,
    secret: str = WEBHOOK_SECRET,
    ts: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    """Build a raw body + headers pair signed the way Stripe signs webhooks.

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
def mock_billing_address():
    return Mock(spec=BillingAddress)


@pytest.fixture
def mock_billing_profile(mock_billing_address):
    profile = Mock(spec=BillingProfile)
    profile.email = "test@example.com"
    profile.first_name = "John"
    profile.last_name = "Doe"
    profile.billing_address = mock_billing_address
    return profile


@pytest.fixture
def mock_plan():
    plan = Mock(spec=Plan)
    plan.id = "plan-123"
    plan.name = "Test Plan"
    plan.value = Decimal("99.90")
    plan.currency = "USD"
    plan.billing_day = 1
    plan.billing_interval = BillingInterval.MONTHLY
    return plan


@pytest.fixture
def mock_created_plan():
    plan = Mock(spec=CreatedPlan)
    plan.id = "plan-123"
    plan.external_id = "price_456"
    plan.name = "Test Plan"
    plan.value = Decimal("99.90")
    plan.currency = "USD"
    plan.billing_day = 1
    plan.billing_interval = BillingInterval.MONTHLY
    return plan


@pytest.fixture
def mock_subscription(mock_plan, mock_billing_profile):
    subscription = Mock(spec=Subscription)
    subscription.id = "subscription-123"
    subscription.external_id = "sub_456"
    subscription.plan = mock_plan
    subscription.plan.external_id = "price_456"
    subscription.billing_profile = mock_billing_profile
    return subscription


@pytest.fixture
def adapter():
    return StripeSubscriptionAdapter("sk_test_123", webhook_secret=WEBHOOK_SECRET)


def test_init():
    adapter = StripeSubscriptionAdapter("sk_test_123")
    assert adapter.provider == PaymentProviders.STRIPE
    assert adapter.verifies_full_body is True


@patch("payments.services.subscription_adapters.stripe_subscription_adapter.stripe.Price")
@patch("payments.services.subscription_adapters.stripe_subscription_adapter.stripe.Product")
def test_create_subscription_plan(mock_product, mock_price, adapter, mock_plan):
    """Stripe has no plan-creation call that doesn't also need a `Product` —
    unlike MercadoPago's single `plan().create()`, this always creates both."""
    mock_product.create.return_value = Mock(id="prod_456")
    mock_price.create.return_value = Mock(id="price_456")

    result = adapter.create_subscription_plan(mock_plan)

    assert result == "price_456"
    mock_product.create.assert_called_once_with(
        name="Test Plan", metadata={"plan_id": "plan-123"}, api_key="sk_test_123"
    )
    mock_price.create.assert_called_once_with(
        product="prod_456",
        unit_amount=9990,
        currency="usd",
        recurring={"interval": "month"},
        api_key="sk_test_123",
    )


@patch("payments.services.subscription_adapters.stripe_subscription_adapter.stripe.Price")
@patch("payments.services.subscription_adapters.stripe_subscription_adapter.stripe.Product")
def test_create_subscription_plan_annual_interval(mock_product, mock_price, adapter, mock_plan):
    mock_plan.billing_interval = BillingInterval.ANNUAL
    mock_product.create.return_value = Mock(id="prod_456")
    mock_price.create.return_value = Mock(id="price_456")

    adapter.create_subscription_plan(mock_plan)

    call_kwargs = mock_price.create.call_args.kwargs
    assert call_kwargs["recurring"] == {"interval": "year"}


@patch("payments.services.subscription_adapters.stripe_subscription_adapter.stripe.Price")
def test_update_subscription_plan_creates_new_price_and_archives_old_one(
    mock_price, adapter, mock_plan
):
    """Stripe `Price` objects are immutable — updating a plan means archiving
    the old price and minting a new one, unlike MercadoPago's in-place update."""
    mock_price.retrieve.return_value = Mock(product="prod_456")
    mock_price.create.return_value = Mock(id="price_789")

    result = adapter.update_subscription_plan("price_456", mock_plan)

    assert result == "price_789"
    mock_price.modify.assert_called_once_with("price_456", active=False, api_key="sk_test_123")
    mock_price.create.assert_called_once_with(
        product="prod_456",
        unit_amount=9990,
        currency="usd",
        recurring={"interval": "month"},
        api_key="sk_test_123",
    )


@patch("payments.services.subscription_adapters.stripe_subscription_adapter.stripe.Price")
def test_update_plan_returns_created_plan_with_new_external_id(mock_price, adapter):
    """Since Stripe prices can't be updated in place, `update_plan` must return a
    *new* external id, not the same one it was given — the base interface
    already supports this (it returns a `CreatedPlan`, not `None`).

    Uses a real `CreatedPlan` dataclass instance (rather than `Mock(spec=...)`,
    used elsewhere in this file) because `update_plan` builds its result via
    `dataclasses.replace`, which requires a genuine dataclass instance.
    """
    created_plan = CreatedPlan(
        id="plan-123",
        name="Test Plan",
        value=Decimal("99.90"),
        currency="USD",
        billing_day=1,
        billing_interval=BillingInterval.MONTHLY,
        external_id="price_456",
    )
    mock_price.retrieve.return_value = Mock(product="prod_456")
    mock_price.create.return_value = Mock(id="price_new_999")

    result = adapter.update_plan(created_plan)

    assert result.external_id == "price_new_999"
    assert result.name == created_plan.name


@patch("payments.services.subscription_adapters.stripe_subscription_adapter.stripe.Subscription")
@patch("payments.services.subscription_adapters.stripe_subscription_adapter.stripe.Customer")
def test_create_subscription_success(
    mock_customer, mock_subscription_resource, adapter, mock_subscription
):
    mock_customer.create.return_value = Mock(id="cus_456")
    mock_subscription_resource.create.return_value = Mock(id="sub_created_123")

    result = adapter.create_subscription(mock_subscription, "pm_test_token")

    assert result == "sub_created_123"
    mock_customer.create.assert_called_once()
    customer_kwargs = mock_customer.create.call_args.kwargs
    assert customer_kwargs["email"] == "test@example.com"
    assert customer_kwargs["payment_method"] == "pm_test_token"

    mock_subscription_resource.create.assert_called_once_with(
        customer="cus_456",
        items=[{"price": "price_456"}],
        default_payment_method="pm_test_token",
        metadata={"subscription_id": "subscription-123"},
        api_key="sk_test_123",
    )


@patch("payments.services.subscription_adapters.stripe_subscription_adapter.stripe.Subscription")
def test_cancel_subscription_success(mock_subscription_resource, adapter, mock_subscription):
    adapter.cancel_subscription(mock_subscription)

    mock_subscription_resource.cancel.assert_called_once_with("sub_456", api_key="sk_test_123")


@patch("payments.services.subscription_adapters.stripe_subscription_adapter.stripe.Subscription")
def test_update_subscription_payment_token_success(
    mock_subscription_resource, adapter, mock_subscription
):
    """`default_payment_method` is a genuine `Subscription` field (confirmed via
    `'default_payment_method' in stripe.Subscription.__annotations__` == `True`)
    — same field name `create_subscription` already sets."""
    adapter.update_subscription_payment_token(mock_subscription, "pm_new_token")

    mock_subscription_resource.modify.assert_called_once_with(
        "sub_456", default_payment_method="pm_new_token", api_key="sk_test_123"
    )


def test_update_subscription_payment_token_without_external_id(adapter, mock_subscription):
    mock_subscription.external_id = None

    from payments.exceptions import PaymentAdapterError

    with pytest.raises(PaymentAdapterError):
        adapter.update_subscription_payment_token(mock_subscription, "pm_new_token")


def test_get_subscription_external_id_from_update_subscription_event(adapter):
    payload = {
        "type": "customer.subscription.updated",
        "data": {"object": {"id": "sub_456"}},
    }

    assert adapter.get_subscription_external_id_from_update(payload) == "sub_456"


def test_get_subscription_external_id_from_update_invoice_event(adapter):
    """`2026-06-24.dahlia`-shaped invoice: `Invoice.subscription` was removed —
    the id lives at `parent.subscription_details.subscription`. Shape derived
    from introspecting `stripe.Invoice.__annotations__` /
    `stripe.Invoice.Parent.__annotations__` /
    `stripe.Invoice.Parent.SubscriptionDetails.__annotations__` on the pinned
    `stripe==15.3.1` SDK."""
    payload = {
        "type": "invoice.paid",
        "data": {
            "object": {
                "id": "in_123",
                "parent": {
                    "type": "subscription_details",
                    "subscription_details": {"subscription": "sub_456"},
                },
            }
        },
    }

    assert adapter.get_subscription_external_id_from_update(payload) == "sub_456"


def test_get_subscription_external_id_from_update_invoice_event_legacy_fallback(adapter):
    """Pre-dahlia payloads (or any future delivery lacking `parent`) fall back to
    the bare `subscription` field rather than returning `None`."""
    payload = {
        "type": "invoice.paid",
        "data": {"object": {"id": "in_123", "subscription": "sub_456"}},
    }

    assert adapter.get_subscription_external_id_from_update(payload) == "sub_456"


def test_get_subscription_external_id_from_update_invoice_event_missing_subscription(adapter):
    payload = {
        "type": "invoice.paid",
        "data": {"object": {"id": "in_123"}},
    }

    assert adapter.get_subscription_external_id_from_update(payload) is None


def test_get_subscription_external_id_from_update_irrelevant_event(adapter):
    payload = {"type": "customer.created", "data": {"object": {"id": "cus_456"}}}

    assert adapter.get_subscription_external_id_from_update(payload) is None


def test_is_payment_update_true(adapter):
    assert adapter.is_payment_update({"type": "invoice.paid"}) is True


def test_is_payment_update_false(adapter):
    assert adapter.is_payment_update({"type": "customer.subscription.updated"}) is False


@patch("payments.services.subscription_adapters.stripe_subscription_adapter.stripe.PaymentIntent")
def test_get_payment_payload(mock_payment_intent, adapter):
    mock_payment_intent.retrieve.return_value = Mock(to_dict=lambda: {"id": "pi_456"})

    result = adapter.get_payment_payload("pi_456")

    assert result == {"id": "pi_456"}
    mock_payment_intent.retrieve.assert_called_once_with("pi_456", api_key="sk_test_123")


@patch("payments.services.subscription_adapters.stripe_subscription_adapter.stripe.Subscription")
def test_get_subscription_payload(mock_subscription_resource, adapter):
    """`latest_invoice.payment_intent` is not a valid expand path under the
    pinned `2026-06-24.dahlia` API version — `Invoice.payment_intent` was
    removed (confirmed via `'payment_intent' in stripe.Invoice.__annotations__`
    == `False`) and Stripe rejects an unknown expand path with
    `invalid_request_error`. `latest_invoice.payments` is the replacement:
    `Invoice.payments` is a valid, `Optional[ListObject["InvoicePayment"]]`
    field per `stripe.Invoice.__annotations__`."""
    mock_subscription_resource.retrieve.return_value = Mock(to_dict=lambda: {"id": "sub_456"})

    result = adapter.get_subscription_payload("sub_456")

    assert result == {"id": "sub_456"}
    mock_subscription_resource.retrieve.assert_called_once_with(
        "sub_456", expand=["latest_invoice.payments"], api_key="sk_test_123"
    )


def test_get_payment_external_id_from_subscription_payload_expanded(adapter):
    """Shape derived from introspecting `stripe.InvoicePayment.__annotations__`
    (`payment: InvoicePayment.Payment`) and
    `stripe.InvoicePayment.Payment.__annotations__`
    (`payment_intent: Union[str, PaymentIntent, None]`) on the pinned
    `stripe==15.3.1` SDK — `latest_invoice.payments.data[0].payment.payment_intent`,
    not the removed `latest_invoice.payment_intent`."""
    subscription_payload = {
        "latest_invoice": {
            "payments": {
                "object": "list",
                "data": [
                    {
                        "id": "inpay_123",
                        "object": "invoice_payment",
                        "payment": {
                            "type": "payment_intent",
                            "payment_intent": {"id": "pi_456", "object": "payment_intent"},
                        },
                    }
                ],
            }
        }
    }

    result = adapter.get_payment_external_id_from_subscription_payload(subscription_payload)

    assert result == "pi_456"


def test_get_payment_external_id_from_subscription_payload_unexpanded_id(adapter):
    """`InvoicePayment.Payment.payment_intent` is a bare id string unless
    further expanded — which `get_subscription_payload` never asks for, since
    only the id is needed."""
    subscription_payload = {
        "latest_invoice": {
            "payments": {
                "object": "list",
                "data": [
                    {
                        "id": "inpay_123",
                        "object": "invoice_payment",
                        "payment": {"type": "payment_intent", "payment_intent": "pi_789"},
                    }
                ],
            }
        }
    }

    result = adapter.get_payment_external_id_from_subscription_payload(subscription_payload)

    assert result == "pi_789"


def test_get_payment_external_id_from_subscription_payload_missing_invoice(adapter):
    assert adapter.get_payment_external_id_from_subscription_payload({}) is None


def test_get_payment_external_id_from_subscription_payload_no_payments_yet(adapter):
    """An invoice that hasn't been paid yet has an empty `payments.data` list —
    must return `None`, not raise an `IndexError`."""
    subscription_payload = {"latest_invoice": {"payments": {"object": "list", "data": []}}}

    assert adapter.get_payment_external_id_from_subscription_payload(subscription_payload) is None


def test_create_subscription_payment_from_payment_payload(adapter):
    """`PaymentIntent.charges` was removed from the API (confirmed via
    `'charges' in stripe.PaymentIntent.__annotations__` == `False`), so
    `billing_profile` is now always an explicitly empty `BillingProfile` — see
    `_billing_profile_from_payment_intent_payload`'s docstring.
    `PaymentService.receive_subscription_payment_update` never reads it (it
    sources billing info from the subscription's own organization), so this is
    a shape requirement, not a functional regression.
    """
    payment_payload = {
        "id": "pi_456",
        "amount": 9990,
        "currency": "usd",
        "status": "succeeded",
        "payment_method_types": ["card"],
        "description": "Subscription payment",
    }

    result = adapter.create_subscription_payment_from_payment_payload("sub_456", payment_payload)

    assert result.subscription_external_id == "sub_456"
    assert result.external_id == "pi_456"
    assert result.value == Decimal("99.90")
    assert result.currency == "USD"
    assert result.payment_provider == PaymentProviders.STRIPE
    assert result.status == "succeeded"
    assert result.billing_profile is not None
    assert result.billing_profile.email is None
    assert result.billing_profile.first_name is None
    assert result.billing_profile.last_name is None


def test_create_status_update_from_payment_payload_maps_known_status(adapter):
    payment_payload = {"id": "pi_456", "status": "succeeded"}

    result = adapter.create_status_update_from_payment_payload(payment_payload)

    assert result.status == PaymentStatuses.APPROVED
    assert result.update_external_id == "pi_456"


@patch("payments.services.subscription_adapters.stripe_subscription_adapter.logger")
def test_create_status_update_from_payment_payload_maps_unknown_status(mock_logger, adapter):
    payment_payload = {"id": "pi_456", "status": "some_new_status"}

    result = adapter.create_status_update_from_payment_payload(payment_payload)

    assert result.status == PaymentStatuses.UNKNOWN
    mock_logger.error.assert_called_once()


def test_verify_signature_accepts_correctly_signed_body(adapter):
    raw_body, headers = build_signed_request()

    assert adapter.verify_signature(raw_body, headers) is True


def test_verify_signature_rejects_tampered_body(adapter):
    raw_body, headers = build_signed_request()
    tampered_body = raw_body.replace(b"sub_123", b"sub_999")

    assert adapter.verify_signature(tampered_body, headers) is False


def test_verify_signature_rejects_missing_signature_header(adapter):
    raw_body, _headers = build_signed_request()

    assert adapter.verify_signature(raw_body, {}) is False


def test_verify_signature_rejects_when_secret_not_configured():
    adapter = StripeSubscriptionAdapter("sk_test_123", webhook_secret="")
    raw_body, headers = build_signed_request()

    assert adapter.verify_signature(raw_body, headers) is False


def test_verify_signature_rejects_stale_timestamp(adapter):
    stale_ts = str(int(time.time()) - 3600)
    raw_body, headers = build_signed_request(ts=stale_ts)

    assert adapter.verify_signature(raw_body, headers) is False


def test_get_event_id_derives_key_from_verified_event(adapter):
    raw_body, headers = build_signed_request(event_id="evt_real")

    event_id = adapter.get_event_id(raw_body, headers, payload={"id": "attacker-controlled"})

    assert event_id == "evt_real"


def test_get_event_id_raises_when_signature_invalid(adapter):
    raw_body, headers = build_signed_request()
    tampered_body = raw_body.replace(b"sub_123", b"sub_999")

    with pytest.raises(ProviderWebhookEventIdMissingError):
        adapter.get_event_id(tampered_body, headers, payload={"id": "evt_123"})
