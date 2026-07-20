import json
import logging
from abc import abstractmethod
from collections.abc import Mapping

from payments.exceptions import ProviderWebhookEventIdMissingError
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

    #: See ``BasePaymentAdapter.verifies_full_body`` — same contract, same
    #: reasoning. A payment provider that also handles subscriptions signs its
    #: subscription-payment webhooks with the same scheme as its payment
    #: webhooks in every provider this codebase integrates with so far, but the
    #: two adapters are declared independently rather than assumed to match.
    verifies_full_body: bool

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
    def change_subscription_plan(self, subscription: Subscription, new_plan: CreatedPlan) -> None:
        """
        Move `subscription`'s already-active provider-side subscription onto
        `new_plan`, with the provider computing and applying proration
        server-side (Phase 9's "proration on upgrade computed provider-side"
        guiding decision). This method does not return an amount and never
        writes anything locally: the actual charge and its outcome are learned
        asynchronously, through the same subscription-payment webhook every
        other charge on this subscription already reports through
        (`SubscriptionService.confirm_plan_change` is what reacts to it).

        :param subscription: The subscription to move, with `external_id` set —
            this is only ever called for a subscription the provider already
            knows about (see `create_subscription` for the first-ever-payment
            case, which has no existing provider-side subscription to move).
        :param new_plan: The provider-side plan/price this subscription should
            move onto.
        """
        raise NotImplementedError

    @abstractmethod
    def update_subscription_payment_token(
        self, subscription: Subscription, payment_token: str
    ) -> None:
        """
        Update the payment method backing an active subscription without
        disrupting its current billing cycle (e.g. the payer's card expired or
        was replaced).
        :param subscription: Subscription object
        :param payment_token: New payment token
        """
        raise NotImplementedError

    @abstractmethod
    def get_subscription_external_id_from_update(self, update_payload: dict) -> str | None:
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

        Defaults to the provider's own top-level notification id (``get_update_id``),
        sourced from ``payload`` — which is only safe to trust when
        ``verifies_full_body`` is ``True`` (the provider's signature covers the
        entire request body, ``payload`` included). A provider whose signature
        only covers a narrow manifest (``verifies_full_body = False``) must
        override this to re-derive the key from signed material instead (see
        ``MercadoPagoSubscriptionAdapter.get_event_id``) — inheriting this
        default would let an attacker replay a single captured valid signature
        under an unbounded number of distinct "new" event ids. This is enforced
        here rather than left to convention: ``verifies_full_body`` would
        otherwise be purely documentary, and a future narrow-manifest adapter
        that forgets to override ``get_event_id`` would get a false green light
        instead of a failure. ``raw_body``/``headers`` are passed through so an
        override can re-derive the key from signed material.
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

    def receive_payment_update(
        self, update_payload: dict
    ) -> tuple[SubscriptionPayment, PaymentStatusUpdate] | None:
        """
        Sources the subscription id from ``get_subscription_external_id_from_update``
        — the per-adapter hook, not a hardcoded ``payload["data"]["id"]`` lookup.
        That hardcoded shape only ever matched MercadoPago's notifications;
        Stripe's equivalent id is not at a fixed path (it depends on the event
        type — a subscription event vs. an invoice event). Each adapter's own
        ``get_subscription_external_id_from_update`` already knows its provider's
        shape, so this template method defers to it instead of assuming one.
        """
        if not self.is_payment_update(update_payload):
            return None

        subscription_external_id = self.get_subscription_external_id_from_update(update_payload)
        if not subscription_external_id:
            logger.error(
                "Subscription external id not found in update payload. payload: %s",
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
