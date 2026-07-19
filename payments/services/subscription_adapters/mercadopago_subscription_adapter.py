import logging
from collections.abc import Mapping
from types import MappingProxyType

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse

import mercadopago

from payments.billing_constants import BillingInterval
from payments.constants import PaymentProviders, PaymentStatuses
from payments.exceptions import ProviderWebhookEventIdMissingError
from payments.services.dataclasses import (
    BillingAddress,
    BillingProfile,
    CreatedPlan,
    PaymentStatusUpdate,
    Plan,
    Subscription,
    SubscriptionPayment,
)
from payments.services.mercadopago_signature import verify_mercadopago_signature
from payments.services.subscription_adapters.base import BaseSubscriptionAdapter


logger = logging.getLogger(__name__)


# `create_status_update_from_payment_payload` processes the underlying MercadoPago
# *payment* attached to a subscription charge, so despite the name this maps onto
# `PaymentStatuses` (the same enum `PAYMENT_STATUS_MAPPING` in the payment adapter
# targets) rather than `SubscriptionStatuses`.
# https://www.mercadopago.com/developers/en/docs/checkout-api/payment-management/status
SUBSCRIPTION_STATUS_MAPPING: MappingProxyType[str, str] = MappingProxyType(
    {
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
)


def _auto_recurring_frequency(billing_interval: str) -> tuple[int, str]:
    """Map our internal billing cadence onto MercadoPago's `auto_recurring` fields.

    MercadoPago's `frequency_type` only accepts `"days"` or `"months"` — there is
    no `"years"` option — so an annual plan is expressed as 12 months rather than
    `frequency=1, frequency_type="years"`.
    """
    if billing_interval == BillingInterval.ANNUAL:
        return 12, "months"
    return 1, "months"


class MercadoPagoSubscriptionAdapter(BaseSubscriptionAdapter):
    provider = PaymentProviders.MERCADOPAGO
    #: MercadoPago's `x-signature` HMAC covers only `data.id` + `x-request-id` +
    #: `ts` — never the request body as a whole. See
    #: `payments.services.mercadopago_signature.verify_mercadopago_signature`.
    verifies_full_body = False

    def __init__(self, access_token: str, webhook_secret: str = ""):
        self.sdk = mercadopago.SDK(access_token)
        self.webhook_secret = webhook_secret

    def create_subscription_plan(self, plan: Plan) -> str:
        frequency, frequency_type = _auto_recurring_frequency(plan.billing_interval)
        response = self.sdk.plan().create(
            {
                "reason": plan.name,
                "external_reference": plan.id,
                "auto_recurring": {
                    "frequency": frequency,
                    "frequency_type": frequency_type,
                    "transaction_amount": str(plan.value),
                    "currency_id": plan.currency,
                    "billing_day": plan.billing_day,
                    "billing_day_proportional": True,
                },
                "payment_methods_allowed": {
                    "payment_types": [
                        "credit_card",
                    ],
                    "payment_methods": [
                        "master",
                        "visa",
                        "amex",
                        "diners",
                    ],
                },
            }
        )
        return response["response"]["id"]

    def update_subscription_plan(self, external_id: str, plan: Plan) -> str:
        frequency, frequency_type = _auto_recurring_frequency(plan.billing_interval)
        response = self.sdk.plan().update(
            external_id,
            {
                "reason": plan.name,
                "external_reference": plan.id,
                "auto_recurring": {
                    "frequency": frequency,
                    "frequency_type": frequency_type,
                    "transaction_amount": str(plan.value),
                    "currency_id": plan.currency,
                    "billing_day": plan.billing_day,
                    "billing_day_proportional": True,
                },
                "payment_methods_allowed": {
                    "payment_types": [
                        "credit_card",
                    ],
                    "payment_methods": [
                        "master",
                        "visa",
                        "amex",
                        "diners",
                    ],
                },
            },
        )
        return response["response"]["id"]

    def create_subscription(self, subscription: Subscription, payment_token: str) -> str:
        notification_url = reverse(
            "api:Payments-subscription-payment-update",
            kwargs={"provider": PaymentProviders.MERCADOPAGO, "pk": subscription.id},
        )

        site_domain = getattr(settings, "SITE_DOMAIN", None)
        if not site_domain:
            raise ImproperlyConfigured(
                "MercadoPagoAdapter requires SITE_DOMAIN to be set in settings.py"
            )
        response = self.sdk.preapproval().create(
            {
                "payer_email": subscription.billing_profile.email,
                "preapproval_plan_id": subscription.plan.external_id,
                "back_url": f"https://{site_domain}/subscription/{subscription.id}/success",
                "external_reference": subscription.id,
                "card_token_id": payment_token,
                "status": "authorized",
                "notification_url": f"https://{site_domain}{notification_url}",
            }
        )
        return response["response"]["id"]

    def cancel_subscription(self, subscription: Subscription) -> None:
        site_domain = getattr(settings, "SITE_DOMAIN", None)
        if not site_domain:
            raise ImproperlyConfigured(
                "MercadoPagoAdapter requires SITE_DOMAIN to be set in settings.py"
            )
        self.sdk.preapproval().update(
            subscription.external_id,
            {
                "back_url": f"https://{site_domain}/subscription/{subscription.id}/cancelled",
                "external_reference": subscription.id,
                "status": "cancelled",
            },
        )

    def update_subscription_payment_token(
        self, subscription: Subscription, payment_token: str
    ) -> None:
        site_domain = getattr(settings, "SITE_DOMAIN", None)
        if not site_domain:
            raise ImproperlyConfigured(
                "MercadoPagoAdapter requires SITE_DOMAIN to be set in settings.py"
            )
        self.sdk.preapproval().update(
            subscription.external_id,
            {
                "back_url": f"https://{site_domain}/subscription/{subscription.id}/cancelled",
                "external_reference": subscription.id,
                "card_token_id": payment_token,
                "status": "authorized",
            },
        )

    def update_plan(self, plan: CreatedPlan) -> CreatedPlan:
        frequency, frequency_type = _auto_recurring_frequency(plan.billing_interval)
        self.sdk.plan().update(
            plan.external_id,
            {
                "reason": plan.name,
                "external_reference": plan.id,
                "auto_recurring": {
                    "frequency": frequency,
                    "frequency_type": frequency_type,
                    "transaction_amount": str(plan.value),
                    "currency_id": plan.currency,
                    "billing_day": plan.billing_day,
                    "billing_day_proportional": True,
                },
                "payment_methods_allowed": {
                    "payment_types": [
                        "credit_card",
                    ],
                    "payment_methods": [
                        "master",
                        "visa",
                        "amex",
                        "diners",
                    ],
                },
            },
        )
        return plan

    def get_subscription_external_id_from_update(self, update_payload: dict) -> str | None:
        return update_payload.get("data", {}).get("id")

    def get_update_id(self, update_payload: dict) -> str | None:
        return update_payload.get("id")

    def get_payment_payload(self, payment_external_id: str) -> dict:
        return self.sdk.payment().get(payment_external_id)

    def create_subscription_payment_from_payment_payload(
        self, subscription_external_id: str, payment_payload: dict
    ) -> SubscriptionPayment:
        return SubscriptionPayment(
            id=None,
            subscription_external_id=subscription_external_id,
            external_id=payment_payload["response"]["id"],
            value=payment_payload["response"]["transaction_amount"],
            currency=payment_payload["response"]["currency_id"],
            payment_provider="mercadopago",
            status=payment_payload["response"]["status"],
            payment_method=payment_payload["response"]["payment_method_id"],
            description=payment_payload["response"]["description"],
            status_updates=[],
            billing_profile=BillingProfile(
                email=payment_payload["response"]["payer"]["email"],
                first_name=payment_payload["response"]["payer"]["first_name"],
                last_name=payment_payload["response"]["payer"]["last_name"],
                document_type=payment_payload["response"]["payer"]["identification"]["type"],
                document_number=payment_payload["response"]["payer"]["identification"]["number"],
                billing_address=BillingAddress(
                    id=None,
                    street_name=payment_payload["response"]["payer"]["address"]["street_name"],
                    street_number=payment_payload["response"]["payer"]["address"]["street_number"],
                    neighborhood=payment_payload["response"]["payer"]["address"]["neighborhood"],
                    city=payment_payload["response"]["payer"]["address"]["city"],
                    state=payment_payload["response"]["payer"]["address"]["federal_unit"],
                    country=payment_payload["response"]["payer"]["address"]["country"],
                    zip_code=payment_payload["response"]["payer"]["address"]["zip_code"],
                    address_line_2="",
                ),
                phone=None,
                pk=None,
            ),
        )

    def create_status_update_from_payment_payload(
        self, payment_payload: dict
    ) -> PaymentStatusUpdate:
        # `payment_payload` is the SDK's payment-get API response (`{"response":
        # {...}}`), which has no top-level "id" — `get_update_id` (which reads
        # `payload["id"]`) never matches anything here, so it always returned None.
        original_status = payment_payload["response"]["status"]
        mapped_status = SUBSCRIPTION_STATUS_MAPPING.get(original_status, PaymentStatuses.UNKNOWN)
        if mapped_status == PaymentStatuses.UNKNOWN:
            logger.error(
                "Unknown subscription payment status: payment_external_id=%s original_status=%s",
                payment_payload["response"].get("id"),
                original_status,
            )
        return PaymentStatusUpdate(
            id=None,
            status=mapped_status,
            description=payment_payload["response"]["status_detail"],
            update_external_id=payment_payload["response"]["id"],
        )

    def is_payment_update(self, update_payload: dict) -> bool:
        return update_payload.get("type") == "subscription_authorized_payment"

    def get_subscription_payload(self, subscription_external_id: str) -> dict:
        return self.sdk.preapproval().get(subscription_external_id)

    def get_payment_external_id_from_subscription_payload(
        self, subscription_payload: dict
    ) -> str | None:
        return subscription_payload.get("response", {}).get("last_payment_id")

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
