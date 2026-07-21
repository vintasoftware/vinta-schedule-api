import logging
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal

import stripe

from payments.constants import PaymentProviders, PaymentStatuses, RefundStatuses
from payments.exceptions import ProviderWebhookEventIdMissingError
from payments.services.dataclasses import Payment, PaymentStatusUpdate, Refund, RefundResult
from payments.services.payment_adapters.base import BasePaymentAdapter
from payments.services.stripe_signature import verify_stripe_event


logger = logging.getLogger(__name__)


#: Stripe currencies with no minor unit — amounts for these are passed as whole
#: units, never multiplied by 100. https://docs.stripe.com/currencies#zero-decimal
ZERO_DECIMAL_CURRENCIES: frozenset[str] = frozenset(
    {
        "bif",
        "clp",
        "djf",
        "gnf",
        "jpy",
        "kmf",
        "krw",
        "mga",
        "pyg",
        "rwf",
        "ugx",
        "vnd",
        "vuv",
        "xaf",
        "xof",
        "xpf",
    }
)

# https://docs.stripe.com/api/payment_intents/object#payment_intent_object-status
PAYMENT_INTENT_STATUS_MAPPING: dict[str, str] = {
    "requires_payment_method": PaymentStatuses.PENDING,
    "requires_confirmation": PaymentStatuses.PENDING,
    "requires_action": PaymentStatuses.PENDING,
    "processing": PaymentStatuses.IN_PROCESS,
    "requires_capture": PaymentStatuses.PENDING,
    "canceled": PaymentStatuses.CANCELLED,
    "succeeded": PaymentStatuses.APPROVED,
}
# https://docs.stripe.com/api/refunds/object#refund_object-status
REFUND_STATUS_MAPPING: dict[str, str] = {
    "pending": RefundStatuses.PENDING,
    "requires_action": RefundStatuses.PENDING,
    "succeeded": RefundStatuses.APPROVED,
    "failed": RefundStatuses.FAILED,
    "canceled": RefundStatuses.REJECTED,
}
#: Stripe webhook event types this adapter's `receive_update` acts on — mirrors
#: `MercadoPagoPaymentAdapter.receive_update`'s `type`/`action` filter.
RELEVANT_PAYMENT_EVENT_TYPES = frozenset(
    {
        "payment_intent.succeeded",
        "payment_intent.payment_failed",
        "payment_intent.canceled",
        "payment_intent.processing",
    }
)


def to_stripe_amount(value: Decimal, currency: str) -> int:
    """Convert a decimal amount into Stripe's smallest-currency-unit integer."""
    if currency.lower() in ZERO_DECIMAL_CURRENCIES:
        return int(value.to_integral_value(rounding=ROUND_HALF_UP))
    return int((value * 100).to_integral_value(rounding=ROUND_HALF_UP))


