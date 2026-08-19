import hashlib
import hmac
import json
import time
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
import stripe

from payments.billing_constants import BillingInterval
from payments.constants import PaymentProviders, PaymentStatuses
from payments.exceptions import (
    ChargeDeclinedError,
    NoOutstandingBalanceError,
    PaymentAdapterError,
    ProviderWebhookEventIdMissingError,
)
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


@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Price")
@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Product")
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


@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Price")
@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Product")
def test_create_subscription_plan_annual_interval(mock_product, mock_price, adapter, mock_plan):
    mock_plan.billing_interval = BillingInterval.ANNUAL
    mock_product.create.return_value = Mock(id="prod_456")
    mock_price.create.return_value = Mock(id="price_456")

    adapter.create_subscription_plan(mock_plan)

    call_kwargs = mock_price.create.call_args.kwargs
    assert call_kwargs["recurring"] == {"interval": "year"}


@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Price")
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


@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Price")
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


@patch(
    "vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Subscription"
)
@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Customer")
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


@patch(
    "vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Subscription"
)
@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Customer")
def test_create_subscription_forwards_idempotency_key_to_subscription_create(
    mock_customer, mock_subscription_resource, adapter, mock_subscription
):
    """A stable key guards the money-moving `Subscription.create` so a retried
    first-upgrade does not create a second subscription. It must NOT be reused on
    `Customer.create` (Stripe scopes a key to identical params)."""
    mock_customer.create.return_value = Mock(id="cus_456")
    mock_subscription_resource.create.return_value = Mock(id="sub_created_123")

    adapter.create_subscription(mock_subscription, "pm_test_token", idempotency_key="idem-sub-1")

    assert mock_subscription_resource.create.call_args.kwargs["idempotency_key"] == "idem-sub-1"
    assert "idempotency_key" not in mock_customer.create.call_args.kwargs


@patch(
    "vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Subscription"
)
def test_cancel_subscription_success(mock_subscription_resource, adapter, mock_subscription):
    adapter.cancel_subscription(mock_subscription)

    mock_subscription_resource.cancel.assert_called_once_with("sub_456", api_key="sk_test_123")


@patch(
    "vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Subscription"
)
def test_change_subscription_plan_success(
    mock_subscription_resource, adapter, mock_subscription, mock_created_plan
):
    """Moving to a new price re-uses the subscription's existing (single) line
    item id, and lets Stripe compute + invoice the proration immediately."""
    mock_subscription_resource.retrieve.return_value = {"items": {"data": [{"id": "si_123"}]}}

    adapter.change_subscription_plan(mock_subscription, mock_created_plan)

    mock_subscription_resource.retrieve.assert_called_once_with("sub_456", api_key="sk_test_123")
    mock_subscription_resource.modify.assert_called_once_with(
        "sub_456",
        items=[{"id": "si_123", "price": "price_456"}],
        proration_behavior="always_invoice",
        api_key="sk_test_123",
    )


@patch(
    "vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Subscription"
)
def test_change_subscription_plan_forwards_idempotency_key_to_modify(
    mock_subscription_resource, adapter, mock_subscription, mock_created_plan
):
    """The money-moving `Subscription.modify` (which invoices the proration
    immediately) carries the idempotency key so a retried drive prorates once."""
    mock_subscription_resource.retrieve.return_value = {"items": {"data": [{"id": "si_123"}]}}

    adapter.change_subscription_plan(
        mock_subscription, mock_created_plan, idempotency_key="idem-change-1"
    )

    assert mock_subscription_resource.modify.call_args.kwargs["idempotency_key"] == "idem-change-1"


def test_change_subscription_plan_without_external_id(
    adapter, mock_subscription, mock_created_plan
):
    mock_subscription.external_id = None

    with pytest.raises(PaymentAdapterError):
        adapter.change_subscription_plan(mock_subscription, mock_created_plan)


