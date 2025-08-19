from decimal import Decimal
from unittest.mock import Mock, patch

from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

import pytest

from payments.constants import PaymentProviders
from payments.services.dataclasses import (
    BillingAddress,
    BillingProfile,
    CreatedPlan,
    PaymentStatusUpdate,
    Plan,
    Subscription,
    SubscriptionPayment,
)
from payments.services.subscription_adapters.mercadopago_subscription_adapter import (
    MercadoPagoSubscriptionAdapter,
)


@pytest.fixture
def mock_billing_address():
    """Mock billing address object."""
    address = Mock(spec=BillingAddress)
    address.id = "address-123"
    address.street_name = "Test Street"
    address.street_number = "123"
    address.neighborhood = "Test Neighborhood"
    address.city = "Test City"
    address.state = "Test State"
    address.country = "BR"
    address.zip_code = "12345-678"
    address.address_line_2 = ""
    return address


@pytest.fixture
def mock_billing_profile(mock_billing_address):
    """Mock billing profile object."""
    profile = Mock(spec=BillingProfile)
    profile.pk = 123
    profile.email = "test@example.com"
    profile.document_type = "CPF"
    profile.document_number = "12345678901"
    profile.first_name = "John"
    profile.last_name = "Doe"
    profile.billing_address = mock_billing_address
    profile.phone = "+5511999999999"
    return profile


@pytest.fixture
def mock_plan():
    """Mock plan object."""
    plan = Mock(spec=Plan)
    plan.id = "plan-123"
    plan.name = "Test Plan"
    plan.value = Decimal("99.90")
    plan.currency = "BRL"
    plan.billing_day = 15
    return plan


@pytest.fixture
def mock_created_plan():
    """Mock created plan object."""
    plan = Mock(spec=CreatedPlan)
    plan.id = "plan-123"
    plan.external_id = "mp-plan-456"
    plan.name = "Test Plan"
    plan.value = Decimal("99.90")
    plan.currency = "BRL"
    plan.billing_day = 15
    return plan


@pytest.fixture
def mock_subscription(mock_plan, mock_billing_profile):
    """Mock subscription object."""
    subscription = Mock(spec=Subscription)
    subscription.id = "subscription-123"
    subscription.external_id = "mp-subscription-456"
    subscription.plan = mock_plan
    subscription.plan.external_id = "mp-plan-456"
    subscription.billing_profile = mock_billing_profile
    return subscription


@pytest.fixture
def adapter():
    """Create MercadoPagoSubscriptionAdapter instance with mocked SDK."""
    with patch(
        "payments.services.subscription_adapters.mercadopago_subscription_adapter.mercadopago.SDK"
    ) as mock_sdk:
        adapter = MercadoPagoSubscriptionAdapter("test-access-token")
        adapter.sdk = mock_sdk.return_value
        return adapter


def test_init():
    """Test adapter initialization."""
    with patch(
        "payments.services.subscription_adapters.mercadopago_subscription_adapter.mercadopago.SDK"
    ) as mock_sdk:
        adapter = MercadoPagoSubscriptionAdapter("test-token")
        mock_sdk.assert_called_once_with("test-token")
        assert adapter.provider == PaymentProviders.MERCADOPAGO


def test_create_subscription_plan(adapter, mock_plan):
    """Test creating a subscription plan."""
    adapter.sdk.plan().create.return_value = {"response": {"id": "mp-plan-456"}}

    result = adapter.create_subscription_plan(mock_plan)

    assert result == "mp-plan-456"
    expected_plan_data = {
        "reason": "Test Plan",
        "external_reference": "plan-123",
        "auto_recurring": {
            "frequency": 1,
            "frequency_type": "months",
            "transaction_amount": "99.90",
            "currency_id": "BRL",
            "billing_day": 15,
            "billing_day_proportional": True,
        },
        "payment_methods_allowed": {
            "payment_types": ["credit_card"],
            "payment_methods": ["master", "visa", "amex", "diners"],
        },
    }
    adapter.sdk.plan().create.assert_called_once_with(expected_plan_data)


def test_update_subscription_plan(adapter, mock_plan):
    """Test updating a subscription plan."""
    adapter.sdk.plan().update.return_value = {"response": {"id": "mp-plan-456"}}

    result = adapter.update_subscription_plan("mp-plan-456", mock_plan)

    assert result == "mp-plan-456"
    expected_plan_data = {
        "reason": "Test Plan",
        "external_reference": "plan-123",
        "auto_recurring": {
            "frequency": 1,
            "frequency_type": "months",
            "transaction_amount": "99.90",
            "currency_id": "BRL",
            "billing_day": 15,
            "billing_day_proportional": True,
        },
        "payment_methods_allowed": {
            "payment_types": ["credit_card"],
            "payment_methods": ["master", "visa", "amex", "diners"],
        },
    }
    adapter.sdk.plan().update.assert_called_once_with("mp-plan-456", expected_plan_data)


