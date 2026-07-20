import datetime
import logging
from collections.abc import Mapping
from dataclasses import asdict
from decimal import Decimal
from typing import Annotated

from django.db import transaction

from dependency_injector.wiring import Provide, inject

from organizations.models import Organization
from payments.billing_constants import BillingInterval, ProviderWebhookRoute
from payments.constants import (
    PaymentStatuses,
    RefundStatuses,
    SubscriptionStatuses,
)
from payments.exceptions import (
    BillingProfileContactEmailMissingError,
    MissingBillingProfileError,
    UnknownPaymentProviderError,
)
from payments.models import BillingAddress as BillingAddressModel
from payments.models import BillingPlan as BillingPlanModel
from payments.models import BillingProfile as BillingProfileModel
from payments.models import Payment as PaymentModel
from payments.models import PaymentStatusUpdate as PaymentStatusUpdateModel
from payments.models import ProviderWebhookEvent as ProviderWebhookEventModel
from payments.models import Refund as RefundModel
from payments.models import RefundStatusUpdate, SubscriptionStatusUpdate
from payments.models import Subscription as SubscriptionModel
from payments.services.dataclasses import (
    BillingAddress,
    BillingProfile,
    CreatedPlan,
    Payment,
    PaymentStatusUpdate,
    Plan,
    Refund,
    Subscription,
)
from payments.services.payment_adapters.base import BasePaymentAdapter
from payments.services.subscription_adapters.base import BaseSubscriptionAdapter
from payments.services.subscription_plan_factory.base import BaseSubscriptionPlanFactory


logger = logging.getLogger(__name__)


