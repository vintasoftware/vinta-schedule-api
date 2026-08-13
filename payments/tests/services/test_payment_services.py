import datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from model_bakery import baker

from audit.constants import AuditAction
from organizations.models import Organization
from organizations.tests.helpers import grant_membership_groups
from payments.billing_constants import BillingInterval, BillingState
from payments.constants import (
    PaymentProviders,
    PaymentStatuses,
    RefundStatuses,
    SubscriptionStatuses,
)
from payments.exceptions import (
    MissingBillingProfileError,
    PaymentProviderNotConfiguredError,
    UnknownPaymentProviderError,
)
from payments.models import BillingPlan, ProviderWebhookEvent
from payments.models import Payment as PaymentModel
from payments.models import Subscription as SubscriptionModel
from payments.services.dataclasses import (
    BillingAddress as BillingAddressDataclass,
)
from payments.services.dataclasses import (
    BillingProfile as BillingProfileDataclass,
)
from payments.services.dataclasses import (
    CreatedPlan,
    RefundResult,
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
from payments.services.payment_adapters.stripe_payment_adapter import StripePaymentAdapter
from payments.services.payment_service import PaymentService
from payments.services.subscription_adapters.base import (
    BaseSubscriptionAdapter,
)
from payments.services.subscription_adapters.stripe_subscription_adapter import (
    StripeSubscriptionAdapter,
)
from payments.services.subscription_plan_factory.base import BaseSubscriptionPlanFactory
from payments.services.subscription_service import SubscriptionService


# This module builds its own Subscription rows (OneToOne with Organization), so it
# opts out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription


class MockSubscriptionPlanFactory(BaseSubscriptionPlanFactory):
    def make_plan_from_subscription(self, subscription):
        return CreatedPlan(
            id=123,
            name="Test Plan",
            value=Decimal("100"),
            currency="USD",
            billing_day=1,
            billing_interval=BillingInterval.MONTHLY,
            external_id="external_123",
        )


@pytest.fixture
def organization():
    return baker.make(Organization)


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
def billing_profile(organization, billing_address):
    """Unpinned by default -- several Phase 2 tests below assert on the
    "never pinned" (``payment_provider == ""``) starting state. Tests that
    exercise `create_payment`/`create_subscription` (Rule B: resolves the
    provider from the organization) pin this explicitly to MercadoPago, to
    match the mocked MercadoPago DI slot rather than resolving to
    ``settings.DEFAULT_PAYMENT_PROVIDER`` and driving the real, unmocked
    Stripe adapter."""
    return baker.make(
        "payments.BillingProfile",
        organization=organization,
        document_type="CPF",
        document_number="12345678900",
        billing_address=billing_address,
    )


@pytest.fixture
def mercadopago_pinned_billing_profile(billing_profile):
    """`billing_profile`, pinned to MercadoPago -- for tests exercising Rule B
    (`create_payment`/`create_subscription`) that need the resolved provider to
    match the mocked MercadoPago DI slot."""
    billing_profile.payment_provider = PaymentProviders.MERCADOPAGO
    billing_profile.save(update_fields=["payment_provider"])
    return billing_profile


@pytest.fixture
def billing_plan():
    return baker.make(BillingPlan)


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
def stripe_payment_adapter():
    """The Stripe payment DI slot, mocked the same way as ``payment_adapter``
    -- used by the Rule A/Rule B routing tests below, which need a *second*,
    independently assertable provider in the registry so a test can prove a
    call reached one adapter and not the other."""
    adapter = MagicMock(spec=BasePaymentAdapter)
    adapter.provider = PaymentProviders.STRIPE
    return adapter


@pytest.fixture
def stripe_subscription_adapter():
    adapter = MagicMock(spec=BaseSubscriptionAdapter)
    adapter.provider = PaymentProviders.STRIPE
    return adapter


@pytest.fixture
def subscription_plan_factory():
    return MockSubscriptionPlanFactory()


@pytest.fixture
def payment_service(
    payment_adapter,
    subscription_adapter,
    stripe_payment_adapter,
    stripe_subscription_adapter,
    subscription_plan_factory,
    di_container,
):
    """Both provider slots are mocked -- MercadoPago (what ``billing_profile``
    is pinned to by default) and Stripe (used by the Rule A/Rule B routing
    tests) -- so no test constructing this fixture can accidentally reach a
    real, unconfigured adapter over the network."""
    with (
        di_container.payment_gateway.override(payment_adapter),
        di_container.subscription_gateway.override(subscription_adapter),
        di_container.stripe_payment_gateway.override(stripe_payment_adapter),
        di_container.stripe_subscription_gateway.override(stripe_subscription_adapter),
    ):
        return PaymentService(subscription_plan_factory=subscription_plan_factory)


@pytest.fixture
def subscription_service():
    """Bare ``SubscriptionService()`` — ``payment_service``/``audit_service`` are
    auto-resolved from the wired container, the same pattern every other bare
    ``SubscriptionService()`` construction in this test suite relies on."""
    return SubscriptionService()


@pytest.mark.django_db
def test_success_create_payment(
    payment_service, payment_adapter, mercadopago_pinned_billing_profile
):
    billing_profile = mercadopago_pinned_billing_profile
    # Create payment using service
    payment_adapter.process.return_value = "payment_12345"

    created_payment = payment_service.create_payment(
        organization=billing_profile.organization,
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
def test_create_payment_raises_when_billing_profile_missing_contact_email(
    payment_service, organization, billing_address
):
    """`_serialize_billing_profile` must raise a clear error rather than silently
    send the payment gateway a null payer email."""
    from payments.exceptions import BillingProfileContactEmailMissingError
    from payments.models import BillingProfile as BillingProfileModel

    billing_profile = BillingProfileModel.objects.create(
        organization=organization,
        contact_first_name="Ada",
        contact_email="",
        document_type="CPF",
        document_number="12345678900",
        billing_address=billing_address,
    )

    with pytest.raises(BillingProfileContactEmailMissingError):
        payment_service.create_payment(
            organization=billing_profile.organization,
            currency="BRL",
            amount=Decimal("100"),
            description="Test Payment",
            payment_method="credit_card",
            payment_token="card_token_123",
        )


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
    payment_adapter.refund.return_value = RefundResult(
        external_id=external_refund_id, status=RefundStatuses.PENDING
    )

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

    # Verify refund was updated correctly — the status comes straight off the
    # provider's create-refund response (`RefundResult`), not a follow-up
    # `check_refund_status` poll.
    assert created_refund.external_id == external_refund_id
    assert created_refund.status == RefundStatuses.PENDING


@pytest.mark.django_db
def test_create_refund_persists_unknown_status_from_provider(
    payment_service, payment_adapter, billing_profile
):
    """`refund.status` is written straight from the provider response — an
    unmapped/unrecognized status must persist as `RefundStatuses.UNKNOWN`
    (not silently coerced into something else), and the corresponding
    `RefundStatusUpdate` row must record it too."""
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

    external_refund_id = "refund_unknown_1"
    payment_adapter.refund.return_value = RefundResult(
        external_id=external_refund_id, status=RefundStatuses.UNKNOWN
    )

    created_refund = payment_service.create_refund(
        payment_id=payment.pk,
        value=Decimal("100"),
        currency="USD",
    )

    assert created_refund.external_id == external_refund_id
    assert created_refund.status == RefundStatuses.UNKNOWN
    latest_status_update = created_refund.status_updates.latest("id")
    assert latest_status_update.status == RefundStatuses.UNKNOWN


@pytest.mark.django_db
def test_create_refund_persists_failed_status_from_provider(
    payment_service, payment_adapter, billing_profile
):
    """A provider-reported `failed` refund status must persist as-is, not be
    silently downgraded to `UNKNOWN` or left at `PENDING_SEND`."""
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

    external_refund_id = "refund_failed_1"
    payment_adapter.refund.return_value = RefundResult(
        external_id=external_refund_id, status=RefundStatuses.FAILED
    )

    created_refund = payment_service.create_refund(
        payment_id=payment.pk,
        value=Decimal("100"),
        currency="USD",
    )

    assert created_refund.external_id == external_refund_id
    assert created_refund.status == RefundStatuses.FAILED
    latest_status_update = created_refund.status_updates.latest("id")
    assert latest_status_update.status == RefundStatuses.FAILED


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

    # Verify check_refund_status was called with a `Refund` dataclass carrying
    # both the refund's own external id and the parent payment's — MercadoPago
    # has no single-refund-by-id lookup, only a list-by-payment one.
    payment_adapter.check_refund_status.assert_called_once()
    refund_arg = payment_adapter.check_refund_status.call_args[0][0]
    assert isinstance(refund_arg, RefundDataclass)
    assert refund_arg.external_id == refund.external_id
    assert refund_arg.payment.external_id == payment.external_id

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
    result = payment_service.receive_payment_update(
        update_payload, provider=PaymentProviders.MERCADOPAGO
    )

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
        billing_interval=BillingInterval.MONTHLY,
    )

    # Set up mock for create_subscription_plan method
    external_plan_id = "plan_12345"
    subscription_adapter.create_subscription_plan.return_value = external_plan_id

    # Create subscription plan
    created_plan = payment_service.create_subscription_plan(
        plan, provider=PaymentProviders.MERCADOPAGO
    )

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
        billing_interval=BillingInterval.MONTHLY,
    )

    # Set up mock for update_subscription_plan method
    subscription_adapter.update_subscription_plan.return_value = external_id

    # Update subscription plan
    updated_plan = payment_service.update_subscription_plan(
        external_id, plan, provider=PaymentProviders.MERCADOPAGO
    )

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
def test_success_create_subscription(
    payment_service, subscription_adapter, mercadopago_pinned_billing_profile, billing_plan
):
    billing_profile = mercadopago_pinned_billing_profile
    # Create a subscription
    now = datetime.datetime.now(tz=datetime.UTC)

    # Create subscription
    created_subscription = payment_service.create_subscription(
        organization=billing_profile.organization,
        plan=billing_plan,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )
    assert created_subscription.pk is not None
    assert created_subscription.status == SubscriptionStatuses.PENDING_SEND
    assert created_subscription.payment_provider == PaymentProviders.MERCADOPAGO