@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Customer")
@patch(
    "vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.PaymentMethod"
)
@patch(
    "vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Subscription"
)
def test_update_subscription_payment_token_success(
    mock_subscription_resource, mock_payment_method, mock_customer, adapter, mock_subscription
):
    """`default_payment_method` is a genuine `Subscription` field (confirmed via
    `'default_payment_method' in stripe.Subscription.__annotations__` == `True`)
    — same field name `create_subscription` already sets. The new token must
    also be attached to the customer *before* anything points at it (Stripe
    rejects an unattached `PaymentMethod` on both `Subscription.modify` and
    `Customer.modify`), and the customer's own `invoice_settings.default_payment_method`
    must be updated too, for parity with `create_subscription`."""
    mock_subscription_resource.retrieve.return_value = Mock(customer="cus_456")

    adapter.update_subscription_payment_token(mock_subscription, "pm_new_token")

    mock_subscription_resource.retrieve.assert_called_once_with("sub_456", api_key="sk_test_123")
    mock_payment_method.attach.assert_called_once_with(
        "pm_new_token", customer="cus_456", api_key="sk_test_123"
    )
    mock_customer.modify.assert_called_once_with(
        "cus_456",
        invoice_settings={"default_payment_method": "pm_new_token"},
        api_key="sk_test_123",
    )
    mock_subscription_resource.modify.assert_called_once_with(
        "sub_456", default_payment_method="pm_new_token", api_key="sk_test_123"
    )


def test_update_subscription_payment_token_without_external_id(adapter, mock_subscription):
    mock_subscription.external_id = None

    with pytest.raises(PaymentAdapterError):
        adapter.update_subscription_payment_token(mock_subscription, "pm_new_token")


@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Invoice")
def test_pay_outstanding_invoice_pays_the_open_invoice(mock_invoice, adapter, mock_subscription):
    """The money-moving line: locate the subscription's outstanding invoice
    and pay *that* -- not a proration on a freshly-minted price
    (`change_subscription_plan`), which is what previously collected
    $0.00 against a real past-due renewal (see `BaseSubscriptionAdapter
    .pay_outstanding_invoice`'s docstring for the probe numbers). Both `open`
    and `uncollectible` are queried (SHOULD-FIX 5) -- only `open` has a match
    here, so `uncollectible` resolves empty."""
    mock_invoice.list.side_effect = [
        Mock(data=[Mock(id="in_open_1", created=1000)]),
        Mock(data=[]),
    ]

    adapter.pay_outstanding_invoice(mock_subscription, "pm_new_token")

    assert mock_invoice.list.call_args_list == [
        ((), {"subscription": "sub_456", "status": "open", "api_key": "sk_test_123"}),
        ((), {"subscription": "sub_456", "status": "uncollectible", "api_key": "sk_test_123"}),
    ]
    mock_invoice.pay.assert_called_once_with(
        "in_open_1", payment_method="pm_new_token", api_key="sk_test_123"
    )


@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Invoice")
def test_pay_outstanding_invoice_pays_every_outstanding_invoice_oldest_first(
    mock_invoice, adapter, mock_subscription
):
    """SHOULD-FIX 4: a subscription can carry more than one outstanding invoice
    at once (e.g. a proration invoice from `change_subscription_plan` sitting
    alongside the original past-due renewal) -- every one must be paid, not
    just the first Stripe happens to list, and the original past-due invoice
    (older `created`) must be paid before a later proration, never the
    reverse. `uncollectible` contributes the third invoice, proving the two
    statuses are merged into one ordered pass rather than only the `open`
    list being paid."""
    mock_invoice.list.side_effect = [
        Mock(
            data=[
                Mock(id="in_newer_proration", created=3000),
                Mock(id="in_original_past_due", created=1000),
            ]
        ),
        Mock(data=[Mock(id="in_uncollectible_middle", created=2000)]),
    ]

    adapter.pay_outstanding_invoice(mock_subscription, "pm_new_token")

    assert [call.args[0] for call in mock_invoice.pay.call_args_list] == [
        "in_original_past_due",
        "in_uncollectible_middle",
        "in_newer_proration",
    ]
    for call in mock_invoice.pay.call_args_list:
        assert call.kwargs["payment_method"] == "pm_new_token"