class PaymentService[
    PaymentAdapter: BasePaymentAdapter,
    SubscriptionAdapter: BaseSubscriptionAdapter,
    SubscriptionPlanFactory: BaseSubscriptionPlanFactory,
]:
    @inject
    def __init__(
        self,
        subscription_plan_factory: SubscriptionPlanFactory,
        payment_gateway: Annotated[PaymentAdapter, Provide["payment_gateway"]],
        subscription_gateway: Annotated[SubscriptionAdapter, Provide["subscription_gateway"]],
        payment_provider_registry: Annotated[
            dict[str, PaymentAdapter], Provide["payment_provider_registry"]
        ],
        subscription_provider_registry: Annotated[
            dict[str, SubscriptionAdapter], Provide["subscription_provider_registry"]
        ],
    ):
        self.payment_gateway = payment_gateway
        self.subscription_gateway = subscription_gateway
        self.subscription_plan_factory = subscription_plan_factory
        self.payment_provider_registry = payment_provider_registry
        self.subscription_provider_registry = subscription_provider_registry

    def get_payment_adapter(self, provider: str) -> PaymentAdapter:
        """Resolve the payment adapter registered for *provider* (a URL kwarg slug)."""
        try:
            return self.payment_provider_registry[provider]
        except KeyError as e:
            raise UnknownPaymentProviderError(provider) from e

    def get_subscription_adapter(self, provider: str) -> SubscriptionAdapter:
        """Resolve the subscription adapter registered for *provider* (a URL kwarg slug)."""
        try:
            return self.subscription_provider_registry[provider]
        except KeyError as e:
            raise UnknownPaymentProviderError(provider) from e

    def create_payment(
        self,
        organization: Organization,
        currency: str,
        amount: Decimal,
        description: str,
        payment_method: str,
        payment_token: str,
    ) -> PaymentModel:
        try:
            billing_profile = organization.billing_profile
        except BillingProfileModel.DoesNotExist as e:
            raise ValueError("Organization does not have a billing profile") from e

        payment = PaymentModel.objects.create(
            billing_profile=billing_profile,
            currency=currency,
            value=amount,
            description=description,
            payment_method=payment_method,
            status=PaymentStatuses.PENDING_SEND,
            payment_provider=self.payment_gateway.provider,
        )
        external_id = self.payment_gateway.process(
            payment=self._serialize_payment(payment),
            payment_token=payment_token,
        )

        payment.external_id = external_id
        payment.save(update_fields=["external_id"])
        return payment

    def _serialize_billing_address(self, billing_address: BillingAddressModel) -> BillingAddress:
        return BillingAddress(
            id=billing_address.id,
            street_name=billing_address.street_name,
            street_number=billing_address.street_number,
            neighborhood=billing_address.neighborhood,
            city=billing_address.city,
            state=billing_address.state,
            country=billing_address.country,
            zip_code=billing_address.zip_code,
            address_line_2=billing_address.address_line_2,
        )

    def _serialize_billing_profile(self, billing_profile: BillingProfileModel) -> BillingProfile:
        # Billing is owned by the organization, not a person, but the gateway still
        # requires a payer identity (MercadoPago hard-400s on a null payer email).
        # `contact_*` on BillingProfile is the organization's designated billing
        # contact, sourced explicitly rather than left null.
        if not billing_profile.contact_email:
            raise BillingProfileContactEmailMissingError
        return BillingProfile(
            pk=billing_profile.pk,
            first_name=billing_profile.contact_first_name,
            last_name=billing_profile.contact_last_name or None,
            email=billing_profile.contact_email,
            phone=billing_profile.contact_phone or None,
            document_type=billing_profile.document_type,
            document_number=billing_profile.document_number,
            billing_address=self._serialize_billing_address(billing_profile.billing_address),
        )

    def _serialize_payment(self, payment: PaymentModel) -> Payment:
        return Payment(
            id=payment.id,
            value=payment.value,
            description=payment.description,
            payment_method=payment.payment_method,
            billing_profile=self._serialize_billing_profile(payment.billing_profile),
            currency=payment.currency,
            external_id=payment.external_id,
            status=payment.status,
            payment_provider=self.payment_gateway.provider,
            status_updates=[
                PaymentStatusUpdate(
                    id=status_update.id,
                    status=status_update.status,
                    description=status_update.description,
                    update_external_id=status_update.external_id,
                )
                for status_update in payment.status_updates.all()
            ],
        )

    def process_payment(self, payment: PaymentModel, card_token: str) -> PaymentModel:
        external_payment_id = self.payment_gateway.process(
            self._serialize_payment(payment),
            card_token,
        )

        payment.external_id = external_payment_id
        payment.save()

        return payment

    def create_refund(
        self,
        payment_id: int,
        value: Decimal,
        currency: str,
    ) -> RefundModel:
        refund = RefundModel.objects.create(
            payment_id=payment_id,
            value=value,
            currency=currency,
            status=RefundStatuses.PENDING_SEND,
        )
        RefundStatusUpdate.objects.create(
            refund=refund,
            status=RefundStatuses.PENDING_SEND,
            description="Refund created in the database, will send to payment gateway",
        )
        try:
            refund_result = self.payment_gateway.refund(
                Refund(
                    id=refund.id,
                    value=refund.value,
                    currency=refund.currency,
                    payment=self._serialize_payment(refund.payment),
                )
            )
            refund.external_id = refund_result.external_id
            refund.status = refund_result.status
            RefundStatusUpdate.objects.create(
                refund=refund,
                status=refund_result.status,
                # The status comes straight off the provider's create-refund
                # response (see `RefundResult`), not a subsequent
                # `check_refund_status` poll — both MercadoPago and Stripe return
                # it synchronously alongside the new refund's id.
                description=f"Refund created in the payment gateway with status {refund_result.status}",
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(e)
            RefundStatusUpdate.objects.create(
                refund=refund,
                status=RefundStatuses.FAILED,
                description="Failed to process refund",
            )
            refund.status = RefundStatuses.FAILED
            pass

        refund.save()
        return refund

    def check_payment_status(self, payment: PaymentModel) -> PaymentStatusUpdate:
        return self.payment_gateway.check_status(payment.external_id)

    def check_refund_status(self, refund: RefundModel) -> None:
        refund.status = self.payment_gateway.check_refund_status(
            Refund(
                id=refund.id,
                value=refund.value,
                currency=refund.currency,
                payment=self._serialize_payment(refund.payment),
                external_id=refund.external_id,
            )
        )
        refund.save()

    def get_payment_by_external_id(self, external_id: str) -> PaymentModel | None:
        return PaymentModel.objects.filter(external_id=external_id).first()

    def receive_payment_update(
        self, update_payload: dict, provider: str
    ) -> PaymentStatusUpdateModel | None:
        adapter = self.get_payment_adapter(provider)
        update_data = adapter.receive_update(update_payload)
        if not update_data:
            return None
        payment_external_id, payment_status_update_data = update_data

        payment = self.get_payment_by_external_id(payment_external_id)
        if not payment:
            return None

        return PaymentStatusUpdateModel.objects.create(
            status=payment_status_update_data.status,
            description=payment_status_update_data.description or "",
            external_id=payment_status_update_data.update_external_id or "",
            payment=payment,
        )

    def get_subscription_by_external_id(self, external_id: str) -> SubscriptionModel | None:
        return SubscriptionModel.objects.filter(external_id=external_id).first()

    def receive_subscription_payment_update(
        self, update_payload: dict, provider: str
    ) -> PaymentStatusUpdateModel | None:
        adapter = self.get_subscription_adapter(provider)
        update_data = adapter.receive_payment_update(update_payload)

        if not update_data:
            return None

        subscription_payment_data, payment_status_update_data = update_data

        subscription_external_id = subscription_payment_data.subscription_external_id
        subscription = self.get_subscription_by_external_id(subscription_external_id)
        if not subscription:
            return None

        payment_external_id = subscription_payment_data.external_id
        payment = self.get_payment_by_external_id(payment_external_id)
        if not payment:
            billing_profile = BillingProfileModel.objects.filter(
                organization=subscription.organization
            ).first()
            if billing_profile is None:
                logger.warning(
                    "Cannot create payment for subscription %s: organization %s has no "
                    "billing profile.",
                    subscription.id,
                    subscription.organization_id,
                )
                return None
            payment = PaymentModel.objects.create(
                external_id=payment_external_id,
                billing_profile=billing_profile,
                value=subscription_payment_data.value,
                currency=subscription_payment_data.currency,
                status=subscription_payment_data.status,
                description=subscription_payment_data.description,
                payment_method=subscription_payment_data.payment_method,
                subscription=subscription,
            )

        return PaymentStatusUpdateModel.objects.create(
            status=payment_status_update_data.status,
            description=payment_status_update_data.description or "",
            external_id=payment_status_update_data.update_external_id or "",
            payment=payment,
        )

    def verify_payment_webhook_signature(
        self, provider: str, raw_body: bytes, headers: Mapping[str, str]
    ) -> bool:
        """Verify an inbound ``payment-update`` webhook against *raw_body*.

        ``raw_body`` must be the literal bytes the provider sent (see
        ``BasePaymentAdapter.verify_signature``) — callers must capture
        ``request.body`` before touching ``request.data``.
        """
        return self.get_payment_adapter(provider).verify_signature(raw_body, headers)

    def verify_subscription_webhook_signature(
        self, provider: str, raw_body: bytes, headers: Mapping[str, str]
    ) -> bool:
        """Verify an inbound ``subscription-payment-update`` webhook against *raw_body*.

        ``raw_body`` must be the literal bytes the provider sent (see
        ``BaseSubscriptionAdapter.verify_signature``) — callers must capture
        ``request.body`` before touching ``request.data``.
        """
        return self.get_subscription_adapter(provider).verify_signature(raw_body, headers)

    def handle_payment_webhook(
        self, provider: str, raw_body: bytes, headers: Mapping[str, str], payload: dict
    ) -> PaymentStatusUpdateModel | None:
        """Idempotently process an inbound ``payment-update`` webhook notification.

        Callers must call ``verify_payment_webhook_signature`` first — this method
        does not re-verify authenticity, only idempotency + dispatch. Safe to call
        more than once with the same provider event: a redelivery of an
        already-processed event is a no-op.

        ``raw_body``/``headers`` must be the same values already verified by
        ``verify_payment_webhook_signature`` — the idempotency ledger key is
        derived from them (signed material), never from ``payload`` alone, which
        may contain unsigned fields an attacker can vary across replays.

        ``mark_processed`` only runs when ``receive_payment_update`` actually
        returns a result. A ``None`` result — whether because the event is
        irrelevant or because something failed to resolve (e.g. a since-fixed
        adapter bug) — leaves the ledger row unprocessed, per
        ``ProviderWebhookEventManager.get_or_create_pending``'s contract: an
        unprocessed row is exactly what allows a provider redelivery of the same
        event to be retried instead of being permanently burned.
        """
        adapter = self.get_payment_adapter(provider)
        event_id = adapter.get_event_id(raw_body, headers, payload)
        with transaction.atomic():
            event, is_new_delivery = ProviderWebhookEventModel.objects.get_or_create_pending(
                provider=provider,
                route=ProviderWebhookRoute.PAYMENT_UPDATE,
                external_event_id=event_id,
                payload=payload,
            )
            if not is_new_delivery:
                return None

            result = self.receive_payment_update(payload, provider=provider)
            if result is not None:
                ProviderWebhookEventModel.objects.mark_processed(event)
        return result

    def handle_subscription_payment_webhook(
        self, provider: str, raw_body: bytes, headers: Mapping[str, str], payload: dict
    ) -> PaymentStatusUpdateModel | None:
        """Idempotently process an inbound ``subscription-payment-update`` webhook.

        Callers must call ``verify_subscription_webhook_signature`` first — this
        method does not re-verify authenticity, only idempotency + dispatch. Safe to
        call more than once with the same provider event: a redelivery of an
        already-processed event is a no-op.

        ``raw_body``/``headers`` must be the same values already verified by
        ``verify_subscription_webhook_signature`` — the idempotency ledger key is
        derived from them (signed material), never from ``payload`` alone, which
        may contain unsigned fields an attacker can vary across replays.

        ``mark_processed`` only runs when ``receive_subscription_payment_update``
        actually returns a result — see ``handle_payment_webhook``'s docstring for
        why a ``None`` result must not permanently burn the delivery.
        """
        adapter = self.get_subscription_adapter(provider)
        event_id = adapter.get_event_id(raw_body, headers, payload)
        with transaction.atomic():
            event, is_new_delivery = ProviderWebhookEventModel.objects.get_or_create_pending(
                provider=provider,
                route=ProviderWebhookRoute.SUBSCRIPTION_PAYMENT_UPDATE,
                external_event_id=event_id,
                payload=payload,
            )
            if not is_new_delivery:
                return None

            result = self.receive_subscription_payment_update(payload, provider=provider)
            if result is not None:
                ProviderWebhookEventModel.objects.mark_processed(event)
        return result

    def _serialize_subscription(self, subscription: SubscriptionModel) -> Subscription:
        organization_billing_profile = BillingProfileModel.objects.filter(
            organization=subscription.organization
        ).first()
        if organization_billing_profile is None:
            logger.warning(
                "Cannot serialize subscription %s: organization %s has no billing profile.",
                subscription.id,
                subscription.organization_id,
            )
            raise MissingBillingProfileError
        return Subscription(
            id=subscription.id,
            plan=self.subscription_plan_factory.make_plan_from_subscription(subscription),
            status=subscription.status,
            external_id=subscription.external_id,
            billing_profile=self._serialize_billing_profile(organization_billing_profile),
            start_date=subscription.current_period_start.strftime("%Y-%m-%d"),
            end_date=subscription.current_period_end.strftime("%Y-%m-%d"),
        )

    def create_subscription_plan(self, plan: Plan) -> CreatedPlan:
        external_id = self.subscription_gateway.create_subscription_plan(plan)
        return CreatedPlan(external_id=external_id, **asdict(plan))

    def update_subscription_plan(self, external_id: str, new_plan_data: Plan) -> CreatedPlan:
        external_id = self.subscription_gateway.update_subscription_plan(external_id, new_plan_data)
        return CreatedPlan(external_id=external_id, **asdict(new_plan_data))

    def create_subscription(
        self,
        organization: Organization,
        plan: BillingPlanModel,
        current_period_start: datetime.datetime,
        current_period_end: datetime.datetime,
        billing_interval: str = BillingInterval.MONTHLY,
    ) -> SubscriptionModel:
        # NOTE (Phase 4 verification review, carried forward, not fixed here — out
        # of Phase 4 scope): this is an unconditional ``SubscriptionModel.objects.create``
        # against a ``OneToOneField`` to ``organization``. Since Phase 4,
        # every billing-root organization already has a ``Subscription`` (see
        # ``SubscriptionService.create_subscription_for_organization``), so calling
        # this against one will raise ``IntegrityError``, and it does not create
        # ``SubscriptionPlanLimit`` / ``SubscriptionEntitlement`` rows even when it
        # succeeds. Currently exercised only by tests (latent). Do not build new
        # subscription-creation flows on this path — use ``SubscriptionService``
        # instead; this needs reconciling with the Phase 4 "no plan-less state"
        # invariant before it is used for real.
        if not BillingProfileModel.objects.filter(organization=organization).exists():
            raise MissingBillingProfileError

        subscription = SubscriptionModel.objects.create(
            organization=organization,
            plan=plan,
            billing_interval=billing_interval,
            current_period_start=current_period_start,
            current_period_end=current_period_end,
            status=SubscriptionStatuses.PENDING_SEND,
            payment_provider=self.subscription_gateway.provider,
        )

        SubscriptionStatusUpdate.objects.create(
            subscription=subscription,
            status=SubscriptionStatuses.PENDING_SEND,
            description="Subscription created in the database, will send to subscription gateway",
        )

        return subscription

    def process_subscription(
        self,
        subscription: SubscriptionModel,
        payment_token: str,
    ) -> SubscriptionModel:
        subscription.external_id = self.subscription_gateway.create_subscription(
            subscription=self._serialize_subscription(subscription),
            payment_token=payment_token,
        )
        SubscriptionStatusUpdate.objects.create(
            subscription=subscription,
            status=SubscriptionStatuses.PENDING,
            description="Subscription created in subscription gateway, waiting for payment",
        )
        subscription.status = SubscriptionStatuses.PENDING
        subscription.save(update_fields=["external_id", "status"])
        return subscription

    def change_subscription_plan(
        self, subscription: SubscriptionModel, new_plan: CreatedPlan
    ) -> None:
        """Move `subscription`'s provider-side subscription onto `new_plan`.

        Thin wrapper over the adapter -- see
        `BaseSubscriptionAdapter.change_subscription_plan` for the proration
        contract. Writes nothing locally: the outcome arrives later through the
        subscription-payment webhook.
        """
        self.subscription_gateway.change_subscription_plan(
            self._serialize_subscription(subscription), new_plan
        )

    def cancel_subscription(self, subscription: SubscriptionModel) -> None:
        self.subscription_gateway.cancel_subscription(self._serialize_subscription(subscription))
        SubscriptionStatusUpdate.objects.create(
            subscription=subscription,
            status=SubscriptionStatuses.CANCELLED,
            description="Subscription cancelled",
        )
        subscription.status = SubscriptionStatuses.CANCELLED
        subscription.save()
