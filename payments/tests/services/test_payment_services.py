import datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from model_bakery import baker

from payments.constants import (
    PaymentProviders,
    PaymentStatuses,
    RefundStatuses,
    SubscriptionStatuses,
)
from payments.services.dataclasses import (
    BillingAddress as BillingAddressDataclass,
)
from payments.services.dataclasses import (
    BillingProfile as BillingProfileDataclass,
)
from payments.services.dataclasses import (
    CreatedPlan,
)
from payments.services.dataclasses import (
    Payment as PaymentDataclass,
)
from payments.services.dataclasses import (
    PaymentStatusUpdate as PaymentStatusUpdateDataclass,
)
from payments.services.dataclasses import (
    Plan as PlanDataclass,
)
from payments.services.dataclasses import (
    Refund as RefundDataclass,
)
from payments.services.dataclasses import (
    Subscription as SubscriptionDataclass,
)
from payments.services.payment_adapters.base import BasePaymentAdapter
from payments.services.payment_service import PaymentService
from payments.services.subscription_adapters.base import (
    BaseSubscriptionAdapter,
)
from payments.services.subscription_plan_factory.base import BaseSubscriptionPlanFactory


class MockSubscriptionPlanFactory(BaseSubscriptionPlanFactory):
    def make_plan_from_subscription(self, subscription):
        return CreatedPlan(
            id=123,
            name="Test Plan",
            value=Decimal("100"),
            currency="USD",
            billing_day=1,
            external_id="external_123",
        )


@pytest.fixture
def billing_address():
    return baker.make(
        "payments.BillingAddress",
        street_name="Test Street",
        street_number="123",
        city="Test City",
        state="Test State",
        country="Test Country",
        zip_code="12345",
    )


@pytest.fixture
def billing_profile(user, billing_address):
    return baker.make(
        "payments.BillingProfile",
        user=user,
        document_type="CPF",
        document_number="12345678900",
        billing_address=billing_address,
    )


@pytest.fixture
def payment_adapter():
    adapter = MagicMock(spec=BasePaymentAdapter)
    adapter.provider = PaymentProviders.MERCADOPAGO
    return adapter


@pytest.fixture
def subscription_adapter():
    adapter = MagicMock(spec=BaseSubscriptionAdapter)
    adapter.provider = PaymentProviders.MERCADOPAGO
    return adapter


@pytest.fixture
def subscription_plan_factory():
    return MockSubscriptionPlanFactory()


@pytest.fixture
def payment_service(payment_adapter, subscription_adapter, subscription_plan_factory, di_container):
    with (
        di_container.payment_gateway.override(payment_adapter),
        di_container.subscription_gateway.override(subscription_adapter),
    ):
        return PaymentService(subscription_plan_factory=subscription_plan_factory)


@pytest.mark.django_db
def test_success_create_payment(payment_service, billing_profile):
    # Create payment using service
    payment_service.payment_gateway.process.return_value = "payment_12345"

    created_payment = payment_service.create_payment(
        user=billing_profile.user,
        currency="BRL",
        amount=Decimal("100"),
        description="Test Payment",
        payment_method="credit_card",
        payment_token="card_token_123",
    )

    # Verify payment was created correctly
    assert created_payment.id is not None
    assert created_payment.value == Decimal("100")
    assert created_payment.currency == "BRL"
    assert created_payment.payment_provider == PaymentProviders.MERCADOPAGO
    assert created_payment.status == PaymentStatuses.PENDING_SEND
    assert created_payment.payment_method == "credit_card"
    assert created_payment.description == "Test Payment"
    assert created_payment.billing_profile == billing_profile