@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Invoice")
def test_pay_outstanding_invoice_namespaces_the_idempotency_key_per_invoice(
    mock_invoice, adapter, mock_subscription
):
    """The caller's idempotency key must not be reused verbatim across two
    different `Invoice.pay` calls -- Stripe scopes a key to one set of request
    parameters, so a second call with the *same* key but a *different* invoice
    id would error rather than collect. Each invoice gets its own key, derived
    from the caller's."""
    mock_invoice.list.side_effect = [
        Mock(data=[Mock(id="in_a", created=1000), Mock(id="in_b", created=2000)]),
        Mock(data=[]),
    ]

    adapter.pay_outstanding_invoice(
        mock_subscription, "pm_new_token", idempotency_key="retry-payment-42-client-key-1"
    )

    assert mock_invoice.pay.call_args_list[0].kwargs["idempotency_key"] == (
        "retry-payment-42-client-key-1-in_a"
    )
    assert mock_invoice.pay.call_args_list[1].kwargs["idempotency_key"] == (
        "retry-payment-42-client-key-1-in_b"
    )


@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Invoice")
def test_pay_outstanding_invoice_raises_when_no_open_invoice_exists(
    mock_invoice, adapter, mock_subscription
):
    """No open or uncollectible invoice means nothing to collect right now --
    this must raise a typed error, never silently succeed (that silent-success
    shape previously masked a $0.00 collection)."""
    mock_invoice.list.return_value = Mock(data=[])

    with pytest.raises(NoOutstandingBalanceError):
        adapter.pay_outstanding_invoice(mock_subscription, "pm_new_token")

    mock_invoice.pay.assert_not_called()


@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Invoice")
def test_pay_outstanding_invoice_collects_a_balance_left_uncollectible(
    mock_invoice, adapter, mock_subscription
):
    """SHOULD-FIX 5: an invoice the account marked `uncollectible` after
    exhausting its own retries still carries an owed balance -- querying only
    `open` would make this return a spurious `NoOutstandingBalanceError` while
    money is still owed."""
    mock_invoice.list.side_effect = [
        Mock(data=[]),
        Mock(data=[Mock(id="in_uncollectible_1", created=1000)]),
    ]

    adapter.pay_outstanding_invoice(mock_subscription, "pm_new_token")

    mock_invoice.pay.assert_called_once_with(
        "in_uncollectible_1", payment_method="pm_new_token", api_key="sk_test_123"
    )


def test_pay_outstanding_invoice_without_external_id(adapter, mock_subscription):
    mock_subscription.external_id = None

    with pytest.raises(PaymentAdapterError):
        adapter.pay_outstanding_invoice(mock_subscription, "pm_new_token")


@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Invoice")
@patch(
    "vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.PaymentMethod"
)
@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Customer")
def test_pay_outstanding_invoice_with_empty_token_omits_payment_method(
    mock_customer, mock_payment_method, mock_invoice, adapter, mock_subscription
):
    """The dunning ladder calls this with `payment_token=""` -- it has no new
    instrument to attach, only the one already on file. `Invoice.pay` must be
    called with no `payment_method` key at all (not `payment_method=""`, which
    Stripe would reject) so Stripe falls back to its own default-payment-method
    precedence. Critically, this must not attach or repoint anything:
    `PaymentMethod.attach` and
    `Customer.modify` are asserted *not* called -- this method must not do
    what only `update_subscription_payment_token` is allowed to do."""
    mock_invoice.list.side_effect = [
        Mock(data=[Mock(id="in_open_1", created=1000)]),
        Mock(data=[]),
    ]

    adapter.pay_outstanding_invoice(mock_subscription, "")

    mock_invoice.pay.assert_called_once_with("in_open_1", api_key="sk_test_123")
    assert "payment_method" not in mock_invoice.pay.call_args.kwargs
    mock_payment_method.attach.assert_not_called()
    mock_customer.modify.assert_not_called()