@override_settings(SITE_DOMAIN="example.com")
@patch("payments.services.subscription_adapters.mercadopago_subscription_adapter.reverse")
def test_create_subscription_success(mock_reverse, adapter, mock_subscription):
    """Test successful subscription creation."""
    mock_reverse.return_value = (
        "/api/payments/subscription-payment-update/mercadopago/subscription-123/"
    )
    adapter.sdk.preapproval().create.return_value = {"response": {"id": "mp-subscription-456"}}

    result = adapter.create_subscription(mock_subscription, "test-token")

    assert result == "mp-subscription-456"
    expected_subscription_data = {
        "payer_email": "test@example.com",
        "preapproval_plan_id": "mp-plan-456",
        "back_url": "https://example.com/subscription/subscription-123/success",
        "external_reference": "subscription-123",
        "card_token_id": "test-token",
        "status": "authorized",
        "notification_url": "https://example.com/api/payments/subscription-payment-update/mercadopago/subscription-123/",
    }
    adapter.sdk.preapproval().create.assert_called_once_with(expected_subscription_data)


@override_settings(SITE_DOMAIN=None)
def test_create_subscription_missing_site_domain(adapter, mock_subscription):
    """Test create subscription raises error when SITE_DOMAIN is not configured."""
    with pytest.raises(ImproperlyConfigured, match="MercadoPagoAdapter requires SITE_DOMAIN"):
        adapter.create_subscription(mock_subscription, "test-token")


@override_settings(SITE_DOMAIN="example.com")
def test_cancel_subscription_success(adapter, mock_subscription):
    """Test successful subscription cancellation."""
    adapter.cancel_subscription(mock_subscription)

    expected_update_data = {
        "back_url": "https://example.com/subscription/subscription-123/cancelled",
        "external_reference": "subscription-123",
        "status": "cancelled",
    }
    adapter.sdk.preapproval().update.assert_called_once_with(
        "mp-subscription-456", expected_update_data
    )


@override_settings(SITE_DOMAIN=None)
def test_cancel_subscription_missing_site_domain(adapter, mock_subscription):
    """Test cancel subscription raises error when SITE_DOMAIN is not configured."""
    with pytest.raises(ImproperlyConfigured, match="MercadoPagoAdapter requires SITE_DOMAIN"):
        adapter.cancel_subscription(mock_subscription)


@override_settings(SITE_DOMAIN="example.com")
def test_update_subscription_payment_token_success(adapter, mock_subscription):
    """Test successful subscription payment token update."""
    adapter.update_subscription_payment_token(mock_subscription, "new-token")

    expected_update_data = {
        "back_url": "https://example.com/subscription/subscription-123/cancelled",
        "external_reference": "subscription-123",
        "card_token_id": "new-token",
        "status": "authorized",
    }
    adapter.sdk.preapproval().update.assert_called_once_with(
        "mp-subscription-456", expected_update_data
    )


@override_settings(SITE_DOMAIN=None)
def test_update_subscription_payment_token_missing_site_domain(adapter, mock_subscription):
    """Test update payment token raises error when SITE_DOMAIN is not configured."""
    with pytest.raises(ImproperlyConfigured, match="MercadoPagoAdapter requires SITE_DOMAIN"):
        adapter.update_subscription_payment_token(mock_subscription, "new-token")


def test_update_plan(adapter, mock_created_plan):
    """Test updating a created plan."""
    result = adapter.update_plan(mock_created_plan)

    assert result == mock_created_plan
    expected_plan_data = {
        "reason": "Test Plan",
        "external_reference": "plan-123",
        "auto_recurring": {
            "frequency": 1,
            "frequency_type": "months",
            "transaction_amount": "99.90",
            "currency_id": "BRL",
            "billing_day": 15,
            "billing_day_proportional": True,
        },
        "payment_methods_allowed": {
            "payment_types": ["credit_card"],
            "payment_methods": ["master", "visa", "amex", "diners"],
        },
    }
    adapter.sdk.plan().update.assert_called_once_with("mp-plan-456", expected_plan_data)


def test_get_subscription_external_id_from_update(adapter):
    """Test extracting subscription external ID from update payload."""
    update_payload = {"data": {"id": "mp-subscription-456"}, "other_field": "value"}

    result = adapter.get_subscription_external_id_from_update(update_payload)
    assert result == "mp-subscription-456"


def test_get_subscription_external_id_from_update_missing_data(adapter):
    """Test extracting subscription external ID when data is missing."""
    update_payload = {"other_field": "value"}

    result = adapter.get_subscription_external_id_from_update(update_payload)
    assert result is None


def test_get_update_id(adapter):
    """Test extracting update ID from payload."""
    update_payload = {"id": "update-123", "other_field": "value"}

    result = adapter.get_update_id(update_payload)
    assert result == "update-123"


def test_get_update_id_missing(adapter):
    """Test extracting update ID when missing."""
    update_payload = {"other_field": "value"}

    result = adapter.get_update_id(update_payload)
    assert result is None


def test_get_payment_payload(adapter):
    """Test getting payment payload."""
    expected_payload = {"response": {"id": "payment-123"}}
    adapter.sdk.payment().get.return_value = expected_payload

    result = adapter.get_payment_payload("payment-123")

    assert result == expected_payload
    adapter.sdk.payment().get.assert_called_once_with("payment-123")