@pytest.mark.django_db
def test_success_process_payment(payment_service, payment_adapter, billing_profile):
    # Create a payment
    payment = baker.make(
        "payments.Payment",
        billing_profile=billing_profile,
        value=Decimal("100"),
        currency="USD",
        payment_provider=PaymentProviders.MERCADOPAGO,
        status=PaymentStatuses.PENDING_SEND,
        payment_method="credit_card",
        description="Test Payment",
    )

    # Set up mock for process method
    external_payment_id = "ext_12345"
    payment_adapter.process.return_value = external_payment_id

    # Process payment
    processed_payment = payment_service.process_payment(payment, "card_token_123")

    # Verify process was called with the correct arguments
    payment_adapter.process.assert_called_once()
    payment_arg = payment_adapter.process.call_args[0][0]
    assert isinstance(payment_arg, PaymentDataclass)
    assert payment_arg.id == payment.id
    assert payment_arg.value == payment.value

    # Verify payment was updated correctly
    assert processed_payment.external_id == external_payment_id


@pytest.mark.django_db
def test_success_check_payment_status(payment_service, payment_adapter, billing_profile):
    # Create a payment
    payment = baker.make(
        "payments.Payment",
        billing_profile=billing_profile,
        value=Decimal("100"),
        currency="USD",
        payment_provider=PaymentProviders.MERCADOPAGO,
        status=PaymentStatuses.PENDING_SEND,
        payment_method="credit_card",
        description="Test Payment",
        external_id="ext_12345",
    )

    # Set up mock for check_status method
    status_update = PaymentStatusUpdateDataclass(
        id=None,
        status="approved",
        description="Payment approved",
        update_external_id="update_123",
    )
    payment_adapter.check_status.return_value = status_update

    # Check payment status
    result = payment_service.check_payment_status(payment)

    # Verify check_status was called with the correct arguments
    payment_adapter.check_status.assert_called_once_with(payment.external_id)

    # Verify result is correct
    assert result.status == "approved"
    assert result.description == "Payment approved"
    assert result.update_external_id == "update_123"


@pytest.mark.django_db
def test_success_create_refund(payment_service, payment_adapter, billing_profile):
    # Create payment and refund
    payment = baker.make(
        "payments.Payment",
        billing_profile=billing_profile,
        value=Decimal("100"),
        currency="USD",
        payment_provider=PaymentProviders.MERCADOPAGO,
        status=PaymentStatuses.APPROVED,
        payment_method="credit_card",
        description="Test Payment",
        external_id="ext_12345",
    )

    # Set up mock for request_refund method
    external_refund_id = "refund_12345"
    payment_adapter.refund.return_value = external_refund_id

    # Create refund
    created_refund = payment_service.create_refund(
        payment_id=payment.pk,
        value=Decimal("100"),
        currency="USD",
    )

    # Verify request_refund was called
    payment_adapter.refund.assert_called_once()
    refund_arg = payment_adapter.refund.call_args[0][0]
    assert isinstance(refund_arg, RefundDataclass)

    # Verify refund was updated correctly
    assert created_refund.external_id == external_refund_id


@pytest.mark.django_db
def test_success_check_refund_status(payment_service, payment_adapter, billing_profile):
    # Create payment and refund
    payment = baker.make(
        "payments.Payment",
        billing_profile=billing_profile,
        value=Decimal("100"),
        currency="USD",
        payment_provider=PaymentProviders.MERCADOPAGO,
        status=PaymentStatuses.APPROVED,
        payment_method="credit_card",
        description="Test Payment",
        external_id="ext_12345",
    )

    refund = baker.make(
        "payments.Refund",
        payment=payment,
        value=Decimal("100"),
        currency="USD",
        status=RefundStatuses.PENDING,
        external_id="refund_12345",
    )

    # Set up mock for check_refund_status method
    payment_adapter.check_refund_status.return_value = RefundStatuses.APPROVED

    # Check refund status
    payment_service.check_refund_status(refund)

    # Verify check_refund_status was called with the correct arguments
    payment_adapter.check_refund_status.assert_called_once_with(refund.external_id)

    # Verify refund status was updated
    refund.refresh_from_db()
    assert refund.status == RefundStatuses.APPROVED