@pytest.mark.django_db
def test_success_process_subscription(
    payment_service, subscription_adapter, mercadopago_pinned_billing_profile, billing_plan
):
    billing_profile = mercadopago_pinned_billing_profile
    # Create a subscription
    now = datetime.datetime.now(tz=datetime.UTC)

    created_subscription = payment_service.create_subscription(
        organization=billing_profile.organization,
        plan=billing_plan,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
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
def test_success_cancel_subscription(
    payment_service, subscription_adapter, billing_profile, billing_plan
):
    # Create a subscription
    now = datetime.datetime.now(tz=datetime.UTC)
    subscription = baker.make(
        "payments.Subscription",
        organization=billing_profile.organization,
        plan=billing_plan,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
        status=SubscriptionStatuses.ACTIVE,
        external_id="sub_12345",
        payment_provider=PaymentProviders.MERCADOPAGO,
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
    payment_service, subscription_adapter, billing_profile, billing_address, billing_plan, user
):
    # Create a subscription
    now = datetime.datetime.now(tz=datetime.UTC)
    subscription = baker.make(
        "payments.Subscription",
        organization=billing_profile.organization,
        plan=billing_plan,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
        status=SubscriptionStatuses.ACTIVE,
        external_id="sub_12345",
        payment_provider=PaymentProviders.MERCADOPAGO,
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
    result = payment_service.receive_subscription_payment_update(
        update_payload, provider=PaymentProviders.MERCADOPAGO
    )

    # Verify receive_payment_update was called with the correct arguments
    subscription_adapter.receive_payment_update.assert_called_once_with(update_payload)

    # Verify result is correct
    assert result is not None
    assert result.status == "approved"
    assert result.description == "Payment approved"
    assert result.external_id == "update_123"


@pytest.mark.django_db
def test_receive_subscription_payment_update_without_billing_profile_returns_none(
    payment_service, subscription_adapter, organization, billing_plan
):
    """A subscription whose organization has no `BillingProfile` must not raise
    `RelatedObjectDoesNotExist` on the unauthenticated provider webhook path — it
    should log a warning and return `None` instead of a 500."""
    now = datetime.datetime.now(tz=datetime.UTC)
    subscription = baker.make(
        "payments.Subscription",
        organization=organization,
        plan=billing_plan,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
        status=SubscriptionStatuses.ACTIVE,
        external_id="sub_no_profile",
        payment_provider=PaymentProviders.MERCADOPAGO,
    )

    from payments.services.dataclasses import SubscriptionPayment

    subscription_payment = SubscriptionPayment(
        id=None,
        value=Decimal("100"),
        currency="USD",
        payment_provider=PaymentProviders.MERCADOPAGO,
        external_id="payment_no_profile",
        status=PaymentStatuses.APPROVED,
        billing_profile=None,
        payment_method="credit_card",
        description="Subscription payment",
        status_updates=[],
        subscription_external_id=subscription.external_id,
    )

    status_update = PaymentStatusUpdateDataclass(
        id=None,
        status="approved",
        description="Payment approved",
        update_external_id="update_456",
    )

    subscription_adapter.receive_payment_update.return_value = (
        subscription_payment,
        status_update,
    )

    update_payload = {"id": "update_456", "type": "payment"}
    result = payment_service.receive_subscription_payment_update(
        update_payload, provider=PaymentProviders.MERCADOPAGO
    )

    assert result is None
    assert not PaymentModel.objects.filter(external_id="payment_no_profile").exists()


@pytest.mark.django_db
def test_get_payment_adapter_returns_registered_provider(payment_service, payment_adapter):
    assert payment_service.get_payment_adapter(PaymentProviders.MERCADOPAGO) is payment_adapter


@pytest.mark.django_db
def test_get_payment_adapter_raises_for_unknown_provider(payment_service):
    with pytest.raises(UnknownPaymentProviderError):
        payment_service.get_payment_adapter("some-unregistered-provider")


@pytest.mark.django_db
def test_get_subscription_adapter_returns_registered_provider(
    payment_service, subscription_adapter
):
    assert (
        payment_service.get_subscription_adapter(PaymentProviders.MERCADOPAGO)
        is subscription_adapter
    )


@pytest.mark.django_db
def test_get_subscription_adapter_raises_for_unknown_provider(payment_service):
    with pytest.raises(UnknownPaymentProviderError):
        payment_service.get_subscription_adapter("some-unregistered-provider")


# ---------------------------------------------------------------------------
# Provider routing (Payment Provider Selection, Phase 4) -- two resolution
# rules: existing-row operations resolve from the row's own stored provider
# (Rule A); new-row operations resolve from the organization (Rule B).
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_mercadopago_payment_is_refunded_and_status_checked_via_mercadopago_even_when_org_pin_is_stripe(
    payment_service, payment_adapter, stripe_payment_adapter, billing_profile
):
    """The single most important assertion in this phase: a `Payment` row
    stamped `mercadopago` must be refunded and status-checked through the
    MercadoPago adapter even when its organization's *current* pin says
    `stripe` (Rule A). Using the org's pin here would send a MercadoPago
    external id to Stripe -- meaningless at best, a charge against the wrong
    instrument at worst."""
    billing_profile.payment_provider = PaymentProviders.STRIPE
    billing_profile.save(update_fields=["payment_provider"])

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

    payment_adapter.check_status.return_value = PaymentStatusUpdateDataclass(
        id=None, status="approved", description="ok", update_external_id="upd-1"
    )
    status_result = payment_service.check_payment_status(payment)
    payment_adapter.check_status.assert_called_once_with(payment.external_id)
    stripe_payment_adapter.check_status.assert_not_called()
    assert status_result.status == "approved"

    payment_adapter.refund.return_value = RefundResult(
        external_id="refund_1", status=RefundStatuses.PENDING
    )
    created_refund = payment_service.create_refund(
        payment_id=payment.pk, value=Decimal("100"), currency="USD"
    )
    payment_adapter.refund.assert_called_once()
    stripe_payment_adapter.refund.assert_not_called()
    assert created_refund.external_id == "refund_1"

    payment_adapter.check_refund_status.return_value = RefundStatuses.APPROVED
    payment_service.check_refund_status(created_refund)
    payment_adapter.check_refund_status.assert_called_once()
    stripe_payment_adapter.check_refund_status.assert_not_called()
    created_refund.refresh_from_db()
    assert created_refund.status == RefundStatuses.APPROVED


@pytest.mark.django_db
def test_create_payment_for_stripe_pinned_org_stamps_and_drives_stripe(
    payment_service, stripe_payment_adapter, payment_adapter, billing_profile
):
    """Rule B: `create_payment` creates a *new* row, so it resolves from the
    organization's pin, not any existing row."""
    billing_profile.payment_provider = PaymentProviders.STRIPE
    billing_profile.save(update_fields=["payment_provider"])
    stripe_payment_adapter.process.return_value = "stripe_payment_1"

    created_payment = payment_service.create_payment(
        organization=billing_profile.organization,
        currency="USD",
        amount=Decimal("100"),
        description="Test Payment",
        payment_method="credit_card",
        payment_token="tok_stripe",
    )

    assert created_payment.payment_provider == PaymentProviders.STRIPE
    assert created_payment.external_id == "stripe_payment_1"
    stripe_payment_adapter.process.assert_called_once()
    payment_adapter.process.assert_not_called()


@pytest.mark.django_db
def test_create_payment_for_unpinned_org_drives_default_payment_provider(
    payment_service, stripe_payment_adapter, payment_adapter, billing_profile, settings
):
    """Rule B, unpinned case: an org with no pin at all resolves to
    `settings.DEFAULT_PAYMENT_PROVIDER`, not to whatever adapter happens to be
    mocked in this test suite."""
    settings.DEFAULT_PAYMENT_PROVIDER = PaymentProviders.STRIPE
    billing_profile.payment_provider = ""
    billing_profile.save(update_fields=["payment_provider"])
    stripe_payment_adapter.process.return_value = "stripe_payment_default"

    created_payment = payment_service.create_payment(
        organization=billing_profile.organization,
        currency="USD",
        amount=Decimal("50"),
        description="Default Provider Payment",
        payment_method="credit_card",
        payment_token="tok_default",
    )

    assert created_payment.payment_provider == PaymentProviders.STRIPE
    stripe_payment_adapter.process.assert_called_once()
    payment_adapter.process.assert_not_called()


@pytest.mark.django_db
def test_create_payment_for_org_pinned_to_unconfigured_provider_raises_and_creates_no_row(
    subscription_plan_factory, payment_adapter, subscription_adapter, billing_profile, di_container
):
    """An org pinned to a real, registered provider this deployment holds no
    **outbound** credential for raises `PaymentProviderNotConfiguredError` and
    leaves no `Payment` row behind -- never falls back to the default.

    Drives a *real* `StripePaymentAdapter` built with an empty `api_key`, which
    is exactly what the DI container produces when `STRIPE_SECRET_KEY` is unset
    -- rather than asserting against a settings flag the adapter never reads.
    "Configured" is defined by the credential the adapter authenticates its
    outbound calls with (`BasePaymentAdapter.is_configured`), not by the
    browser-safe publishable key: a deployment can legitimately hold one without
    the other, and gating charges on the publishable key both refuses a provider
    whose secret works and green-lights one whose secret is empty. No network
    call is reachable here -- resolution raises before `process` is called."""
    billing_profile.payment_provider = PaymentProviders.STRIPE
    billing_profile.save(update_fields=["payment_provider"])
    unconfigured_stripe = StripePaymentAdapter(api_key="", webhook_secret="whsec_x")
    assert unconfigured_stripe.is_configured is False

    with (
        di_container.payment_gateway.override(payment_adapter),
        di_container.subscription_gateway.override(subscription_adapter),
        di_container.stripe_payment_gateway.override(unconfigured_stripe),
    ):
        payment_service = PaymentService(subscription_plan_factory=subscription_plan_factory)

        with pytest.raises(PaymentProviderNotConfiguredError):
            payment_service.create_payment(
                organization=billing_profile.organization,
                currency="USD",
                amount=Decimal("10"),
                description="Should not be created",
                payment_method="credit_card",
                payment_token="tok_x",
            )

    assert not PaymentModel.objects.filter(description="Should not be created").exists()


@pytest.mark.django_db
def test_configured_check_reads_the_outbound_credential_not_the_publishable_key(
    payment_service, stripe_payment_adapter, billing_profile, settings
):
    """The counterpart of the test above, and the reason it exists: an empty
    *publishable* key must not stop a charge through a provider whose secret key
    is present. `STRIPE_PUBLISHABLE_KEY` is what a browser needs to build a form;
    it is never sent on an outbound call from this process."""
    settings.STRIPE_PUBLISHABLE_KEY = ""
    billing_profile.payment_provider = PaymentProviders.STRIPE
    billing_profile.save(update_fields=["payment_provider"])
    stripe_payment_adapter.process.return_value = "stripe_payment_no_pubkey"

    created_payment = payment_service.create_payment(
        organization=billing_profile.organization,
        currency="USD",
        amount=Decimal("10"),
        description="Charged with no publishable key",
        payment_method="credit_card",
        payment_token="tok_x",
    )

    assert created_payment.payment_provider == PaymentProviders.STRIPE
    stripe_payment_adapter.process.assert_called_once()


@pytest.mark.django_db
def test_create_payment_for_org_pinned_to_slug_that_is_not_a_real_provider_raises_unknown(
    payment_service, billing_profile
):
    """Proves the Unknown/NotConfigured distinction survives at the
    `get_payment_adapter` boundary: a pin naming a slug that is not a provider
    at all -- bad data in the pin column -- raises `UnknownPaymentProviderError`,
    never `PaymentProviderNotConfiguredError`."""
    billing_profile.payment_provider = "not_a_real_provider"
    billing_profile.save(update_fields=["payment_provider"])

    with pytest.raises(UnknownPaymentProviderError):
        payment_service.create_payment(
            organization=billing_profile.organization,
            currency="USD",
            amount=Decimal("10"),
            description="Should not be created either",
            payment_method="credit_card",
            payment_token="tok_y",
        )

    assert not PaymentModel.objects.filter(description="Should not be created either").exists()


@pytest.mark.django_db
def test_handle_payment_webhook_is_idempotent(payment_service, payment_adapter, billing_profile):
    """A redelivery of the same provider event runs the handler at most once."""
    payment = baker.make(
        "payments.Payment",
        billing_profile=billing_profile,
        value=Decimal("100"),
        currency="USD",
        payment_provider=PaymentProviders.MERCADOPAGO,
        status=PaymentStatuses.PENDING,
        payment_method="credit_card",
        external_id="ext_12345",
    )
    payment_adapter.get_event_id.return_value = "notif-1"
    payment_adapter.receive_update.return_value = (
        "ext_12345",
        PaymentStatusUpdateDataclass(
            id=None, status="approved", description="ok", update_external_id="notif-1"
        ),
    )
    payload = {"type": "payment", "action": "payment.update", "id": "notif-1"}
    raw_body = b"{}"
    headers: dict[str, str] = {}

    first = payment_service.handle_payment_webhook(
        PaymentProviders.MERCADOPAGO, raw_body, headers, payload
    )
    second = payment_service.handle_payment_webhook(
        PaymentProviders.MERCADOPAGO, raw_body, headers, payload
    )

    assert first is not None
    assert second is None  # short-circuited: already processed
    assert payment_adapter.receive_update.call_count == 1
    assert ProviderWebhookEvent.objects.count() == 1
    assert ProviderWebhookEvent.objects.get().processed_at is not None
    payment.refresh_from_db()
    assert payment.status_updates.count() == 1


@pytest.mark.django_db
def test_handle_subscription_payment_webhook_is_idempotent(
    payment_service, subscription_adapter, billing_profile, billing_address, billing_plan, user
):
    """A redelivery of the same provider event runs the handler at most once —
    once the event has actually been processed (a non-`None` result)."""
    now = datetime.datetime.now(tz=datetime.UTC)
    subscription = baker.make(
        "payments.Subscription",
        organization=billing_profile.organization,
        plan=billing_plan,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
        status=SubscriptionStatuses.ACTIVE,
        external_id="sub_12345",
        payment_provider=PaymentProviders.MERCADOPAGO,
    )
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
    subscription_adapter.get_event_id.return_value = "notif-2"
    subscription_adapter.receive_payment_update.return_value = (
        subscription_payment,
        PaymentStatusUpdateDataclass(
            id=None, status="approved", description="ok", update_external_id="notif-2"
        ),
    )
    payload = {"type": "subscription_authorized_payment", "id": "notif-2"}
    raw_body = b"{}"
    headers: dict[str, str] = {}

    first = payment_service.handle_subscription_payment_webhook(
        PaymentProviders.MERCADOPAGO, raw_body, headers, payload
    )
    second = payment_service.handle_subscription_payment_webhook(
        PaymentProviders.MERCADOPAGO, raw_body, headers, payload
    )

    assert first is not None
    assert second is None  # short-circuited: already processed
    assert subscription_adapter.receive_payment_update.call_count == 1
    assert ProviderWebhookEvent.objects.count() == 1
    assert ProviderWebhookEvent.objects.get().processed_at is not None


@pytest.mark.django_db
def test_handle_payment_webhook_none_result_does_not_burn_the_delivery(
    payment_service, payment_adapter, billing_profile
):
    """A `None` result from `receive_payment_update` — e.g. from an adapter bug —
    must not be recorded as `mark_processed`, so a provider redelivery of the
    same event can still be processed once the underlying issue is fixed. Old
    behavior (`PaymentExternalIdMissingInNotificationError` raised outside any
    handler) 500'd the view and rolled the whole `ProviderWebhookEvent` row back
    via `transaction.atomic()` — allowing exactly this kind of retry. The `None`
    return path must preserve that property instead of silently swallowing it."""
    payment = baker.make(
        "payments.Payment",
        billing_profile=billing_profile,
        value=Decimal("100"),
        currency="USD",
        payment_provider=PaymentProviders.MERCADOPAGO,
        status=PaymentStatuses.PENDING,
        payment_method="credit_card",
        external_id="ext_99999",
    )
    payment_adapter.get_event_id.return_value = "notif-3"
    payload = {"type": "payment", "action": "payment.update", "id": "notif-3"}
    raw_body = b"{}"
    headers: dict[str, str] = {}

    payment_adapter.receive_update.return_value = None
    first = payment_service.handle_payment_webhook(
        PaymentProviders.MERCADOPAGO, raw_body, headers, payload
    )

    assert first is None
    event = ProviderWebhookEvent.objects.get()
    assert event.processed_at is None

    # Redelivery of the same event, now that the (hypothetical) bug is fixed.
    payment_adapter.receive_update.return_value = (
        "ext_99999",
        PaymentStatusUpdateDataclass(
            id=None, status="approved", description="ok", update_external_id="notif-3"
        ),
    )
    second = payment_service.handle_payment_webhook(
        PaymentProviders.MERCADOPAGO, raw_body, headers, payload
    )

    assert second is not None
    assert payment_adapter.receive_update.call_count == 2
    assert ProviderWebhookEvent.objects.count() == 1
    event.refresh_from_db()
    assert event.processed_at is not None
    payment.refresh_from_db()
    assert payment.status_updates.count() == 1


@pytest.mark.django_db
def test_handle_subscription_payment_webhook_none_result_does_not_burn_the_delivery(
    payment_service, subscription_adapter
):
    subscription_adapter.get_event_id.return_value = "notif-4"
    subscription_adapter.receive_payment_update.return_value = None
    payload = {"type": "subscription_authorized_payment", "id": "notif-4"}
    raw_body = b"{}"
    headers: dict[str, str] = {}

    first = payment_service.handle_subscription_payment_webhook(
        PaymentProviders.MERCADOPAGO, raw_body, headers, payload
    )
    second = payment_service.handle_subscription_payment_webhook(
        PaymentProviders.MERCADOPAGO, raw_body, headers, payload
    )

    assert first is None
    assert second is None
    # Not marked processed either time — still retriable, not call-count-limited.
    assert subscription_adapter.receive_payment_update.call_count == 2
    assert ProviderWebhookEvent.objects.count() == 1


# ---------------------------------------------------------------------------
# BillingProfile.payment_provider pin — SubscriptionService.record_payment_method
# / SubscriptionService.set_payment_provider (Payment Provider Selection, Phase 2)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_record_payment_method_sets_pin_on_first_call(subscription_service, billing_profile):
    """The first confirmed instrument for an organization pins its BillingProfile
    to the provider that confirmed it."""
    assert billing_profile.payment_provider == ""

    subscription_service.record_payment_method(
        billing_profile.organization, PaymentProviders.MERCADOPAGO, "instrument_1"
    )

    billing_profile.refresh_from_db()
    assert billing_profile.payment_provider == PaymentProviders.MERCADOPAGO


@pytest.mark.django_db
def test_record_payment_method_second_call_different_provider_leaves_pin_unchanged_and_logs(
    subscription_service, billing_profile, caplog
):
    """A second confirmed instrument at a *different* provider must not repoint
    the organization's pin -- the discrepancy is logged instead so it surfaces
    rather than silently moving future charges onto a provider the org never
    explicitly settled on."""
    subscription_service.record_payment_method(
        billing_profile.organization, PaymentProviders.MERCADOPAGO, "instrument_1"
    )
    billing_profile.refresh_from_db()
    assert billing_profile.payment_provider == PaymentProviders.MERCADOPAGO

    with caplog.at_level("WARNING", logger="payments.services.subscription_service"):
        subscription_service.record_payment_method(
            billing_profile.organization, PaymentProviders.STRIPE, "instrument_2"
        )

    billing_profile.refresh_from_db()
    assert billing_profile.payment_provider == PaymentProviders.MERCADOPAGO
    assert "already pinned" in caplog.text


@pytest.mark.django_db
def test_record_payment_method_pin_write_is_a_conditional_update_not_read_then_write(
    subscription_service, billing_profile, caplog
):
    """The pin write must be a single conditional ``UPDATE ... WHERE
    payment_provider = ''``, not a Python-level "is it empty" branch followed by
    an unconditional ``save()``. Two concurrent ``record_payment_method`` calls
    for the same organization at different providers, each inside its own
    transaction, would otherwise both observe an empty pin in memory and both
    write -- the second silently overwriting the first with no warning, because
    its own in-memory check also saw an empty pin.

    Proven structurally rather than by outcome: re-running the sequential case
    (see ``test_record_payment_method_second_call_different_provider_leaves_pin_unchanged_and_logs``)
    cannot tell an atomic conditional write apart from a read-then-write that
    happens to run sequentially in a single-threaded test -- both produce the
    same final state. This spies on ``QuerySet.update`` to assert the write is
    actually issued as a conditional ``UPDATE`` against ``BillingProfile``, that
    it reports zero affected rows once the row is already pinned to a different
    provider, and that the zero-row result is what drives the warning -- exactly
    the observable a real race would produce for the losing caller.
    """
    from django.db.models.query import QuerySet

    from payments.models import BillingProfile as BillingProfileModel

    billing_profile.payment_provider = PaymentProviders.MERCADOPAGO
    billing_profile.save(update_fields=["payment_provider"])

    calls: list[tuple[dict, int]] = []
    original_update = QuerySet.update

    def spying_update(self, **kwargs):
        result = original_update(self, **kwargs)
        if self.model is BillingProfileModel:
            calls.append((kwargs, result))
        return result

    with patch.object(QuerySet, "update", spying_update):
        with caplog.at_level("WARNING", logger="payments.services.subscription_service"):
            subscription_service.record_payment_method(
                billing_profile.organization, PaymentProviders.STRIPE, "instrument_2"
            )

    assert calls, "record_payment_method must pin via a QuerySet.update() call, not save()"
    kwargs, affected_rows = calls[-1]
    assert kwargs == {"payment_provider": PaymentProviders.STRIPE}
    assert affected_rows == 0
    assert "already pinned" in caplog.text
    billing_profile.refresh_from_db()
    assert billing_profile.payment_provider == PaymentProviders.MERCADOPAGO


@pytest.mark.django_db
def test_set_payment_provider_rejects_unknown_slug(subscription_service, organization):
    with pytest.raises(UnknownPaymentProviderError):
        subscription_service.set_payment_provider(organization, "not_a_real_provider")


@pytest.mark.django_db
def test_set_payment_provider_requires_billing_profile(subscription_service, organization):
    with pytest.raises(MissingBillingProfileError):
        subscription_service.set_payment_provider(organization, PaymentProviders.STRIPE)


@pytest.mark.django_db
def test_set_payment_provider_writes_audit_entry_naming_previous_provider(
    subscription_service, billing_profile, django_capture_on_commit_callbacks
):
    billing_profile.payment_provider = PaymentProviders.MERCADOPAGO
    billing_profile.save(update_fields=["payment_provider"])

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            subscription_service.set_payment_provider(
                billing_profile.organization, PaymentProviders.STRIPE
            )

    billing_profile.refresh_from_db()
    assert billing_profile.payment_provider == PaymentProviders.STRIPE

    assert mock_task.delay.call_count == 1
    payload = mock_task.delay.call_args_list[0].args[0]
    assert payload["organization_id"] == billing_profile.organization_id
    assert payload["action"] == AuditAction.UPDATE
    assert payload["subject"]["subject_type"] == "payments.BillingProfile"
    assert payload["subject"]["subject_id"] == str(billing_profile.pk)
    assert payload["diff"] == {
        "payment_provider": {
            "old": PaymentProviders.MERCADOPAGO,
            "new": PaymentProviders.STRIPE,
        }
    }


@pytest.mark.django_db
def test_set_payment_provider_succeeds_with_active_subscription_at_old_provider(
    subscription_service, billing_profile, billing_plan
):
    """Deliberate absence of an active-subscription guard.

    ``set_payment_provider`` must repoint an organization's BillingProfile even
    while it holds a live Subscription at the provider being replaced. This is
    not an oversight -- it is the plan's explicit **Pin mutability** guiding
    decision (see the Payment Provider Selection implementation plan and the
    resolved "no guard" entry in its Open Questions table): the lever exists
    precisely for the migrate-a-customer-off-a-provider case, and a guard would
    block it in exactly that scenario. A future reviewer must not reinstate a
    guard here without revisiting that decision first.
    """
    billing_profile.payment_provider = PaymentProviders.MERCADOPAGO
    billing_profile.save(update_fields=["payment_provider"])
    now = datetime.datetime.now(tz=datetime.UTC)
    subscription = baker.make(
        SubscriptionModel,
        organization=billing_profile.organization,
        plan=billing_plan,
        billing_state=BillingState.ACTIVE,
        payment_provider=PaymentProviders.MERCADOPAGO,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )
    assert subscription.billing_state == BillingState.ACTIVE

    result = subscription_service.set_payment_provider(
        billing_profile.organization, PaymentProviders.STRIPE
    )

    assert result.payment_provider == PaymentProviders.STRIPE
    billing_profile.refresh_from_db()
    assert billing_profile.payment_provider == PaymentProviders.STRIPE


@pytest.mark.django_db
def test_set_payment_provider_records_actor_from_user(
    subscription_service, billing_profile, django_capture_on_commit_callbacks
):
    """Passing ``actor`` (as ``BillingProfileAdmin.save_model`` does with
    ``request.user``) must name that staff member in the audit entry as a
    MEMBERSHIP actor, not the generic SYSTEM actor every other caller gets."""
    from audit.constants import AuditActorType
    from organizations.models import OrganizationMembership, OrganizationRole

    staff_user = baker.make("users.User")
    membership = grant_membership_groups(
        OrganizationMembership.objects.create(
            user=staff_user,
            organization=billing_profile.organization,
            role=OrganizationRole.ADMIN,
        )
    )

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            subscription_service.set_payment_provider(
                billing_profile.organization, PaymentProviders.STRIPE, actor=staff_user
            )

    assert mock_task.delay.call_count == 1
    payload = mock_task.delay.call_args_list[0].args[0]
    assert payload["actor"]["actor_type"] == AuditActorType.MEMBERSHIP
    assert payload["actor"]["actor_id"] == membership.user_id
    assert payload["actor"]["actor_role"] == OrganizationRole.ADMIN


@pytest.mark.django_db
def test_set_payment_provider_empty_string_unpins_without_raising(
    subscription_service, billing_profile
):
    """``provider=""`` is a legitimate un-pin (the admin's cleared ``<select>``
    submits it), not a slug ``UnknownPaymentProviderError`` should reject."""
    billing_profile.payment_provider = PaymentProviders.MERCADOPAGO
    billing_profile.save(update_fields=["payment_provider"])

    result = subscription_service.set_payment_provider(billing_profile.organization, "")

    assert result.payment_provider == ""
    billing_profile.refresh_from_db()
    assert billing_profile.payment_provider == ""


# ---------------------------------------------------------------------------
# Rule A across every existing-row operation.
#
# `test_mercadopago_payment_is_refunded_and_status_checked_via_mercadopago_even_when_org_pin_is_stripe`
# above covers `create_refund`/`check_refund_status`/`check_payment_status`.
# These cover the four that had no disagreeing-provider coverage:
# `process_payment`, `process_subscription`, `change_subscription_plan`,
# `cancel_subscription` -- which is precisely where the hardcoded
# `payment_provider=mercadopago` in `create_subscription_for_organization` hid.
# Every one of them pins the organization to `stripe` and the row to
# `mercadopago`, and asserts the Stripe adapter is never touched.
# ---------------------------------------------------------------------------


@pytest.fixture
def stripe_pinned_billing_profile(billing_profile):
    billing_profile.payment_provider = PaymentProviders.STRIPE
    billing_profile.save(update_fields=["payment_provider"])
    return billing_profile


def _mercadopago_subscription_for(billing_profile, billing_plan) -> SubscriptionModel:
    """A ``Subscription`` row stamped ``mercadopago`` -- built directly rather
    than through ``SubscriptionService`` so the stamped provider is unambiguously
    the test's own choice, not whatever the service resolved."""
    return baker.make(
        SubscriptionModel,
        organization=billing_profile.organization,
        plan=billing_plan,
        status=SubscriptionStatuses.ACTIVE,
        external_id="mp-sub-1",
        payment_provider=PaymentProviders.MERCADOPAGO,
        current_period_start=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        current_period_end=datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC),
    )


@pytest.mark.django_db
def test_process_payment_drives_the_payment_rows_provider_not_the_org_pin(
    payment_service, payment_adapter, stripe_payment_adapter, stripe_pinned_billing_profile
):
    payment = baker.make(
        "payments.Payment",
        billing_profile=stripe_pinned_billing_profile,
        value=Decimal("100"),
        currency="USD",
        payment_provider=PaymentProviders.MERCADOPAGO,
        status=PaymentStatuses.PENDING_SEND,
        payment_method="credit_card",
        description="Test Payment",
    )
    payment_adapter.process.return_value = "mp-payment-processed"

    result = payment_service.process_payment(payment, "card_token_1")

    assert result.external_id == "mp-payment-processed"
    payment_adapter.process.assert_called_once()
    stripe_payment_adapter.process.assert_not_called()


@pytest.mark.django_db
def test_process_subscription_drives_the_subscription_rows_provider_not_the_org_pin(
    payment_service,
    subscription_adapter,
    stripe_subscription_adapter,
    stripe_pinned_billing_profile,
    billing_plan,
):
    subscription = _mercadopago_subscription_for(stripe_pinned_billing_profile, billing_plan)
    subscription_adapter.create_subscription.return_value = "mp-sub-created"

    result = payment_service.process_subscription(subscription, "card_token_1")

    assert result.external_id == "mp-sub-created"
    subscription_adapter.create_subscription.assert_called_once()
    stripe_subscription_adapter.create_subscription.assert_not_called()


@pytest.mark.django_db
def test_change_subscription_plan_drives_the_subscription_rows_provider_not_the_org_pin(
    payment_service,
    subscription_adapter,
    stripe_subscription_adapter,
    stripe_pinned_billing_profile,
    billing_plan,
):
    subscription = _mercadopago_subscription_for(stripe_pinned_billing_profile, billing_plan)
    new_plan = CreatedPlan(
        id=1,
        name="Pro",
        value=Decimal("50"),
        currency="USD",
        billing_day=1,
        billing_interval=BillingInterval.MONTHLY,
        external_id="mp-plan-1",
    )

    payment_service.change_subscription_plan(subscription, new_plan, idempotency_key="idem-1")

    subscription_adapter.change_subscription_plan.assert_called_once()
    stripe_subscription_adapter.change_subscription_plan.assert_not_called()


@pytest.mark.django_db
def test_cancel_subscription_drives_the_subscription_rows_provider_not_the_org_pin(
    payment_service,
    subscription_adapter,
    stripe_subscription_adapter,
    stripe_pinned_billing_profile,
    billing_plan,
):
    subscription = _mercadopago_subscription_for(stripe_pinned_billing_profile, billing_plan)

    payment_service.cancel_subscription(subscription)

    subscription_adapter.cancel_subscription.assert_called_once()
    stripe_subscription_adapter.cancel_subscription.assert_not_called()
    subscription.refresh_from_db()
    assert subscription.status == SubscriptionStatuses.CANCELLED


@pytest.mark.django_db
def test_create_subscription_plan_drives_the_provider_it_is_given_not_the_org_pin(
    payment_service,
    subscription_adapter,
    stripe_subscription_adapter,
    stripe_pinned_billing_profile,
):
    """``_ensure_provider_plan`` passes ``subscription.payment_provider``; this
    asserts ``PaymentService`` honors that argument rather than re-resolving."""
    subscription_adapter.create_subscription_plan.return_value = "mp-plan-1"

    created = payment_service.create_subscription_plan(
        PlanDataclass(
            id=1,
            name="Pro",
            value=Decimal("50"),
            currency="USD",
            billing_day=1,
            billing_interval=BillingInterval.MONTHLY,
        ),
        provider=PaymentProviders.MERCADOPAGO,
    )

    assert created.external_id == "mp-plan-1"
    subscription_adapter.create_subscription_plan.assert_called_once()
    stripe_subscription_adapter.create_subscription_plan.assert_not_called()


# ---------------------------------------------------------------------------
# Adapter-resolution split: registry-only vs. credential-asserting.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_get_payment_adapter_ignores_the_outbound_credential(
    subscription_plan_factory, payment_adapter, subscription_adapter, di_container
):
    """The registry-only variant is what the inbound webhook path calls. It must
    resolve a registered provider whose outbound credential is empty, or every
    webhook delivery for that provider 500s and the provider retries forever."""
    unconfigured_stripe = StripePaymentAdapter(api_key="", webhook_secret="whsec_x")
    with (
        di_container.payment_gateway.override(payment_adapter),
        di_container.subscription_gateway.override(subscription_adapter),
        di_container.stripe_payment_gateway.override(unconfigured_stripe),
    ):
        payment_service = PaymentService(subscription_plan_factory=subscription_plan_factory)

        assert payment_service.get_payment_adapter(PaymentProviders.STRIPE) is unconfigured_stripe
        with pytest.raises(PaymentProviderNotConfiguredError):
            payment_service.get_configured_payment_adapter(PaymentProviders.STRIPE)


