import json
from abc import abstractmethod
from venv import logger

from payments.exceptions import PaymentExternalIdMissingInNotificationError
from payments.services.dataclasses import (
    Payment,
    PaymentStatusUpdate,
    Refund,
)


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
        update_id = self.get_update_id(update_payload)
        latest_status_update = self.check_status(payment_external_id, update_id)
        if not latest_status_update:
            return None

        return payment_external_id, latest_status_update
