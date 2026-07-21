import logging
from collections.abc import Mapping

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse

import mercadopago
import mercadopago.config

from payments.constants import PaymentProviders, PaymentStatuses, RefundStatuses
from payments.exceptions import ProviderWebhookEventIdMissingError
from payments.services.dataclasses import Refund, RefundResult
from payments.services.mercadopago_signature import verify_mercadopago_signature
from payments.services.payment_adapters.base import BasePaymentAdapter, Payment, PaymentStatusUpdate


logger = logging.getLogger(__name__)


# Our internal `payment_method` / `document_type` values are already stored using
# MercadoPago's own vocabulary (there is currently no other provider to alias
# from/to), so these are close to identity maps. They exist as a translation seam
# for the day a second naming convention (e.g. a future provider-agnostic form
# value) needs to land on MercadoPago's specific codes.
PAYMENT_METHODS_MAPPING: dict[str, str] = {
    "visa": "visa",
    "master": "master",
    "amex": "amex",
    "diners": "diners",
    "elo": "elo",
    "pix": "pix",
    "boleto": "bolbradesco",
}
DOCUMENT_TYPES_MAPPING: dict[str, str] = {
    "CPF": "CPF",
    "CNPJ": "CNPJ",
    "DNI": "DNI",
    "CI": "CI",
    "RUT": "RUT",
    "OTHER": "OTHER",
}
# MercadoPago payment statuses: https://www.mercadopago.com/developers/en/docs/checkout-api/payment-management/status
PAYMENT_STATUS_MAPPING: dict[str, str] = {
    "pending": PaymentStatuses.PENDING,
    "approved": PaymentStatuses.APPROVED,
    "authorized": PaymentStatuses.APPROVED,
    "in_process": PaymentStatuses.IN_PROCESS,
    "in_mediation": PaymentStatuses.IN_MEDIATION,
    "rejected": PaymentStatuses.REJECTED,
    "cancelled": PaymentStatuses.CANCELLED,
    "refunded": PaymentStatuses.REFUNDED,
    "charged_back": PaymentStatuses.CHARGED_BACK,
}
REFUND_STATUS_MAPPING: dict[str, str] = {
    "pending": RefundStatuses.PENDING,
    "approved": RefundStatuses.APPROVED,
    "rejected": RefundStatuses.REJECTED,
}


class MercadoPagoPaymentAdapter(BasePaymentAdapter):
    provider = PaymentProviders.MERCADOPAGO
    #: MercadoPago's `x-signature` HMAC covers only `data.id` + `x-request-id` +
    #: `ts` — never the request body as a whole. See
    #: `payments.services.mercadopago_signature.verify_mercadopago_signature`.
    verifies_full_body = False

    def __init__(self, access_token: str, webhook_secret: str = ""):
        self.sdk = mercadopago.SDK(access_token)
        self.webhook_secret = webhook_secret

    def process(self, payment: Payment, payment_token: str, idempotency_key: str = "") -> str:
        request_options = mercadopago.config.RequestOptions()
        # A caller-supplied stable key (survives a Payment row being re-created on
        # retry) takes precedence over the local payment id; see
        # `BasePaymentAdapter.process`. Must be a string header value — the prior
        # `{payment.id}` set literal serialized wrong.
        request_options.custom_headers = {"x-idempotency-key": idempotency_key or str(payment.id)}
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

    def refund(self, refund: Refund) -> RefundResult:
        request_options = mercadopago.config.RequestOptions()
        request_options.custom_headers = {"x-idempotency-key": {refund.id}}
        response = self.sdk.refund().create(
            refund.payment.external_id, {"amount": str(refund.value)}, request_options
        )
        # MercadoPago's create-refund response already carries the refund's
        # status (see https://www.mercadopago.com/developers/en/reference/.../create-refund/post)
        # — no need to force a second round trip through `check_refund_status`
        # just to learn what this response already told us.
        original_status = response["response"].get("status")
        mapped_status = REFUND_STATUS_MAPPING.get(original_status, RefundStatuses.UNKNOWN)
        if mapped_status == RefundStatuses.UNKNOWN:
            logger.error(
                "Unknown refund status: refund_id=%s original_status=%s",
                refund.id,
                original_status,
            )
        return RefundResult(external_id=response["response"]["id"], status=mapped_status)

    def check_status(
        self, payment_external_id: str, update_id: str | None = None
    ) -> PaymentStatusUpdate:
        response = self.sdk.payment().get(payment_external_id)
        original_status = response["response"]["status"]
        mapped_status = PAYMENT_STATUS_MAPPING.get(original_status, PaymentStatuses.UNKNOWN)
        if mapped_status == PaymentStatuses.UNKNOWN:
            logger.error(
                "Unknown payment status: payment_external_id=%s original_status=%s",
                payment_external_id,
                original_status,
            )
        return PaymentStatusUpdate(
            id=None,
            status=mapped_status,
            description=response["response"]["status_detail"],
            # Sourced from the authenticated API response, not the caller-supplied
            # `update_id` — the webhook notification's own top-level id is never
            # covered by MercadoPago's signature and must not be persisted as an
            # external id (see `get_event_id` below).
            update_external_id=response["response"].get("id"),
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

    def check_refund_status(self, refund: Refund) -> str:
        """
        MercadoPago has no single-refund-by-id endpoint — only "list refunds for
        a payment" (see `Refund.list_all` in the `mercadopago` SDK) — so, unlike
        `check_status` for payments, this requires the *payment's* external id
        (`refund.payment.external_id`), not just the refund's own id.
        """
        if not refund.external_id:
            logger.error(
                "Cannot check refund status without an external_id: refund_id=%s", refund.id
            )
            return RefundStatuses.UNKNOWN

        refunds_payload = self.sdk.refund().list_all(refund.payment.external_id)
        for item in refunds_payload.get("response", []):
            if str(item.get("id")) == str(refund.external_id):
                original_status = item.get("status")
                mapped_status = REFUND_STATUS_MAPPING.get(original_status, RefundStatuses.UNKNOWN)
                if mapped_status == RefundStatuses.UNKNOWN:
                    logger.error(
                        "Unknown refund status: refund_external_id=%s original_status=%s",
                        refund.external_id,
                        original_status,
                    )
                return mapped_status

        logger.error(
            "Refund not found in provider's refund list: refund_external_id=%s payment_external_id=%s",
            refund.external_id,
            refund.payment.external_id,
        )
        return RefundStatuses.UNKNOWN

    def verify_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        return verify_mercadopago_signature(raw_body, headers, self.webhook_secret) is not None

    def get_event_id(self, raw_body: bytes, headers: Mapping[str, str], payload: dict) -> str:
        """MercadoPago's HMAC never covers the notification payload's top-level
        ``id`` (only ``data.id`` + ``x-request-id`` + ``ts``), so it cannot be used
        as the idempotency ledger key — an attacker can vary it freely across
        replays of one captured valid signature. Derive the key entirely from the
        verified manifest instead.
        """
        manifest = verify_mercadopago_signature(raw_body, headers, self.webhook_secret)
        if manifest is None:
            raise ProviderWebhookEventIdMissingError
        return manifest.event_id