@pytest.mark.django_db
def test_success_receive_payment_update(payment_service, payment_adapter, billing_profile):
    # Create a payment
    payment = baker.make(
        "payments.Payment",
        billing_profile=billing_profile,
        value=Decimal("100"),
        currency="USD",
        payment_provider=PaymentProviders.MERCADOPAGO,
        status=PaymentStatuses.PENDING,
        payment_method="credit_card",
        description="Test Payment",
        external_id="ext_12345",
    )

    # Set up mock for receive_update method
    payment_external_id = "ext_12345"
    status_update = PaymentStatusUpdateDataclass(
        id=None,
        status="approved",
        description="Payment approved",
        update_external_id="update_123",
    )
    payment_adapter.receive_update.return_value = (payment_external_id, status_update)

    # Receive payment update
    update_payload = {"type": "payment", "data": {"id": payment_external_id}}
    result = payment_service.receive_payment_update(update_payload)

    # Verify receive_update was called with the correct arguments
    payment_adapter.receive_update.assert_called_once_with(update_payload)

    # Verify result is correct
    assert result is not None
    assert result.payment == payment
    assert result.status == "approved"
    assert result.description == "Payment approved"
    assert result.external_id == "update_123"


@pytest.mark.django_db
def test_success_create_subscription_plan(payment_service, subscription_adapter):
    # Create a plan using the dataclass
    plan = PlanDataclass(
        id=123,
        name="Test Plan",
        value=Decimal("100"),
        currency="USD",
        billing_day=1,
    )

    # Set up mock for create_subscription_plan method
    external_plan_id = "plan_12345"
    subscription_adapter.create_subscription_plan.return_value = external_plan_id

    # Create subscription plan
    created_plan = payment_service.create_subscription_plan(plan)

    # Verify create_subscription_plan was called with the correct arguments
    subscription_adapter.create_subscription_plan.assert_called_once_with(plan)

    # Verify result is correct
    assert created_plan.id == plan.id
    assert created_plan.name == plan.name
    assert created_plan.value == plan.value
    assert created_plan.currency == plan.currency
    assert created_plan.billing_day == plan.billing_day
    assert created_plan.external_id == external_plan_id


@pytest.mark.django_db
def test_success_update_subscription_plan(payment_service, subscription_adapter):
    # Create a plan using the dataclass
    external_id = "plan_12345"
    plan = PlanDataclass(
        id=123,
        name="Updated Test Plan",
        value=Decimal("150"),
        currency="USD",
        billing_day=15,
    )

    # Set up mock for update_subscription_plan method
    subscription_adapter.update_subscription_plan.return_value = external_id

    # Update subscription plan
    updated_plan = payment_service.update_subscription_plan(external_id, plan)

    # Verify update_subscription_plan was called with the correct arguments
    subscription_adapter.update_subscription_plan.assert_called_once_with(external_id, plan)

    # Verify result is correct
    assert updated_plan.id == plan.id
    assert updated_plan.name == plan.name
    assert updated_plan.value == plan.value
    assert updated_plan.currency == plan.currency
    assert updated_plan.billing_day == plan.billing_day
    assert updated_plan.external_id == external_id


@pytest.mark.django_db
def test_success_create_subscription(payment_service, subscription_adapter, billing_profile):
    # Create a subscription
    now = datetime.datetime.now(tz=datetime.UTC)

    # Create subscription
    created_subscription = payment_service.create_subscription(
        user=billing_profile.user,
        start_date=now.date(),
        end_date=(now + datetime.timedelta(days=30)).date(),
    )
    assert created_subscription.pk is not None
    assert created_subscription.status == SubscriptionStatuses.PENDING_SEND