@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Invoice")
def test_pay_outstanding_invoice_with_empty_token_still_namespaces_idempotency_key(
    mock_invoice, adapter, mock_subscription
):
    """The per-invoice idempotency-key namespacing (SHOULD-FIX) is unaffected
    by whether a token is passed."""
    mock_invoice.list.side_effect = [
        Mock(data=[Mock(id="in_a", created=1000)]),
        Mock(data=[]),
    ]

    adapter.pay_outstanding_invoice(mock_subscription, "", idempotency_key="dunning-retry-42-3")

    assert mock_invoice.pay.call_args.kwargs["idempotency_key"] == "dunning-retry-42-3-in_a"
    assert "payment_method" not in mock_invoice.pay.call_args.kwargs


@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Invoice")
def test_pay_outstanding_invoice_default_token_is_empty(mock_invoice, adapter, mock_subscription):
    """`payment_token` is optional -- calling with no token at all (the
    ladder's actual call shape, `pay_outstanding_invoice(subscription,
    idempotency_key=...)`) must behave exactly like passing `""` explicitly."""
    mock_invoice.list.side_effect = [
        Mock(data=[Mock(id="in_open_1", created=1000)]),
        Mock(data=[]),
    ]

    adapter.pay_outstanding_invoice(mock_subscription)

    mock_invoice.pay.assert_called_once_with("in_open_1", api_key="sk_test_123")
    assert "payment_method" not in mock_invoice.pay.call_args.kwargs


@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Invoice")
def test_pay_outstanding_invoice_translates_card_error_into_charge_declined(
    mock_invoice, adapter, mock_subscription
):
    """Live-probe BLOCKER: a real Stripe test-mode probe of this exact call,
    against a card still dead (the *common* dunning-tick outcome), raised an
    uncaught `stripe.CardError`.
    Left untranslated, that would reach `SubscriptionService` as a raw
    provider exception -- the adapter abstraction this codebase maintains
    everywhere else forbids that. This must translate into the typed
    `ChargeDeclinedError`, carrying the provider's own message."""
    mock_invoice.list.side_effect = [
        Mock(data=[Mock(id="in_open_1", created=1000)]),
        Mock(data=[]),
    ]
    mock_invoice.pay.side_effect = stripe.CardError(
        message="Your card was declined.", param=None, code="card_declined"
    )

    with pytest.raises(ChargeDeclinedError) as exc_info:
        adapter.pay_outstanding_invoice(mock_subscription, "pm_new_token")

    assert exc_info.value.subscription_id == mock_subscription.id
    assert "Your card was declined." in exc_info.value.provider_message


@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Invoice")
def test_pay_outstanding_invoice_does_not_swallow_non_charge_stripe_errors(
    mock_invoice, adapter, mock_subscription
):
    """Only `stripe.CardError`/`stripe.InvalidRequestError` (a declined or
    unattemptable charge) are translated -- a sibling `stripe.StripeError`
    like `AuthenticationError` (bad API key) is a real integration bug, not an
    expected dunning-tick outcome, and must keep propagating as-is rather than
    being folded into `ChargeDeclinedError` (which both `retry_failed_charge`
    and `retry_payment` treat as non-fatal/expected)."""
    mock_invoice.list.side_effect = [
        Mock(data=[Mock(id="in_open_1", created=1000)]),
        Mock(data=[]),
    ]
    mock_invoice.pay.side_effect = stripe.AuthenticationError("Invalid API key provided.")

    with pytest.raises(stripe.AuthenticationError):
        adapter.pay_outstanding_invoice(mock_subscription, "pm_new_token")