class StripePaymentAdapter(BasePaymentAdapter):
    provider = PaymentProviders.STRIPE
    #: Stripe's `Stripe-Signature` header signs `{timestamp}.{raw_body}` — the
    #: entire body — unlike MercadoPago's narrower manifest. See
    #: `payments.services.stripe_signature.verify_stripe_event`.
    verifies_full_body = True

    def __init__(self, api_key: str, webhook_secret: str = ""):
        self.api_key = api_key
        self.webhook_secret = webhook_secret

    def process(self, payment: Payment, payment_token: str, idempotency_key: str = "") -> str:
        """
        `payment_token` is a Stripe `PaymentMethod` id (e.g. from Stripe.js /
        Stripe Elements on the client) — the closest Stripe equivalent to
        MercadoPago's card token, both being an opaque, single-use-until-attached
        reference to payment credentials the server never sees directly.

        `idempotency_key`, when set, is passed as Stripe's `Idempotency-Key` so a
        retried charge (e.g. after the local transaction that created the dedup
        row rolled back) resolves to the *same* PaymentIntent instead of creating
        a second one. See `BasePaymentAdapter.process`.
        """
        params: dict = {
            "amount": to_stripe_amount(payment.value, payment.currency),
            "currency": payment.currency.lower(),
            "payment_method": payment_token,
            "confirm": True,
            "description": payment.description,
            "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
            "metadata": {"payment_id": str(payment.id)},
            "api_key": self.api_key,
        }
        # `receipt_email` is an optional key, not an `Optional[str]` value, in
        # Stripe's typed params — passing `None` explicitly is a type error even
        # though the field is genuinely optional, so it's only included when set.
        if payment.billing_profile.email:
            params["receipt_email"] = payment.billing_profile.email
        if idempotency_key:
            params["idempotency_key"] = idempotency_key
        intent = stripe.PaymentIntent.create(**params)
        return intent.id

    def check_status(
        self, payment_external_id: str, update_id: str | None = None
    ) -> PaymentStatusUpdate:
        intent = stripe.PaymentIntent.retrieve(payment_external_id, api_key=self.api_key)
        original_status = intent.status
        mapped_status = PAYMENT_INTENT_STATUS_MAPPING.get(original_status, PaymentStatuses.UNKNOWN)
        if mapped_status == PaymentStatuses.UNKNOWN:
            logger.error(
                "Unknown payment status: payment_external_id=%s original_status=%s",
                payment_external_id,
                original_status,
            )
        last_payment_error = getattr(intent, "last_payment_error", None)
        description = last_payment_error.get("message") if last_payment_error else original_status
        return PaymentStatusUpdate(
            id=None,
            status=mapped_status,
            description=description,
            # Sourced from the authenticated API response, not any caller-supplied
            # `update_id` — matches `MercadoPagoPaymentAdapter.check_status`'s
            # discipline, even though Stripe's own signature (unlike
            # MercadoPago's) would actually cover a webhook-supplied id too. Kept
            # symmetric across adapters so nothing downstream has to special-case
            # which provider it's talking to.
            update_external_id=intent.id,
        )

    def refund(self, refund: Refund) -> RefundResult:
        stripe_refund = stripe.Refund.create(
            payment_intent=refund.payment.external_id,
            amount=to_stripe_amount(refund.value, refund.currency),
            api_key=self.api_key,
        )
        mapped_status = REFUND_STATUS_MAPPING.get(
            stripe_refund.status or "", RefundStatuses.UNKNOWN
        )
        if mapped_status == RefundStatuses.UNKNOWN:
            logger.error(
                "Unknown refund status: refund_id=%s original_status=%s",
                refund.id,
                stripe_refund.status,
            )
        return RefundResult(external_id=stripe_refund.id, status=mapped_status)

    def check_refund_status(self, refund: Refund) -> str:
        """
        Unlike MercadoPago, Stripe does have a single-refund-by-id endpoint, so
        `refund.payment.external_id` isn't actually needed here — but the
        parameter stays a full `Refund` (rather than a bare id) to keep one
        signature that both adapters can satisfy without a provider-specific
        carve-out (see `BasePaymentAdapter.check_refund_status`).
        """
        if not refund.external_id:
            logger.error(
                "Cannot check refund status without an external_id: refund_id=%s", refund.id
            )
            return RefundStatuses.UNKNOWN

        stripe_refund = stripe.Refund.retrieve(refund.external_id, api_key=self.api_key)
        mapped_status = REFUND_STATUS_MAPPING.get(
            stripe_refund.status or "", RefundStatuses.UNKNOWN
        )
        if mapped_status == RefundStatuses.UNKNOWN:
            logger.error(
                "Unknown refund status: refund_external_id=%s original_status=%s",
                refund.external_id,
                stripe_refund.status,
            )
        return mapped_status

    def get_payment_external_id_from_update(self, update_payload: dict) -> str | None:
        return update_payload.get("data", {}).get("object", {}).get("id")

    def get_update_id(self, update_payload: dict) -> str | None:
        return update_payload.get("id")

    def receive_update(self, update_payload: dict) -> tuple[str, PaymentStatusUpdate] | None:
        if update_payload.get("type") not in RELEVANT_PAYMENT_EVENT_TYPES:
            return None
        return super().receive_update(update_payload)

    def verify_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        return verify_stripe_event(raw_body, headers, self.webhook_secret) is not None

    def get_event_id(self, raw_body: bytes, headers: Mapping[str, str], payload: dict) -> str:
        """
        Stripe's `Stripe-Signature` authenticates the whole body, unlike
        MercadoPago's narrow manifest — but the ledger key is still derived from
        a *fresh, independent* `construct_event` call here, not from `payload`
        (the caller's already-parsed copy of the same bytes). See
        `stripe_signature.verify_stripe_event`'s docstring for why: relying on
        two independently-parsed copies of one signed byte string to agree is an
        assumption, not a guarantee, and `event.id` is cheap to recompute.
        """
        event = verify_stripe_event(raw_body, headers, self.webhook_secret)
        if event is None:
            raise ProviderWebhookEventIdMissingError
        return event.id