@pytest.mark.django_db
def test_success_process_subscription(payment_service, subscription_adapter, billing_profile):
    # Create a subscription
    now = datetime.datetime.now(tz=datetime.UTC)

    created_subscription = payment_service.create_subscription(
        user=billing_profile.user,
        start_date=now.date(),
        end_date=(now + datetime.timedelta(days=30)).date(),
    )

    # Set up mock for create_subscription method
    external_subscription_id = "sub_12345"
    subscription_adapter.create_subscription.return_value = external_subscription_id

    # Create subscription
    created_subscription = payment_service.process_subscription(
        subscription=created_subscription,
        payment_token="card_token_123",
    )

    # Verify create_subscription was called with the correct arguments
    subscription_adapter.create_subscription.assert_called_once()
    subscription_arg = subscription_adapter.create_subscription.call_args[1]["subscription"]
    assert isinstance(subscription_arg, SubscriptionDataclass)

    # Verify result is correct
    assert created_subscription.external_id == external_subscription_id
    assert created_subscription.status == SubscriptionStatuses.PENDING


@pytest.mark.django_db
def test_success_cancel_subscription(payment_service, subscription_adapter, billing_profile):
    # Create a subscription
    now = datetime.datetime.now(tz=datetime.UTC)
    subscription = baker.make(
        "payments.Subscription",
        billing_profile=billing_profile,
        start_date=now.date(),
        end_date=(now + datetime.timedelta(days=30)).date(),
        status=SubscriptionStatuses.ACTIVE,
        external_id="sub_12345",
    )

    # Cancel subscription
    payment_service.cancel_subscription(subscription)

    # Verify cancel_subscription was called with the correct arguments
    subscription_adapter.cancel_subscription.assert_called_once()
    subscription_arg = subscription_adapter.cancel_subscription.call_args[0][0]
    assert isinstance(subscription_arg, SubscriptionDataclass)

    # Verify subscription status was updated
    subscription.refresh_from_db()
    assert subscription.status == SubscriptionStatuses.CANCELLED


@pytest.mark.django_db
def test_success_receive_subscription_payment_update(
    payment_service, subscription_adapter, billing_profile, billing_address, user
):
    # Create a subscription
    now = datetime.datetime.now(tz=datetime.UTC)
    subscription = baker.make(
        "payments.Subscription",
        billing_profile=billing_profile,
        start_date=now.date(),
        end_date=(now + datetime.timedelta(days=30)).date(),
        status=SubscriptionStatuses.ACTIVE,
        external_id="sub_12345",
    )

    # Set up mock for receive_payment_update method
    from payments.services.dataclasses import SubscriptionPayment

    subscription_payment = SubscriptionPayment(
        id=None,
        value=Decimal("100"),
        currency="USD",
        payment_provider=PaymentProviders.MERCADOPAGO,
        external_id="payment_12345",
        status=PaymentStatuses.APPROVED,
        billing_profile=BillingProfileDataclass(
            pk=billing_profile.pk,
            first_name=user.profile.first_name,
            last_name=user.profile.last_name,
            email=user.email,
            phone=user.phone_number,
            document_type=billing_profile.document_type,
            document_number=billing_profile.document_number,
            billing_address=BillingAddressDataclass(
                id=billing_address.id,
                street_name=billing_address.street_name,
                street_number=billing_address.street_number,
                neighborhood=billing_address.neighborhood,
                address_line_2=billing_address.address_line_2,
                city=billing_address.city,
                state=billing_address.state,
                country=billing_address.country,
                zip_code=billing_address.zip_code,
            ),
        ),
        payment_method="credit_card",
        description="Subscription payment",
        status_updates=[],
        subscription_external_id=subscription.external_id,
    )

    status_update = PaymentStatusUpdateDataclass(
        id=None,
        status="approved",
        description="Payment approved",
        update_external_id="update_123",
    )

    subscription_adapter.receive_payment_update.return_value = (
        subscription_payment,
        status_update,
    )

    # Receive subscription payment update
    update_payload = {"id": "update_123", "type": "payment"}
    result = payment_service.receive_subscription_payment_update(update_payload)

    # Verify receive_payment_update was called with the correct arguments
    subscription_adapter.receive_payment_update.assert_called_once_with(update_payload)

    # Verify result is correct
    assert result is not None
    assert result.status == "approved"
    assert result.description == "Payment approved"
    assert result.external_id == "update_123"
