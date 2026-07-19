import json
import logging
from abc import abstractmethod
from collections.abc import Mapping

from payments.exceptions import (
    PaymentExternalIdMissingInNotificationError,
    ProviderWebhookEventIdMissingError,
)
from payments.services.dataclasses import (
    CreatedPlan,
    PaymentStatusUpdate,
    Plan,
    Subscription,
    SubscriptionPayment,
)


logger = logging.getLogger(__name__)


class BaseSubscriptionAdapter:
    provider: str

    class Meta:
        abstract = True

    @abstractmethod
    def create_subscription_plan(self, plan: Plan) -> str:
        raise NotImplementedError

    @abstractmethod
    def update_subscription_plan(self, external_id: str, plan: Plan) -> str:
        raise NotImplementedError

    @abstractmethod
    def create_subscription(self, subscription: Subscription, payment_token: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def cancel_subscription(self, subscription: Subscription) -> None:
        raise NotImplementedError

    @abstractmethod
    def update_plan(self, plan: CreatedPlan) -> CreatedPlan:
        raise NotImplementedError

    @abstractmethod
    def get_subscription_external_id_from_update(self, update_payload: dict) -> str | None:
        """
        Get the external ID from a payment status update payload.
        :param update_payload: Payment status update payload
        :return: External ID
        """
        raise NotImplementedError

    def _get_required_subscription_external_id_from_update(self, update_payload: dict) -> str:
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
    def get_payment_payload(self, payment_external_id: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    def create_subscription_payment_from_payment_payload(
        self, subscription_external_id: str, payment_payload: dict
    ) -> SubscriptionPayment:
        raise NotImplementedError

    @abstractmethod
    def create_status_update_from_payment_payload(
        self, payment_payload: dict
    ) -> PaymentStatusUpdate:
        raise NotImplementedError

    @abstractmethod
    def is_payment_update(self, update_payload: dict) -> bool:
        raise NotImplementedError

    @abstractmethod
    def get_subscription_payload(self, subscription_external_id: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    def get_payment_external_id_from_subscription_payload(
        self, subscription_payload: dict
    ) -> str | None:
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
        ``MercadoPagoSubscriptionAdapter.get_event_id``).
        """
        event_id = self.get_update_id(payload)
        if not event_id:
            raise ProviderWebhookEventIdMissingError
        return str(event_id)

    def receive_payment_update(
        self, update_payload: dict
    ) -> tuple[SubscriptionPayment, PaymentStatusUpdate] | None:
        if not self.is_payment_update(update_payload):
            return None

        try:
            subscription_external_id = self._get_required_subscription_external_id_from_update(
                update_payload
            )
        except PaymentExternalIdMissingInNotificationError:
            logger.error(
                "Payment external id not found in update payload. payload: %s",
                json.dumps(update_payload),
            )
            return None

        subscription_payload = self.get_subscription_payload(subscription_external_id)
        payment_external_id = self.get_payment_external_id_from_subscription_payload(
            subscription_payload
        )
        if not payment_external_id:
            return None

        payment_payload = self.get_payment_payload(payment_external_id)
        return (
            self.create_subscription_payment_from_payment_payload(
                subscription_external_id, payment_payload
            ),
            self.create_status_update_from_payment_payload(payment_payload),
        )
