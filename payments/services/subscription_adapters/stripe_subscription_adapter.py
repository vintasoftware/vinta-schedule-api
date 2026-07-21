import logging
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Literal

import stripe

from payments.billing_constants import BillingInterval
from payments.constants import PaymentProviders, PaymentStatuses
from payments.exceptions import PaymentAdapterError, ProviderWebhookEventIdMissingError
from payments.services.dataclasses import (
    BillingAddress,
    BillingProfile,
    CreatedPlan,
    PaymentStatusUpdate,
    Plan,
    Subscription,
    SubscriptionPayment,
)
from payments.services.payment_adapters.stripe_payment_adapter import (
    PAYMENT_INTENT_STATUS_MAPPING,
    ZERO_DECIMAL_CURRENCIES,
    to_stripe_amount,
)
from payments.services.stripe_signature import verify_stripe_event
from payments.services.subscription_adapters.base import BaseSubscriptionAdapter


logger = logging.getLogger(__name__)


#: Stripe webhook event types this adapter's `is_payment_update` acts on.
RELEVANT_SUBSCRIPTION_PAYMENT_EVENT_TYPES = frozenset({"invoice.paid", "invoice.payment_failed"})
#: Event types whose `data.object` *is* the subscription itself, as opposed to an
#: invoice that merely references one via its own `subscription` field.
SUBSCRIPTION_EVENT_TYPE_PREFIX = "customer.subscription."
INVOICE_EVENT_TYPE_PREFIX = "invoice."


def _stripe_recurring_interval(billing_interval: str) -> Literal["month", "year"]:
    """Map our internal cadence onto Stripe's `Price.recurring.interval`.

    Stripe only accepts `"day"`, `"week"`, `"month"`, or `"year"` — there is no
    concept of an explicit "every N months" annual plan the way MercadoPago's
    `frequency`/`frequency_type` pair allows, so annual maps directly onto
    `"year"` rather than 12 months.
    """
    if billing_interval == BillingInterval.ANNUAL:
        return "year"
    return "month"


def _expandable_id(value: "str | stripe.Product") -> str:
    """Stripe's expandable relations (e.g. `Price.product`) are typed as either
    the bare id string or the expanded resource, depending on whether the
    original call requested expansion. Neither of this adapter's `Price`
    retrievals asks for it, but narrowing explicitly here is cheaper than
    threading an `expand=` list through every call site that only ever wants
    the id.
    """
    return value if isinstance(value, str) else value.id


def _billing_profile_from_payment_intent_payload(payment_payload: dict) -> BillingProfile:
    """Best-effort `BillingProfile` reconstruction from a Stripe `PaymentIntent`.

    `PaymentService.receive_subscription_payment_update` never actually reads
    `SubscriptionPayment.billing_profile` — it sources the persisted payment's
    billing profile from the subscription's own organization instead — so this
    exists purely to satisfy the dataclass's shape, not because anything
    downstream depends on its accuracy.

    `PaymentIntent.charges` (the field this used to reconstruct billing details
    from) was removed from the API; the replacement, `latest_charge`, is an
    expandable id/`Charge` union that `get_payment_payload` never asks to be
    expanded purely to populate a value nothing downstream reads. Rather than
    add an expand solely for that, this returns an explicitly empty profile.
    """
    return BillingProfile(
        pk=None,
        first_name=None,
        last_name=None,
        email=None,
        phone=None,
        document_type=None,
        document_number=None,
        billing_address=BillingAddress(
            id=None,
            street_name="",
            street_number="",
            neighborhood=None,
            address_line_2="",
            city="",
            state="",
            country="",
            zip_code="",
        ),
    )