@pytest.mark.django_db
def test_get_subscription_adapter_ignores_the_outbound_credential(
    subscription_plan_factory, payment_adapter, subscription_adapter, di_container
):
    """Subscription-side counterpart of the test above."""
    unconfigured_stripe = StripeSubscriptionAdapter(api_key="", webhook_secret="whsec_x")
    with (
        di_container.payment_gateway.override(payment_adapter),
        di_container.subscription_gateway.override(subscription_adapter),
        di_container.stripe_subscription_gateway.override(unconfigured_stripe),
    ):
        payment_service = PaymentService(subscription_plan_factory=subscription_plan_factory)

        assert (
            payment_service.get_subscription_adapter(PaymentProviders.STRIPE) is unconfigured_stripe
        )
        with pytest.raises(PaymentProviderNotConfiguredError):
            payment_service.get_configured_subscription_adapter(PaymentProviders.STRIPE)
        with pytest.raises(PaymentProviderNotConfiguredError):
            payment_service.assert_subscription_provider_configured(PaymentProviders.STRIPE)


@pytest.mark.django_db
def test_configured_adapter_raises_unknown_before_not_configured(payment_service):
    """The Unknown/NotConfigured distinction survives the credential assertion: a
    slug that is not a provider at all is a data error, not a deployment one."""
    with pytest.raises(UnknownPaymentProviderError):
        payment_service.get_configured_payment_adapter("not_a_real_provider")
    with pytest.raises(UnknownPaymentProviderError):
        payment_service.get_configured_subscription_adapter("not_a_real_provider")


