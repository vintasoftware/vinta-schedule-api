"""Shared Stripe webhook signature verification.

Both ``StripePaymentAdapter`` and ``StripeSubscriptionAdapter`` receive webhooks
through the same ``Stripe-Signature`` mechanism, so the check — and the one
verified ``stripe.Event`` object it produces — lives once here instead of being
duplicated in both adapters.
"""

import logging
from collections.abc import Mapping

from django.conf import settings

import stripe

from payments.services.mercadopago_signature import DEFAULT_WEBHOOK_SIGNATURE_TOLERANCE_SECONDS


logger = logging.getLogger(__name__)


def verify_stripe_event(
    raw_body: bytes, headers: Mapping[str, str], webhook_secret: str
) -> "stripe.Event | None":
    """Verify an inbound Stripe webhook request and return its authenticated event.

    Unlike MercadoPago's HMAC (which covers only a narrow manifest carved out of
    the request — see ``mercadopago_signature.py``), Stripe's ``Stripe-Signature``
    header signs ``{timestamp}.{raw_body}`` in full: the *entire* body is
    authenticated, not just a handful of fields. ``stripe.Webhook.construct_event``
    verifies that signature and parses the body into a trustworthy ``Event``
    object in one atomic step.

    Callers must build the idempotency ledger key (and read any other
    signature-sensitive field) off the ``Event`` object this function returns —
    never off an independently-parsed copy of the same bytes (e.g. DRF's
    ``request.data``). Even though the raw bytes are signed, a second, separately
    parsed copy is a different code path than the one actually verified here;
    relying on "the two parses should agree" is an assumption, not a guarantee,
    and it can silently stop holding (a different parser, a future DRF version, a
    proxy that rewrites the body) without anything failing loudly. The verified
    ``Event`` is the only value this codebase treats as ground truth for a Stripe
    webhook delivery.

    :param raw_body: The raw, unparsed HTTP request body.
    :param headers: The HTTP request headers (case-insensitive lookup).
    :param webhook_secret: The ``STRIPE_WEBHOOK_SECRET`` configured for this environment.
    :return: The verified ``stripe.Event``, or ``None`` if the signature is
        missing, malformed, forged, or stale.
    """
    if not webhook_secret:
        logger.error("STRIPE_WEBHOOK_SECRET is not configured; rejecting webhook")
        return None

    normalized_headers = {k.lower(): v for k, v in headers.items()}
    signature_header = normalized_headers.get("stripe-signature", "")
    if not signature_header:
        logger.warning("Stripe webhook missing Stripe-Signature header")
        return None

    # Stripe's own default tolerance (300s) is also what
    # `mercadopago_signature.py` uses — reuse the single project-wide setting
    # rather than introduce a second tolerance knob for the second provider.
    tolerance_seconds = getattr(
        settings,
        "WEBHOOK_SIGNATURE_TOLERANCE_SECONDS",
        DEFAULT_WEBHOOK_SIGNATURE_TOLERANCE_SECONDS,
    )

    try:
        return stripe.Webhook.construct_event(
            payload=raw_body,
            sig_header=signature_header,
            secret=webhook_secret,
            tolerance=tolerance_seconds,
        )
    except stripe.SignatureVerificationError:
        logger.warning("Stripe webhook signature verification failed")
        return None
    except ValueError:
        logger.warning("Stripe webhook body is not valid JSON; cannot verify signature")
        return None
