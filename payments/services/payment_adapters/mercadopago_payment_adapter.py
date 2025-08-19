import json
import logging

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse

import mercadopago
import mercadopago.config

from payments.constants import PaymentProviders, RefundStatuses
from payments.services.dataclasses import Refund
from payments.services.payment_adapters.base import BasePaymentAdapter, Payment, PaymentStatusUpdate


logger = logging.getLogger(__name__)


PAYMENT_METHODS_MAPPING: dict[str, str] = {}
DOCUMENT_TYPES_MAPPING: dict[str, str] = {}
PAYMENT_STATUS_MAPPING: dict[str, str] = {}
REFUND_STATUS_MAPPING: dict[str, str] = {}


class MercadoPagoPaymentAdapter(BasePaymentAdapter):
    provider = PaymentProviders.MERCADOPAGO

    def __init__(self, access_token: str):
        self.sdk = mercadopago.SDK(access_token)

    def process(self, payment: Payment, payment_token: str) -> str:
        request_options = mercadopago.config.RequestOptions()
        request_options.custom_headers = {"x-idempotency-key": {payment.id}}
        notification_url = reverse(
            "api:Payments-payment-update",
            kwargs={"provider": PaymentProviders.MERCADOPAGO, "pk": payment.id},
        )

        site_domain = getattr(settings, "SITE_DOMAIN", None)
        if not site_domain:
            raise ImproperlyConfigured(
                "MercadoPagoAdapter requires SITE_DOMAIN to be set in settings.py"
            )

        payment_data = {
            "transaction_amount": str(payment.value),
            "token": payment_token,
            "description": payment.description,
            "payment_method_id": PAYMENT_METHODS_MAPPING.get(
                payment.payment_method, payment.payment_method
            ),
            "notification_url": f"https://{site_domain}{notification_url}",
            "installments": 1,
            "payer": {
                "email": payment.billing_profile.email,
                "identification": {
                    "type": DOCUMENT_TYPES_MAPPING.get(
                        payment.billing_profile.document_type or "",
                        payment.billing_profile.document_type,
                    ),
                    "number": payment.billing_profile.document_number,
                },
                "first_name": payment.billing_profile.first_name,
                "last_name": payment.billing_profile.last_name,
                "address": {
                    "street_name": payment.billing_profile.billing_address.street_name,
                    "street_number": payment.billing_profile.billing_address.street_number,
                    "neighborhood": payment.billing_profile.billing_address.neighborhood,
                    "city": payment.billing_profile.billing_address.city,
                    "federal_unit": payment.billing_profile.billing_address.state,
                    "country": payment.billing_profile.billing_address.country,
                    "zip_code": payment.billing_profile.billing_address.zip_code,
                },
            },
        }
        result = self.sdk.payment().create(payment_data, request_options)
        return result["response"]["id"]

    def refund(self, refund: Refund) -> str:
        request_options = mercadopago.config.RequestOptions()
        request_options.custom_headers = {"x-idempotency-key": {refund.id}}
        response = self.sdk.refund().create(
            refund.payment.external_id, {"amount": str(refund.value)}, request_options
        )
        return response["response"]["id"]

    def check_status(
        self, payment_external_id: str, update_id: str | None = None
    ) -> PaymentStatusUpdate:
        response = self.sdk.payment().get(payment_external_id)
        return PaymentStatusUpdate(
            id=None,
            status=response["response"]["status"],
            description=response["response"]["status_detail"],
            update_external_id=update_id,
        )

    def get_payment_external_id_from_update(self, update_payload: dict) -> str | None:
        return update_payload.get("data", {}).get("id")

    def get_update_id(self, update_payload: dict) -> str | None:
        return update_payload.get("id")

    def receive_update(self, update_payload: dict) -> tuple[str, PaymentStatusUpdate] | None:
        if (
            update_payload.get("type") != "payment"
            or update_payload.get("action") != "payment.update"
        ):
            return None
        return super().receive_update(update_payload)

    def check_refund_status(self, refund_external_id: str) -> str:
        refund_payload = self.sdk.payment().get(refund_external_id)
        original_status = refund_payload["response"]["status"]
        internal_status = REFUND_STATUS_MAPPING.get(original_status, RefundStatuses.UNKNOWN)
        if internal_status == RefundStatuses.UNKNOWN:
            logger.error("Unknown refund status: %s", json.dumps(refund_payload))
        return internal_status