class StripeSubscriptionAdapter(BaseSubscriptionAdapter):
    provider = PaymentProviders.STRIPE
    #: Stripe's `Stripe-Signature` header signs `{timestamp}.{raw_body}` — the
    #: entire body — unlike MercadoPago's narrower manifest. See
    #: `payments.services.stripe_signature.verify_stripe_event`.
    verifies_full_body = True

    def __init__(self, api_key: str, webhook_secret: str = ""):
        self.api_key = api_key
        self.webhook_secret = webhook_secret

    def create_subscription_plan(self, plan: Plan) -> str:
        """
        Stripe has no single "plan" resource for a new integration to target (the
        legacy `Plan` API is deprecated in favor of `Product` + `Price`) — a
        recurring price always has to be created against a product. The
        `Product` is created here alongside the `Price` rather than assumed to
        pre-exist, since nothing upstream of this adapter has a Stripe product id
        to pass in.
        """
        product = stripe.Product.create(
            name=plan.name,
            metadata={"plan_id": str(plan.id)},
            api_key=self.api_key,
        )
        price = stripe.Price.create(
            product=product.id,
            unit_amount=to_stripe_amount(plan.value, plan.currency),
            currency=plan.currency.lower(),
            recurring={"interval": _stripe_recurring_interval(plan.billing_interval)},
            api_key=self.api_key,
        )
        return price.id

    def update_subscription_plan(self, external_id: str, plan: Plan) -> str:
        """
        Stripe `Price` objects are immutable once created (amount, currency, and
        recurring cadence can never change) — MercadoPago's "update this plan in
        place, same id" has no Stripe equivalent. The idiomatic replacement is to
        archive the old price and mint a new one against the same product,
        returning the *new* external id — callers must persist it, which is why
        this (and `update_plan`, below) both return the id rather than assuming
        it's unchanged.
        """
        old_price = stripe.Price.retrieve(external_id, api_key=self.api_key)
        stripe.Price.modify(external_id, active=False, api_key=self.api_key)
        new_price = stripe.Price.create(
            product=_expandable_id(old_price.product),
            unit_amount=to_stripe_amount(plan.value, plan.currency),
            currency=plan.currency.lower(),
            recurring={"interval": _stripe_recurring_interval(plan.billing_interval)},
            api_key=self.api_key,
        )
        return new_price.id

    def create_subscription(
        self, subscription: Subscription, payment_token: str, idempotency_key: str = ""
    ) -> str:
        """
        `payment_token` is a Stripe `PaymentMethod` id — the closest Stripe
        equivalent to MercadoPago's `card_token_id`.

        `idempotency_key`, when set, guards the money-moving `Subscription.create`
        so a retried first-upgrade does not create a second subscription (and a
        second charge). It is *not* reused for `Customer.create`: Stripe scopes an
        idempotency key to identical request parameters, so reusing one key across
        two different calls would make the second error — and a duplicate Customer
        (unlike a duplicate Subscription) moves no money.
        """
        billing_profile = subscription.billing_profile
        full_name = " ".join(
            part for part in (billing_profile.first_name, billing_profile.last_name) if part
        )
        customer_params: dict = {
            "payment_method": payment_token,
            "invoice_settings": {"default_payment_method": payment_token},
            "metadata": {"subscription_id": str(subscription.id)},
            "api_key": self.api_key,
        }
        # `email`/`name` are optional keys, not `Optional[str]` values, in
        # Stripe's typed params — only included when actually present.
        if billing_profile.email:
            customer_params["email"] = billing_profile.email
        if full_name:
            customer_params["name"] = full_name
        customer = stripe.Customer.create(**customer_params)
        subscription_params: dict = {
            "customer": customer.id,
            "items": [{"price": subscription.plan.external_id}],
            "default_payment_method": payment_token,
            "metadata": {"subscription_id": str(subscription.id)},
            "api_key": self.api_key,
        }
        if idempotency_key:
            subscription_params["idempotency_key"] = idempotency_key
        stripe_subscription = stripe.Subscription.create(**subscription_params)
        return stripe_subscription.id

    def cancel_subscription(self, subscription: Subscription) -> None:
        if not subscription.external_id:
            raise PaymentAdapterError(
                f"Cannot cancel subscription {subscription.id} with no external_id"
            )
        stripe.Subscription.cancel(subscription.external_id, api_key=self.api_key)

    def update_plan(self, plan: CreatedPlan) -> CreatedPlan:
        new_external_id = self.update_subscription_plan(plan.external_id, plan)
        return replace(plan, external_id=new_external_id)

    def change_subscription_plan(
        self, subscription: Subscription, new_plan: CreatedPlan, idempotency_key: str = ""
    ) -> None:
        """
        Stripe subscriptions are moved onto a new price by modifying the
        subscription's existing line item (a subscription always has exactly one
        here — this adapter creates it with a single ``items=[{"price": ...}]``
        in ``create_subscription``) rather than by re-creating the subscription.
        ``proration_behavior="always_invoice"`` makes Stripe compute the prorated
        amount server-side *and* invoice + attempt to charge it immediately
        against the subscription's default payment method, rather than only
        crediting/debiting the next regular invoice — matching "pay now" for an
        upgrade a user just requested.

        `idempotency_key`, when set, guards the money-moving `Subscription.modify`
        (which invoices the proration immediately) so a retried drive prorates at
        most once. The read-only `Subscription.retrieve` above does not need it.
        """
        if not subscription.external_id:
            raise PaymentAdapterError(
                f"Cannot change plan for subscription {subscription.id} with no external_id"
            )
        stripe_subscription = stripe.Subscription.retrieve(
            subscription.external_id, api_key=self.api_key
        )
        item_id = stripe_subscription["items"]["data"][0]["id"]
        modify_params: dict = {
            "items": [{"id": item_id, "price": new_plan.external_id}],
            "proration_behavior": "always_invoice",
            "api_key": self.api_key,
        }
        if idempotency_key:
            modify_params["idempotency_key"] = idempotency_key
        stripe.Subscription.modify(subscription.external_id, **modify_params)

    def update_subscription_payment_token(
        self, subscription: Subscription, payment_token: str
    ) -> None:
        if not subscription.external_id:
            raise PaymentAdapterError(
                f"Cannot update payment token for subscription {subscription.id} "
                "with no external_id"
            )
        stripe.Subscription.modify(
            subscription.external_id,
            default_payment_method=payment_token,
            api_key=self.api_key,
        )

    def get_subscription_external_id_from_update(self, update_payload: dict) -> str | None:
        """
        Unlike MercadoPago's fixed `data.id` path, the subscription id's location
        in a Stripe webhook payload depends on the event type: a
        `customer.subscription.*` event's `data.object` *is* the subscription
        (so its own `id` is what we want), while an `invoice.*` event's
        `data.object` is an invoice that only *references* its subscription.

        As of the pinned `2026-06-24.dahlia` API version, `Invoice.subscription`
        no longer exists — the id lives at `parent.subscription_details.subscription`
        (`Invoice.parent` is only populated for invoices that came from a
        subscription; `type` is `"subscription_details"` in that case). The bare
        `subscription` field is still read as a fallback for any pre-dahlia
        payload this might ever see.
        """
        event_type = update_payload.get("type", "")
        obj = update_payload.get("data", {}).get("object", {})
        if event_type.startswith(SUBSCRIPTION_EVENT_TYPE_PREFIX):
            return obj.get("id")
        if event_type.startswith(INVOICE_EVENT_TYPE_PREFIX):
            subscription_details = (obj.get("parent") or {}).get("subscription_details") or {}
            return subscription_details.get("subscription") or obj.get("subscription")
        return None

    def get_update_id(self, update_payload: dict) -> str | None:
        return update_payload.get("id")

    def get_payment_payload(self, payment_external_id: str) -> dict:
        intent = stripe.PaymentIntent.retrieve(payment_external_id, api_key=self.api_key)
        return intent.to_dict()

    def create_subscription_payment_from_payment_payload(
        self, subscription_external_id: str, payment_payload: dict
    ) -> SubscriptionPayment:
        currency = (payment_payload.get("currency") or "").upper()
        amount = Decimal(payment_payload.get("amount", 0))
        value = amount if currency.lower() in ZERO_DECIMAL_CURRENCIES else amount / Decimal(100)
        return SubscriptionPayment(
            id=None,
            subscription_external_id=subscription_external_id,
            external_id=payment_payload.get("id", ""),
            value=value,
            currency=currency,
            payment_provider=PaymentProviders.STRIPE,
            status=payment_payload.get("status", ""),
            billing_profile=_billing_profile_from_payment_intent_payload(payment_payload),
            payment_method=(payment_payload.get("payment_method_types") or [""])[0],
            description=payment_payload.get("description") or "",
            status_updates=[],
        )

    def create_status_update_from_payment_payload(
        self, payment_payload: dict
    ) -> PaymentStatusUpdate:
        original_status = payment_payload.get("status", "")
        mapped_status = PAYMENT_INTENT_STATUS_MAPPING.get(original_status, PaymentStatuses.UNKNOWN)
        if mapped_status == PaymentStatuses.UNKNOWN:
            logger.error(
                "Unknown subscription payment status: payment_external_id=%s original_status=%s",
                payment_payload.get("id"),
                original_status,
            )
        last_payment_error = payment_payload.get("last_payment_error")
        description = last_payment_error.get("message") if last_payment_error else original_status
        return PaymentStatusUpdate(
            id=None,
            status=mapped_status,
            description=description,
            update_external_id=payment_payload.get("id"),
        )

    def is_payment_update(self, update_payload: dict) -> bool:
        return update_payload.get("type") in RELEVANT_SUBSCRIPTION_PAYMENT_EVENT_TYPES

    def get_subscription_payload(self, subscription_external_id: str) -> dict:
        """
        `Invoice.payment_intent` no longer exists as of the pinned
        `2026-06-24.dahlia` API version — expanding it raises
        `invalid_request_error`. The PaymentIntent id is reached instead via
        `Invoice.payments` (a list of `InvoicePayment`s, itself only populated
        when expanded) -> `InvoicePayment.payment.payment_intent`. Only the id
        is needed (see `get_payment_external_id_from_subscription_payload`), so
        the payment_intent sub-field itself is left unexpanded.
        """
        subscription = stripe.Subscription.retrieve(
            subscription_external_id,
            expand=["latest_invoice.payments"],
            api_key=self.api_key,
        )
        return subscription.to_dict()

    def get_payment_external_id_from_subscription_payload(
        self, subscription_payload: dict
    ) -> str | None:
        latest_invoice = subscription_payload.get("latest_invoice")
        if not isinstance(latest_invoice, dict):
            return None
        payments = latest_invoice.get("payments") or {}
        payment_entries = payments.get("data") or [] if isinstance(payments, dict) else []
        if not payment_entries:
            return None
        payment = payment_entries[0].get("payment") or {}
        payment_intent = payment.get("payment_intent")
        if isinstance(payment_intent, dict):
            return payment_intent.get("id")
        return payment_intent

    def verify_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        return verify_stripe_event(raw_body, headers, self.webhook_secret) is not None

    def get_event_id(self, raw_body: bytes, headers: Mapping[str, str], payload: dict) -> str:
        """See `StripePaymentAdapter.get_event_id` — same reasoning, same source
        of truth (a fresh, independently re-verified `construct_event` call, not
        `payload`)."""
        event = verify_stripe_event(raw_body, headers, self.webhook_secret)
        if event is None:
            raise ProviderWebhookEventIdMissingError
        return event.id
