import datetime
import logging
from dataclasses import asdict
from decimal import Decimal
from typing import Annotated

from dependency_injector.wiring import Provide, inject

from organizations.models import Organization
from payments.billing_constants import BillingInterval
from payments.constants import PaymentStatuses, RefundStatuses, SubscriptionStatuses
from payments.models import BillingAddress as BillingAddressModel
from payments.models import BillingPlan as BillingPlanModel
from payments.models import BillingProfile as BillingProfileModel
from payments.models import Payment as PaymentModel
from payments.models import PaymentStatusUpdate as PaymentStatusUpdateModel
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
    ):
        self.payment_gateway = payment_gateway
        self.subscription_gateway = subscription_gateway
        self.subscription_plan_factory = subscription_plan_factory

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
        # Billing is owned by the organization, not a person: the payer identity the
        # gateway sees is the organization's name. There is no per-organization
        # email/phone to source here yet, so those stay unset.
        return BillingProfile(
            pk=billing_profile.pk,
            first_name=billing_profile.organization.name,
            last_name=None,
            email=None,
            phone=None,
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
            refund.external_id = self.payment_gateway.refund(
                Refund(
                    id=refund.id,
                    value=refund.value,
                    currency=refund.currency,
                    payment=self._serialize_payment(refund.payment),
                )
            )
            RefundStatusUpdate.objects.create(
                refund=refund,
                status=RefundStatuses.PENDING,
                description="Refund created in the payment gateway, waiting for processing",
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
        refund.status = self.payment_gateway.check_refund_status(refund.external_id)
        refund.save()

    def get_payment_by_external_id(self, external_id: str) -> PaymentModel | None:
        return PaymentModel.objects.filter(external_id=external_id).first()

    def receive_payment_update(self, update_payload: dict) -> PaymentStatusUpdateModel | None:
        update_data = self.payment_gateway.receive_update(update_payload)
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
        self, update_payload: dict
    ) -> PaymentStatusUpdateModel | None:
        update_data = self.subscription_gateway.receive_payment_update(update_payload)

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
            payment = PaymentModel.objects.create(
                external_id=payment_external_id,
                billing_profile=subscription.organization.billing_profile,
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

    def _serialize_subscription(self, subscription: SubscriptionModel) -> Subscription:
        organization_billing_profile = subscription.organization.billing_profile
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
        try:
            _ = organization.billing_profile
        except BillingProfileModel.DoesNotExist as e:
            raise ValueError("Organization does not have a billing profile") from e

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

    def cancel_subscription(self, subscription: SubscriptionModel) -> None:
        self.subscription_gateway.cancel_subscription(self._serialize_subscription(subscription))
        SubscriptionStatusUpdate.objects.create(
            subscription=subscription,
            status=SubscriptionStatuses.CANCELLED,
            description="Subscription cancelled",
        )
        subscription.status = SubscriptionStatuses.CANCELLED
        subscription.save()
