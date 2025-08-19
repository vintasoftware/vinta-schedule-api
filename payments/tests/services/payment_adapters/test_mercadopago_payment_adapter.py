import json
from decimal import Decimal
from unittest.mock import Mock, patch

from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

import pytest

from payments.constants import PaymentProviders, RefundStatuses
from payments.services.dataclasses import Refund
from payments.services.payment_adapters.base import Payment, PaymentStatusUpdate
from payments.services.payment_adapters.mercadopago_payment_adapter import (
    MercadoPagoPaymentAdapter,
)


@pytest.fixture
def mock_billing_address():
    """Mock billing address object."""
    address = Mock()
    address.street_name = "Test Street"
    address.street_number = "123"
    address.neighborhood = "Test Neighborhood"
    address.city = "Test City"
    address.state = "Test State"
    address.country = "BR"
    address.zip_code = "12345-678"
    return address


@pytest.fixture
def mock_billing_profile(mock_billing_address):
    """Mock billing profile object."""
    profile = Mock()
    profile.email = "test@example.com"
    profile.document_type = "CPF"
    profile.document_number = "12345678901"
    profile.first_name = "John"
    profile.last_name = "Doe"
    profile.billing_address = mock_billing_address
    return profile


@pytest.fixture
def mock_payment(mock_billing_profile):
    """Mock payment object."""
    payment = Mock(spec=Payment)
    payment.id = "payment-123"
    payment.value = Decimal("100.50")
    payment.description = "Test payment"
    payment.payment_method = "credit_card"
    payment.billing_profile = mock_billing_profile
    payment.external_id = "mp-payment-456"
    return payment


@pytest.fixture
def mock_refund(mock_payment):
    """Mock refund object."""
    refund = Mock(spec=Refund)
    refund.id = "refund-123"
    refund.value = Decimal("50.25")
    refund.payment = mock_payment
    return refund


@pytest.fixture
def adapter():
    """Create MercadoPagoAdapter instance with mocked SDK."""
    with patch(
        "payments.services.payment_adapters.mercadopago_payment_adapter.mercadopago.SDK"
    ) as mock_sdk:
        adapter = MercadoPagoPaymentAdapter("test-access-token")
        adapter.sdk = mock_sdk.return_value
        return adapter


def test_init():
    """Test adapter initialization."""
    with patch(
        "payments.services.payment_adapters.mercadopago_payment_adapter.mercadopago.SDK"
    ) as mock_sdk:
        adapter = MercadoPagoPaymentAdapter("test-token")
        mock_sdk.assert_called_once_with("test-token")
        assert adapter.provider == PaymentProviders.MERCADOPAGO