def test_create_subscription_payment_from_payment_payload(adapter):
    """Test creating subscription payment from payment payload."""
    payment_payload = {
        "response": {
            "id": "payment-456",
            "transaction_amount": "99.90",
            "currency_id": "BRL",
            "status": "approved",
            "payment_method_id": "visa",
            "description": "Subscription payment",
            "payer": {
                "email": "test@example.com",
                "first_name": "John",
                "last_name": "Doe",
                "identification": {"type": "CPF", "number": "12345678901"},
                "address": {
                    "street_name": "Test Street",
                    "street_number": "123",
                    "neighborhood": "Test Neighborhood",
                    "city": "Test City",
                    "federal_unit": "Test State",
                    "country": "BR",
                    "zip_code": "12345-678",
                },
            },
        }
    }

    result = adapter.create_subscription_payment_from_payment_payload(
        "subscription-123", payment_payload
    )

    assert isinstance(result, SubscriptionPayment)
    assert result.id is None
    assert result.subscription_external_id == "subscription-123"
    assert result.external_id == "payment-456"
    assert result.value == "99.90"
    assert result.currency == "BRL"
    assert result.payment_provider == "mercadopago"
    assert result.status == "approved"
    assert result.payment_method == "visa"
    assert result.description == "Subscription payment"
    assert result.status_updates == []

    # Check billing profile
    assert result.billing_profile.email == "test@example.com"
    assert result.billing_profile.first_name == "John"
    assert result.billing_profile.last_name == "Doe"
    assert result.billing_profile.document_type == "CPF"
    assert result.billing_profile.document_number == "12345678901"
    assert result.billing_profile.pk is None
    assert result.billing_profile.phone is None

    # Check billing address
    assert result.billing_profile.billing_address.id is None
    assert result.billing_profile.billing_address.street_name == "Test Street"
    assert result.billing_profile.billing_address.street_number == "123"
    assert result.billing_profile.billing_address.neighborhood == "Test Neighborhood"
    assert result.billing_profile.billing_address.city == "Test City"
    assert result.billing_profile.billing_address.state == "Test State"
    assert result.billing_profile.billing_address.country == "BR"
    assert result.billing_profile.billing_address.zip_code == "12345-678"
    assert result.billing_profile.billing_address.address_line_2 == ""


def test_create_status_update_from_payment_payload(adapter):
    """Test creating status update from payment payload."""
    payment_payload = {
        "id": "123",
        "response": {"id": "payment-456", "status": "approved", "status_detail": "accredited"},
    }

    result = adapter.create_status_update_from_payment_payload(payment_payload)

    assert isinstance(result, PaymentStatusUpdate)
    assert result.id == 123
    assert result.status == "approved"
    assert result.description == "accredited"
    assert result.update_external_id == "payment-456"


def test_create_status_update_from_payment_payload_no_update_id(adapter):
    """Test creating status update when update ID is missing."""
    payment_payload = {
        "response": {"id": "payment-456", "status": "approved", "status_detail": "accredited"}
    }

    result = adapter.create_status_update_from_payment_payload(payment_payload)

    assert isinstance(result, PaymentStatusUpdate)
    assert result.id is None
    assert result.status == "approved"
    assert result.description == "accredited"
    assert result.update_external_id == "payment-456"


def test_is_payment_update_true(adapter):
    """Test payment update detection returns true for subscription authorized payment."""
    update_payload = {"type": "subscription_authorized_payment"}

    result = adapter.is_payment_update(update_payload)
    assert result is True


def test_is_payment_update_false(adapter):
    """Test payment update detection returns false for other types."""
    update_payload = {"type": "payment"}

    result = adapter.is_payment_update(update_payload)
    assert result is False


def test_get_subscription_payload(adapter):
    """Test getting subscription payload."""
    expected_payload = {"response": {"id": "subscription-123"}}
    adapter.sdk.preapproval().get.return_value = expected_payload

    result = adapter.get_subscription_payload("subscription-123")

    assert result == expected_payload
    adapter.sdk.preapproval().get.assert_called_once_with("subscription-123")


def test_get_payment_external_id_from_subscription_payload(adapter):
    """Test extracting payment external ID from subscription payload."""
    subscription_payload = {"response": {"last_payment_id": "payment-456"}, "other_field": "value"}

    result = adapter.get_payment_external_id_from_subscription_payload(subscription_payload)
    assert result == "payment-456"


def test_get_payment_external_id_from_subscription_payload_missing_data(adapter):
    """Test extracting payment external ID when data is missing."""
    subscription_payload = {"other_field": "value"}

    result = adapter.get_payment_external_id_from_subscription_payload(subscription_payload)
    assert result is None


def test_get_payment_external_id_from_subscription_payload_missing_payment_id(adapter):
    """Test extracting payment external ID when last_payment_id is missing."""
    subscription_payload = {"response": {"other_field": "value"}}

    result = adapter.get_payment_external_id_from_subscription_payload(subscription_payload)
    assert result is None