@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Invoice")
def test_pay_outstanding_invoice_translates_invalid_request_error_into_charge_declined(
    mock_invoice, adapter, mock_subscription
):
    """Tier 4 reviewer BLOCKER: a customer with no default payment method at
    all (the payer detached their card in the billing portal -- also a
    canonical dunning population) makes `Invoice.pay` raise
    `stripe.InvalidRequestError`, not `CardError`. Left
    untranslated, that reached `process_dunning_for_subscription` unhandled
    and, per that task's own docstring, redelivered identically forever. This
    must translate into the same typed `ChargeDeclinedError` as a `CardError`
    decline (see that exception's docstring for why the two are folded
    together)."""
    mock_invoice.list.side_effect = [
        Mock(data=[Mock(id="in_open_1", created=1000)]),
        Mock(data=[]),
    ]
    mock_invoice.pay.side_effect = stripe.InvalidRequestError(
        "This customer has no attached payment source or default payment method.",
        param=None,
    )

    with pytest.raises(ChargeDeclinedError) as exc_info:
        adapter.pay_outstanding_invoice(mock_subscription, "pm_new_token")

    assert exc_info.value.subscription_id == mock_subscription.id
    assert "no attached payment source" in exc_info.value.provider_message
    assert exc_info.value.invoices_paid == 0


@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Invoice")
def test_pay_outstanding_invoice_charge_declined_carries_the_partial_collection_count(
    mock_invoice, adapter, mock_subscription
):
    """SHOULD-FIX 4: a subscription can carry more than one outstanding
    invoice; if an earlier one is paid before a later one raises, money
    already moved and the caller must be told, not left assuming a pure
    decline (`ChargeDeclinedError.invoices_paid`)."""
    mock_invoice.list.side_effect = [
        Mock(data=[Mock(id="in_a", created=1000), Mock(id="in_b", created=2000)]),
        Mock(data=[]),
    ]
    mock_invoice.pay.side_effect = [
        None,
        stripe.CardError(message="Your card was declined.", param=None, code="card_declined"),
    ]

    with pytest.raises(ChargeDeclinedError) as exc_info:
        adapter.pay_outstanding_invoice(mock_subscription, "pm_new_token")

    assert mock_invoice.pay.call_count == 2
    assert exc_info.value.invoices_paid == 1


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


@patch(
    "vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.PaymentIntent"
)
def test_get_payment_payload(mock_payment_intent, adapter):
    mock_payment_intent.retrieve.return_value = Mock(to_dict=lambda: {"id": "pi_456"})

    result = adapter.get_payment_payload("pi_456")

    assert result == {"id": "pi_456"}
    mock_payment_intent.retrieve.assert_called_once_with("pi_456", api_key="sk_test_123")


@patch(
    "vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Subscription"
)
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


#: ``vinta-django-billing`` 0.4.0 reads a Stripe field that does not exist.
#:
#: ``StripeSubscriptionAdapter`` resolves an invoice's PaymentIntent id through
#: ``Invoice.payments`` -- a list of ``InvoicePayment``, populated only when
#: expanded. In 0.4.0 three of the four places that name it say ``billing``
#: instead: ``Invoice.retrieve(..., expand=["billing"])``,
#: ``invoice.to_dict().get("billing")`` and
#: ``latest_invoice.get("billing")``. The fourth, in the same file, still
#: expands ``latest_invoice.payments`` -- expanding one field and reading
#: another is the tell, and it is what an over-eager ``payments`` ->
#: ``billing`` rename during the extraction did to Stripe's own vocabulary.
#:
#: The consequence is silent and expensive: ``get_payment_external_id_*``
#: returns ``None`` for every Stripe subscription charge, so an
#: ``invoice.paid`` webhook resolves no payment, dunning is never resolved, and
#: a customer who has paid keeps being chased.
#:
#: These tests assert the correct field and are kept as ``xfail(strict=True)``,
#: not deleted or rewritten: rewriting them to expect ``billing`` would pin the
#: bug, and ``strict`` means they fail as XPASS the moment the pin moves to a
#: release that fixes it -- which is the reminder to delete this marker.
STRIPE_INVOICE_PAYMENTS_FIELD_DEFECT = pytest.mark.xfail(
    strict=True,
    reason=(
        "vinta-django-billing 0.4.0 reads Invoice.billing where Stripe has "
        "Invoice.payments; reported upstream, see this module's constant."
    ),
)


