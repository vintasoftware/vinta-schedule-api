"""Shared MercadoPago webhook signature verification.

Both ``MercadoPagoPaymentAdapter`` and ``MercadoPagoSubscriptionAdapter`` receive
webhooks through the same notification mechanism, so the HMAC check lives once
here instead of being duplicated in both adapters.
"""

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass

from django.conf import settings


logger = logging.getLogger(__name__)

#: Stripe's convention (reused here for the same setting) — how far a webhook's
#: signed ``ts`` may drift from "now" before it is rejected as stale. Without this,
#: a single captured (signature, body) pair verifies forever, turning one leaked
#: webhook delivery into a permanent forgery capability.
DEFAULT_WEBHOOK_SIGNATURE_TOLERANCE_SECONDS = 300


@dataclass(frozen=True)
class MercadoPagoSignatureManifest:
    """The signed components extracted from a verified MercadoPago webhook request.

    These are the *only* fields MercadoPago's HMAC actually covers. The
    notification payload's top-level ``id`` is not part of the signed manifest and
    must never be trusted as an idempotency key or persisted as an external id —
    an attacker who captures one valid ``(x-signature, x-request-id)`` pair can
    keep ``data.id`` fixed (so the HMAC still verifies) and vary the top-level
    ``id`` freely across replays.
    """

    data_id: str
    request_id: str
    ts: str

    @property
    def event_id(self) -> str:
        """Stable idempotency ledger key derived entirely from signed material."""
        return f"{self.data_id}:{self.request_id}:{self.ts}"


def verify_mercadopago_signature(
    raw_body: bytes, headers: Mapping[str, str], webhook_secret: str
) -> MercadoPagoSignatureManifest | None:
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

    A signature whose ``ts`` is older than ``WEBHOOK_SIGNATURE_TOLERANCE_SECONDS``
    (default 300s, Stripe's convention) is rejected as stale — otherwise a single
    captured valid ``(x-signature, x-request-id)`` pair would verify forever.

    :param raw_body: The raw, unparsed HTTP request body.
    :param headers: The HTTP request headers (case-insensitive lookup).
    :param webhook_secret: The ``MERCADOPAGO_WEBHOOK_SECRET`` configured for this environment.
    :return: The verified manifest components, or ``None`` if the signature is
        missing, malformed, forged, or stale.
    """
    if not webhook_secret:
        logger.error("MERCADOPAGO_WEBHOOK_SECRET is not configured; rejecting webhook")
        return None

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
        return None

    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        logger.warning("MercadoPago webhook body is not valid JSON; cannot verify signature")
        return None

    data_id = body.get("data", {}).get("id") if isinstance(body, dict) else None
    if not data_id:
        logger.warning("MercadoPago webhook payload missing data.id; cannot verify signature")
        return None

    # MercadoPago's documented manifest template omits segments whose source is
    # absent (e.g. no `x-request-id` header) rather than emitting them empty — build
    # it the same way, or a legitimate webhook delivered without that header would
    # silently 403 against a manifest we computed differently than MercadoPago did.
    manifest = f"id:{str(data_id).lower()};"
    if request_id:
        manifest += f"request-id:{request_id};"
    manifest += f"ts:{ts};"

    expected_signature = hmac.new(
        webhook_secret.encode(), manifest.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_signature, v1):
        logger.warning("MercadoPago webhook signature mismatch")
        return None

    try:
        ts_seconds = int(ts)
    except ValueError:
        logger.warning("MercadoPago webhook x-signature ts is not a valid integer: %r", ts)
        return None

    tolerance_seconds = getattr(
        settings,
        "WEBHOOK_SIGNATURE_TOLERANCE_SECONDS",
        DEFAULT_WEBHOOK_SIGNATURE_TOLERANCE_SECONDS,
    )
    if abs(time.time() - ts_seconds) > tolerance_seconds:
        logger.warning("MercadoPago webhook signature timestamp is stale: ts=%s", ts)
        return None

    return MercadoPagoSignatureManifest(data_id=str(data_id), request_id=request_id, ts=ts)