@pytest.mark.django_db
def test_create_refund_does_not_mislabel_a_local_data_error_as_a_provider_decline(
    payment_service, payment_adapter, organization, billing_address
):
    """``_serialize_payment`` raises ``BillingProfileContactEmailMissingError``
    for a profile with no billing contact -- a local data problem, not a refund
    the provider rejected. Serialized above ``create_refund``'s broad
    ``except Exception``, so it propagates instead of being recorded as a
    provider-declined ``FAILED`` refund (the exact mislabelling the pre-fetch of
    the adapter was added to avoid)."""
    from payments.exceptions import BillingProfileContactEmailMissingError
    from payments.models import BillingProfile as BillingProfileModel
    from payments.models import Refund as RefundModelForTest

    billing_profile = BillingProfileModel.objects.create(
        organization=organization,
        contact_first_name="Ada",
        contact_email="",
        document_type="CPF",
        document_number="12345678900",
        billing_address=billing_address,
        payment_provider=PaymentProviders.MERCADOPAGO,
    )
    payment = baker.make(
        "payments.Payment",
        billing_profile=billing_profile,
        value=Decimal("100"),
        currency="USD",
        payment_provider=PaymentProviders.MERCADOPAGO,
        status=PaymentStatuses.APPROVED,
        payment_method="credit_card",
        external_id="ext-1",
    )

    with pytest.raises(BillingProfileContactEmailMissingError):
        payment_service.create_refund(payment_id=payment.pk, value=Decimal("100"), currency="USD")

    payment_adapter.refund.assert_not_called()
    assert not RefundModelForTest.objects.filter(
        payment=payment, status=RefundStatuses.FAILED
    ).exists()