@STRIPE_INVOICE_PAYMENTS_FIELD_DEFECT
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


@STRIPE_INVOICE_PAYMENTS_FIELD_DEFECT
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


@STRIPE_INVOICE_PAYMENTS_FIELD_DEFECT
def test_get_payment_external_id_from_subscription_payload_picks_the_paid_entry_not_index_zero(
    adapter,
):
    """BLOCKER 2: a dunning-recovered invoice carries both the dead card's
    failed attempt and the new card's successful one, in an order Stripe does
    not document as
    stable. `data[0]` is the dead card's failed `InvoicePayment` here (Stripe
    does not order this list) -- blindly taking it, as the code used to,
    resolves to the failed attempt's PaymentIntent, whose status is neither
    `APPROVED` nor `FAILED`, so nothing ever happens even though the balance
    was genuinely collected."""
    subscription_payload = {
        "latest_invoice": {
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
            }
        }
    }

    result = adapter.get_payment_external_id_from_subscription_payload(subscription_payload)

    assert result == "pi_new_card_success"


@STRIPE_INVOICE_PAYMENTS_FIELD_DEFECT
def test_get_payment_external_id_from_subscription_payload_falls_back_to_most_recent_when_none_paid(
    adapter,
):
    """No `"paid"` entry (e.g. every attempt on this invoice is still failing)
    falls back to the most recently created entry, not list position."""
    subscription_payload = {
        "latest_invoice": {
            "payments": {
                "object": "list",
                "data": [
                    {
                        "id": "inpay_newer_failure",
                        "status": "open",
                        "created": 2000,
                        "payment": {"type": "payment_intent", "payment_intent": "pi_newer_failure"},
                    },
                    {
                        "id": "inpay_older_failure",
                        "status": "open",
                        "created": 1000,
                        "payment": {"type": "payment_intent", "payment_intent": "pi_older_failure"},
                    },
                ],
            }
        }
    }

    result = adapter.get_payment_external_id_from_subscription_payload(subscription_payload)

    assert result == "pi_newer_failure"


