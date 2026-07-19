import json
import logging
from abc import abstractmethod
from collections.abc import Mapping

from payments.exceptions import ProviderWebhookEventIdMissingError
from payments.services.dataclasses import (
    Payment,
    PaymentStatusUpdate,
    Refund,
    RefundResult,
)


logger = logging.getLogger(__name__)


class BasePaymentAdapter:
    provider: str

    #: Whether ``verify_signature`` authenticates the literal request body bytes
    #: end to end (e.g. Stripe's ``Stripe-Signature`` covers
    #: ``{timestamp}.{raw_body}`` in full) or only a narrower manifest carved out
    #: of specific fields (e.g. MercadoPago's ``x-signature`` covers only
    #: ``data.id`` + ``x-request-id`` + ``ts``, never the body as a whole). This is
    #: load-bearing, not decorative: a passing ``verify_signature`` does **not** by
    #: itself imply every field in the parsed payload is trustworthy — that only
    #: holds when this is ``True``. Every concrete adapter must set this
    #: explicitly rather than lean on a base default, so the answer is never left
    #: to tribal knowledge about a specific provider's signing scheme.
    verifies_full_body: bool

    class Meta:
        abstract = True

    @abstractmethod
    def process(self, payment: Payment, payment_token: str) -> str:
        """
        Process a payment using the payment token.
        :param payment: Payment object
        :param payment_token: Payment token
        :return: External ID of the payment
        """
        raise NotImplementedError

    @abstractmethod
    def check_status(
        self, payment_external_id: str, update_id: str | None = None
    ) -> PaymentStatusUpdate:
        """
        Check the status of a payment.
        :param payment: Payment object
        """
        raise NotImplementedError

    @abstractmethod
    def refund(self, refund: Refund) -> RefundResult:
        """
        Request a refund for a payment.

        :param refund: Refund object. ``refund.external_id`` is not yet set at
            this point — assigning it is what this call does.
        :return: A ``RefundResult`` carrying both the provider's external id and
            the refund's initial status. MercadoPago and Stripe both return the
            status synchronously in the same response body that carries the id,
            so callers get an accurate initial status without a forced second
            round trip through ``check_refund_status``.
        """
        raise NotImplementedError

    @abstractmethod
    def get_payment_external_id_from_update(self, update_payload: dict) -> str | None:
        """
        Get the external ID from a payment status update payload.
        :param update_payload: Payment status update payload
        :return: External ID
        """
        raise NotImplementedError

    @abstractmethod
    def get_update_id(self, update_payload: dict) -> str | None:
        """
        Get the external ID from a payment status update payload.
        :param update_payload: Payment status update payload
        :return: External ID
        """
        raise NotImplementedError

    @abstractmethod
    def check_refund_status(self, refund: Refund) -> str:
        """
        Poll the provider for a refund's current status — for later
        reconciliation (an async pending -> succeeded/failed transition, or a
        scheduled cycle-close sweep), not the only way to learn a refund's
        initial status (see ``refund``).

        :param refund: Refund object with ``external_id`` set (from a prior
            ``refund()`` call) and ``payment.external_id`` populated.
            MercadoPago has no single-refund-by-id lookup — its status must be
            read off the list of refunds for the parent payment — so both ids
            are required to support every provider without a fragile
            provider-specific side channel.
        :return: The status of the refund.
        """
        raise NotImplementedError

    @abstractmethod
    def verify_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        """
        Verify that an inbound webhook request actually came from the provider.

        Must be checked against ``raw_body`` — the literal bytes the provider sent —
        never against a re-serialization of an already-parsed payload. A payload
        that has been decoded and re-encoded is not guaranteed to reproduce the
        exact bytes the provider signed, and re-hashing it instead of the wire
        bytes can make a forged request pass this check.

        :param raw_body: The raw, unparsed HTTP request body.
        :param headers: The HTTP request headers.
        :return: True if the signature is valid, False otherwise.
        """
        raise NotImplementedError

    def get_event_id(self, raw_body: bytes, headers: Mapping[str, str], payload: dict) -> str:
        """
        Stable identifier for this specific webhook delivery, used as the
        idempotency ledger key so a provider redelivery of the same event is only
        ever processed once.

        Defaults to the provider's own top-level notification id (``get_update_id``),
        sourced from ``payload`` — which is only safe to trust when
        ``verifies_full_body`` is ``True`` (the provider's signature covers the
        entire request body, ``payload`` included). A provider whose signature
        only covers a narrow manifest (``verifies_full_body = False``) must
        override this to re-derive the key from signed material instead (see
        ``MercadoPagoPaymentAdapter.get_event_id``) — inheriting this default
        would let an attacker replay a single captured valid signature under an
        unbounded number of distinct "new" event ids. This is enforced here
        rather than left to convention: ``verifies_full_body`` would otherwise be
        purely documentary, and a future narrow-manifest adapter that forgets to
        override ``get_event_id`` would get a false green light instead of a
        failure. ``raw_body``/``headers`` are passed through so an override can
        re-derive the key from signed material.
        """
        if not self.verifies_full_body:
            raise NotImplementedError(
                f"{type(self).__name__} must override get_event_id: "
                "verifies_full_body is False, so this default get_update_id(payload)"
                "-based implementation cannot be trusted as an idempotency ledger key."
            )
        event_id = self.get_update_id(payload)
        if not event_id:
            raise ProviderWebhookEventIdMissingError
        return str(event_id)

    def receive_update(self, update_payload: dict) -> tuple[str, PaymentStatusUpdate] | None:
        """
        Receive a payment status update. This method is supposed to be called by a webhook.
        :param update_payload: Payment status update payload
        :return: Payment external ID and PaymentStatusUpdate object

        Sources the payment id from ``get_payment_external_id_from_update`` — the
        per-adapter hook, not a hardcoded ``payload["data"]["id"]`` lookup. That
        hardcoded shape only ever matched MercadoPago's notifications; Stripe's
        equivalent id lives at ``payload["data"]["object"]["id"]``. Each adapter's
        own ``get_payment_external_id_from_update`` already knows its provider's
        shape, so this template method defers to it instead of assuming one.
        """
        payment_external_id = self.get_payment_external_id_from_update(update_payload)
        if not payment_external_id:
            logger.error(
                "Payment external id not found in update payload. payload: %s",
                json.dumps(update_payload),
            )
            return None
        # `update_id` used to be sourced from the unsigned notification payload here
        # and persisted downstream as `PaymentStatusUpdateModel.external_id` — but
        # MercadoPago's signature never covers that field, so an attacker replaying
        # one valid signature could stamp an arbitrary external id. `check_status`
        # now sources it from the authenticated API response instead.
        latest_status_update = self.check_status(payment_external_id)
        if not latest_status_update:
            return None

        return payment_external_id, latest_status_update