@patch("payments.services.payment_adapters.mercadopago_payment_adapter.reverse")
@patch(
    "payments.services.payment_adapters.mercadopago_payment_adapter.mercadopago.config.RequestOptions"
)
@override_settings(SITE_DOMAIN="example.com")
def test_process_success(mock_request_options, mock_reverse, adapter, mock_payment):
    """Test successful payment processing."""
    # Setup mocks
    mock_reverse.return_value = "/payment/update/mercadopago/payment-123/"
    mock_options = Mock()
    mock_request_options.return_value = mock_options

    adapter.sdk.payment().create.return_value = {"response": {"id": "mp-payment-456"}}

    # Execute
    result = adapter.process(mock_payment, "test-token")

    # Verify
    assert result == "mp-payment-456"
    mock_options.custom_headers = {"x-idempotency-key": {"payment-123"}}

    expected_payment_data = {
        "transaction_amount": "100.50",
        "token": "test-token",
        "description": "Test payment",
        "payment_method_id": "credit_card",
        "notification_url": "https://example.com/payment/update/mercadopago/payment-123/",
        "installments": 1,
        "payer": {
            "email": "test@example.com",
            "identification": {
                "type": "CPF",
                "number": "12345678901",
            },
            "first_name": "John",
            "last_name": "Doe",
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
    adapter.sdk.payment().create.assert_called_once_with(expected_payment_data, mock_options)


@override_settings(SITE_DOMAIN=None)
def test_process_missing_site_domain(adapter, mock_payment):
    """Test process raises error when SITE_DOMAIN is not configured."""
    with pytest.raises(ImproperlyConfigured, match="MercadoPagoAdapter requires SITE_DOMAIN"):
        adapter.process(mock_payment, "test-token")


@patch(
    "payments.services.payment_adapters.mercadopago_payment_adapter.mercadopago.config.RequestOptions"
)
def test_refund_success(mock_request_options, adapter, mock_refund):
    """Test successful refund processing."""
    # Setup mocks
    mock_options = Mock()
    mock_request_options.return_value = mock_options

    adapter.sdk.refund().create.return_value = {"response": {"id": "refund-456"}}

    # Execute
    result = adapter.refund(mock_refund)

    # Verify
    assert result == "refund-456"
    mock_options.custom_headers = {"x-idempotency-key": {"refund-123"}}
    adapter.sdk.refund().create.assert_called_once_with(
        "mp-payment-456", {"amount": "50.25"}, mock_options
    )


def test_check_status(adapter):
    """Test payment status checking."""
    # Setup mock
    adapter.sdk.payment().get.return_value = {
        "response": {"status": "approved", "status_detail": "accredited"}
    }

    # Execute
    result = adapter.check_status("mp-payment-456", "update-123")

    # Verify
    assert isinstance(result, PaymentStatusUpdate)
    assert result.id is None
    assert result.status == "approved"
    assert result.description == "accredited"
    assert result.update_external_id == "update-123"
    adapter.sdk.payment().get.assert_called_once_with("mp-payment-456")


def test_get_payment_external_id_from_update(adapter):
    """Test extracting payment external ID from update payload."""
    update_payload = {"data": {"id": "mp-payment-456"}, "other_field": "value"}

    result = adapter.get_payment_external_id_from_update(update_payload)
    assert result == "mp-payment-456"


def test_get_payment_external_id_from_update_missing_data(adapter):
    """Test extracting payment external ID when data is missing."""
    update_payload = {"other_field": "value"}

    result = adapter.get_payment_external_id_from_update(update_payload)
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


@patch.object(MercadoPagoPaymentAdapter, "check_status")
def test_receive_update_valid_payment_update(mock_check_status, adapter):
    """Test receiving valid payment update."""
    update_payload = {
        "type": "payment",
        "action": "payment.update",
        "data": {"id": "mp-payment-456"},
        "id": "update-123",
    }

    mock_status_update = PaymentStatusUpdate(
        id=None, status="approved", description="accredited", update_external_id="update-123"
    )
    mock_check_status.return_value = mock_status_update

    result = adapter.receive_update(update_payload)

    assert result == ("mp-payment-456", mock_status_update)
    mock_check_status.assert_called_once_with("mp-payment-456", "update-123")


def test_receive_update_invalid_type(adapter):
    """Test receiving update with invalid type."""
    update_payload = {
        "type": "refund",
        "action": "payment.update",
        "data": {"id": "mp-payment-456"},
    }

    result = adapter.receive_update(update_payload)
    assert result is None


def test_receive_update_invalid_action(adapter):
    """Test receiving update with invalid action."""
    update_payload = {
        "type": "payment",
        "action": "payment.created",
        "data": {"id": "mp-payment-456"},
    }

    result = adapter.receive_update(update_payload)
    assert result is None


@patch("payments.services.payment_adapters.mercadopago_payment_adapter.logger")
def test_check_refund_status_success(mock_logger, adapter):
    """Test successful refund status check."""
    adapter.sdk.payment().get.return_value = {"response": {"status": "approved"}}

    with patch.dict(
        "payments.services.payment_adapters.mercadopago_payment_adapter.REFUND_STATUS_MAPPING",
        {"approved": RefundStatuses.APPROVED},
    ):
        result = adapter.check_refund_status("refund-456")

    assert result == RefundStatuses.APPROVED
    adapter.sdk.payment().get.assert_called_once_with("refund-456")


@patch("payments.services.payment_adapters.mercadopago_payment_adapter.logger")
def test_check_refund_status_unknown(mock_logger, adapter):
    """Test refund status check with unknown status."""
    refund_payload = {"response": {"status": "unknown_status"}}
    adapter.sdk.payment().get.return_value = refund_payload

    result = adapter.check_refund_status("refund-456")

    assert result == RefundStatuses.UNKNOWN
    mock_logger.error.assert_called_once_with(
        "Unknown refund status: %s", json.dumps(refund_payload)
    )


def test_payment_methods_mapping_usage(adapter, mock_payment):
    """Test that payment methods mapping is used when available."""
    mock_payment.payment_method = "visa"

    with patch.dict(
        "payments.services.payment_adapters.mercadopago_payment_adapter.PAYMENT_METHODS_MAPPING",
        {"visa": "visa_card"},
    ):
        with override_settings(SITE_DOMAIN="example.com"):
            with patch("payments.services.payment_adapters.mercadopago_payment_adapter.reverse"):
                with patch(
                    "payments.services.payment_adapters.mercadopago_payment_adapter.mercadopago.config.RequestOptions"
                ):
                    adapter.sdk.payment().create.return_value = {"response": {"id": "test-id"}}

                    adapter.process(mock_payment, "test-token")

                    # Verify the mapped payment method was used
                    call_args = adapter.sdk.payment().create.call_args[0][0]
                    assert call_args["payment_method_id"] == "visa_card"


def test_document_types_mapping_usage(adapter, mock_payment):
    """Test that document types mapping is used when available."""
    mock_payment.billing_profile.document_type = "CPF"

    with patch.dict(
        "payments.services.payment_adapters.mercadopago_payment_adapter.DOCUMENT_TYPES_MAPPING",
        {"CPF": "cpf_mapped"},
    ):
        with override_settings(SITE_DOMAIN="example.com"):
            with patch("payments.services.payment_adapters.mercadopago_payment_adapter.reverse"):
                with patch(
                    "payments.services.payment_adapters.mercadopago_payment_adapter.mercadopago.config.RequestOptions"
                ):
                    adapter.sdk.payment().create.return_value = {"response": {"id": "test-id"}}

                    adapter.process(mock_payment, "test-token")

                    # Verify the mapped document type was used
                    call_args = adapter.sdk.payment().create.call_args[0][0]
                    assert call_args["payer"]["identification"]["type"] == "cpf_mapped"
