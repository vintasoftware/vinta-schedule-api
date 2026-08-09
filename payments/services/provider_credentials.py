"""Resolves the browser-safe half of a payment provider's credentials.

This module reads ``settings.STRIPE_PUBLISHABLE_KEY`` / ``settings.MERCADOPAGO_PUBLIC_KEY``
**directly** and must never import, construct, or otherwise touch a payment adapter
(``payments.services.payment_adapters`` / ``payments.services.subscription_adapters``) or
any *secret* setting (``STRIPE_SECRET_KEY``, ``MERCADOPAGO_ACCESS_TOKEN``,
``STRIPE_WEBHOOK_SECRET``, ``MERCADOPAGO_WEBHOOK_SECRET``). Those adapters hold the secret
keys used to authenticate outbound calls to the provider; this module backs the
unauthenticated and authenticated-but-read-only provider-credentials endpoints
(``payments.views.PaymentProviderViewSet``), so it must have no code path that could ever
serialize a secret onto a response. Keep it that way -- do not import an adapter here, even
transitively, and do not add a helper that reads a secret setting into this module.
"""

from dataclasses import dataclass

from django.conf import settings

from payments.constants import PaymentProviders
from payments.exceptions import PaymentProviderNotConfiguredError


@dataclass(frozen=True)
class PublicProviderCredentials:
    """The non-secret, browser-safe half of a provider's credentials.

    Deliberately separate from the adapter's constructor arguments: the adapter holds the
    *secret* key (``STRIPE_SECRET_KEY`` / ``MERCADOPAGO_ACCESS_TOKEN``) and must never be a
    source these values are read through, so that no refactor can accidentally serialize a
    secret onto a response.
    """

    provider: str
    stripe_publishable_key: str | None = None
    mercadopago_public_key: str | None = None


def resolve_public_credentials(provider: str) -> PublicProviderCredentials:
    """The public credentials for ``provider``, with only the matching provider's field
    populated.

    :raises PaymentProviderNotConfiguredError: ``provider`` is not a real, registered
        provider (an unknown slug), or it is a real provider whose public key setting is
        empty in this deployment. Both cases collapse to the same error here -- unlike the
        webhook views' ``UnknownPaymentProviderError``/``PaymentProviderNotConfiguredError``
        split, the credentials endpoints have nothing useful to say beyond "this provider
        cannot be used to render a payment form right now" (see the plan's API Design
        section for ``GET /billing/payment-provider/`` and its ``/default/`` sibling).
    """
    if provider == PaymentProviders.STRIPE:
        publishable_key = settings.STRIPE_PUBLISHABLE_KEY
        if not publishable_key:
            raise PaymentProviderNotConfiguredError(provider)
        return PublicProviderCredentials(provider=provider, stripe_publishable_key=publishable_key)

    if provider == PaymentProviders.MERCADOPAGO:
        public_key = settings.MERCADOPAGO_PUBLIC_KEY
        if not public_key:
            raise PaymentProviderNotConfiguredError(provider)
        return PublicProviderCredentials(provider=provider, mercadopago_public_key=public_key)

    raise PaymentProviderNotConfiguredError(provider)
