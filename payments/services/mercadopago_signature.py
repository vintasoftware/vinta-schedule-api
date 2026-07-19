"""Shared MercadoPago webhook signature verification.

Both ``MercadoPagoPaymentAdapter`` and ``MercadoPagoSubscriptionAdapter`` receive
webhooks through the same notification mechanism, so the HMAC check lives once
here instead of being duplicated in both adapters.
"""

import hashlib
import hmac
import json
import logging
from collections.abc import Mapping


logger = logging.getLogger(__name__)


def verify_mercadopago_signature(
    raw_body: bytes, headers: Mapping[str, str], webhook_secret: str
) -> bool:
    """Verify MercadoPago's ``x-signature`` HMAC over an inbound webhook notification.

    MercadoPago signs a manifest built from the notification's ``data.id``, the
    ``x-request-id`` header, and the timestamp embedded in ``x-signature`` itself —
    it does not sign the request body wholesale. This is consistent with the rest
    of this adapter: the body is never trusted for the actual payment/subscription
    state either (``check_status`` / ``get_payment_payload`` always re-fetch that
    from MercadoPago's API by id). The signature's job is narrower but load-bearing:
    it proves the notification (and specifically the ``data.id`` it points at) is
    authentic, so an attacker cannot make us re-check an id of their choosing.

    ``data.id`` is read directly out of ``raw_body`` — the literal bytes MercadoPago
    sent — rather than from an already-parsed-and-reserialized payload, so a
    tampered id is caught even if the tampered body still parses to a
    similar-looking dict.

    :param raw_body: The raw, unparsed HTTP request body.
    :param headers: The HTTP request headers (case-insensitive lookup).
    :param webhook_secret: The ``MERCADOPAGO_WEBHOOK_SECRET`` configured for this environment.
    :return: True if the signature is valid, False otherwise.
    """
    if not webhook_secret:
        logger.error("MERCADOPAGO_WEBHOOK_SECRET is not configured; rejecting webhook")
        return False

    normalized_headers = {k.lower(): v for k, v in headers.items()}
    signature_header = normalized_headers.get("x-signature", "")
    request_id = normalized_headers.get("x-request-id", "")

    ts: str | None = None
    v1: str | None = None
    for part in signature_header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "ts":
            ts = value
        elif key == "v1":
            v1 = value

    if not ts or not v1:
        logger.warning("MercadoPago webhook missing or malformed x-signature header")
        return False

    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        logger.warning("MercadoPago webhook body is not valid JSON; cannot verify signature")
        return False

    data_id = body.get("data", {}).get("id") if isinstance(body, dict) else None
    if not data_id:
        logger.warning("MercadoPago webhook payload missing data.id; cannot verify signature")
        return False

    manifest = f"id:{str(data_id).lower()};request-id:{request_id};ts:{ts};"
    expected_signature = hmac.new(
        webhook_secret.encode(), manifest.encode(), hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected_signature, v1)
