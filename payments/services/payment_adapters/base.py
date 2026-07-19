import json
import logging
from abc import abstractmethod
from collections.abc import Mapping

from payments.exceptions import (
    PaymentExternalIdMissingInNotificationError,
    ProviderWebhookEventIdMissingError,
)
from payments.services.dataclasses import (
    Payment,
    PaymentStatusUpdate,
    Refund,
)


logger = logging.getLogger(__name__)


class BasePaymentAdapter:
    provider: str

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
    def refund(self, refund: Refund) -> str:
        """
        Request a refund for a payment.
        :param payment: Payment object
        :return: The external_id of the refund
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

    def _get_required_payment_external_id_from_update(self, update_payload: dict) -> str:
        payment_external_id = update_payload.get("data", {}).get("id")
        if not payment_external_id:
            raise PaymentExternalIdMissingInNotificationError(
                "Payment external id not found in update payload"
            )
        return payment_external_id

    @abstractmethod
    def get_update_id(self, update_payload: dict) -> str | None:
        """
        Get the external ID from a payment status update payload.
        :param update_payload: Payment status update payload
        :return: External ID
        """
        raise NotImplementedError

    @abstractmethod
    def check_refund_status(self, refund_external_id: str) -> str:
        """
        Check the status of a refund.
        :param refund: External ID of the refund
        :return: The status of the refund
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

        Defaults to the provider's own top-level notification id (``get_update_id``).
        Override when the provider's signature does not cover that id — deriving
        the ledger key from unsigned payload material lets an attacker replay a
        single captured valid signature under an unbounded number of distinct
        "new" event ids. ``raw_body``/``headers`` are passed through specifically
        so an override can re-derive the key from signed material instead (see
        ``MercadoPagoPaymentAdapter.get_event_id``).
        """
        event_id = self.get_update_id(payload)
        if not event_id:
            raise ProviderWebhookEventIdMissingError
        return str(event_id)

    def receive_update(self, update_payload: dict) -> tuple[str, PaymentStatusUpdate] | None:
        """
        Receive a payment status update. This method is supposed to be called by a webhook.
        :param payment: Payment object
        :param update_payload: Payment status update payload
        :return: Payment external ID and PaymentStatusUpdate object
        """
        try:
            payment_external_id = self.get_payment_external_id_from_update(update_payload)
        except PaymentExternalIdMissingInNotificationError:
            logger.error(
                "Payment external id not found in update payload. payload: %s",
                json.dumps(update_payload),
            )
            return None
        payment_external_id = self._get_required_payment_external_id_from_update(update_payload)
        # `update_id` used to be sourced from the unsigned notification payload here
        # and persisted downstream as `PaymentStatusUpdateModel.external_id` — but
        # MercadoPago's signature never covers that field, so an attacker replaying
        # one valid signature could stamp an arbitrary external id. `check_status`
        # now sources it from the authenticated API response instead.
        latest_status_update = self.check_status(payment_external_id)
        if not latest_status_update:
            return None

        return payment_external_id, latest_status_update