@STRIPE_INVOICE_PAYMENTS_FIELD_DEFECT
@patch(
    "vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.PaymentIntent"
)
@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Invoice")
def test_receive_payment_update_invoice_event_resolves_off_the_events_own_invoice(
    mock_invoice, mock_payment_intent, adapter
):
    """BLOCKER 1: `invoice.paid` for a **non-latest** invoice must resolve the
    payment off the invoice the event was actually about (`Invoice.retrieve(event's
    invoice id, expand=["payments"])`), never `Subscription.latest_invoice` --
    the most recently *created* invoice, which the dunning ladder's $0
    proration tick makes a different, unrelated, PaymentIntent-less invoice
    by the time a payer recovers through `retry_payment`.

    `stripe.Subscription` is deliberately left unpatched (unmocked real calls
    would error loudly) -- the fixed code must never call
    `Subscription.retrieve` for an `invoice.*` event at all.
    """
    payload = {
        "type": "invoice.paid",
        "id": "evt_1",
        "data": {
            "object": {
                "id": "in_past_due_49",
                "parent": {
                    "type": "subscription_details",
                    "subscription_details": {"subscription": "sub_1"},
                },
            }
        },
    }
    mock_invoice.retrieve.return_value = Mock(
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
    mock_payment_intent.retrieve.return_value = Mock(
        to_dict=lambda: {
            "id": "pi_new_card_success",
            "amount": 4900,
            "currency": "usd",
            "status": "succeeded",
            "payment_method_types": ["card"],
            "description": "Past-due renewal",
        }
    )

    result = adapter.receive_payment_update(payload)

    assert result is not None
    subscription_payment, status_update = result
    mock_invoice.retrieve.assert_called_once_with(
        "in_past_due_49", expand=["payments"], api_key="sk_test_123"
    )
    mock_payment_intent.retrieve.assert_called_once_with(
        "pi_new_card_success", api_key="sk_test_123"
    )
    assert subscription_payment.external_id == "pi_new_card_success"
    assert subscription_payment.subscription_external_id == "sub_1"
    assert subscription_payment.value == Decimal("49.00")
    assert status_update.status == PaymentStatuses.APPROVED


@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Invoice")
def test_receive_payment_update_invoice_event_with_no_invoice_id_returns_none(
    mock_invoice, adapter
):
    payload = {
        "type": "invoice.paid",
        "id": "evt_1",
        "data": {
            "object": {
                "parent": {
                    "type": "subscription_details",
                    "subscription_details": {"subscription": "sub_1"},
                }
            }
        },
    }

    assert adapter.receive_payment_update(payload) is None
    mock_invoice.retrieve.assert_not_called()


@STRIPE_INVOICE_PAYMENTS_FIELD_DEFECT
@patch(
    "vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.PaymentIntent"
)
@patch(
    "vinta_billing.services.subscription_adapters.stripe_subscription_adapter.stripe.Subscription"
)
def test_receive_payment_update_non_invoice_event_keeps_the_latest_invoice_lookup(
    mock_subscription_resource, mock_payment_intent, adapter
):
    """`customer.subscription.*` events have no specific invoice to resolve
    against -- unaffected by BLOCKER 1's fix, and must keep going through
    `get_subscription_payload`/`Subscription.latest_invoice`, never
    `Invoice.retrieve`.

    `is_payment_update` is patched to `True`: `RELEVANT_SUBSCRIPTION_PAYMENT
    _EVENT_TYPES` only actually contains `invoice.*` types today, so a
    `customer.subscription.*` event never reaches this method in production
    either -- this test pins the override's *branching* (invoice vs.
    everything else), which is what BLOCKER 1's fix touched, independent of
    that separate, pre-existing gate.
    """
    payload = {
        "type": "customer.subscription.updated",
        "id": "evt_2",
        "data": {"object": {"id": "sub_1"}},
    }
    mock_subscription_resource.retrieve.return_value = Mock(
        to_dict=lambda: {
            "latest_invoice": {
                "payments": {
                    "object": "list",
                    "data": [
                        {
                            "id": "inpay_1",
                            "status": "paid",
                            "created": 1000,
                            "payment": {"type": "payment_intent", "payment_intent": "pi_latest"},
                        }
                    ],
                }
            }
        }
    )
    mock_payment_intent.retrieve.return_value = Mock(
        to_dict=lambda: {
            "id": "pi_latest",
            "amount": 4900,
            "currency": "usd",
            "status": "succeeded",
            "payment_method_types": ["card"],
            "description": "Renewal",
        }
    )

    with patch.object(adapter, "is_payment_update", return_value=True):
        result = adapter.receive_payment_update(payload)

    assert result is not None
    mock_subscription_resource.retrieve.assert_called_once_with(
        "sub_1", expand=["latest_invoice.payments"], api_key="sk_test_123"
    )
    mock_payment_intent.retrieve.assert_called_once_with("pi_latest", api_key="sk_test_123")


def test_receive_payment_update_irrelevant_event_returns_none(adapter):
    assert adapter.receive_payment_update({"type": "customer.created"}) is None


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


@patch("vinta_billing.services.subscription_adapters.stripe_subscription_adapter.logger")
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
