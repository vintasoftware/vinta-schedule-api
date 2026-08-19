"""Unit tests for ``payments.services.provider_credentials`` and
``payments.services.payment_provider_resolver``.
"""

import pytest
from model_bakery import baker

from organizations.models import Organization
from payments.constants import PaymentProviders
from payments.exceptions import PaymentProviderNotConfiguredError
from payments.models import BillingProfile
from payments.services.payment_provider_resolver import PaymentProviderResolver
from payments.services.provider_credentials import (
    PublicProviderCredentials,
    resolve_public_credentials,
)
from payments.tests.provider_settings import use_providers


pytestmark = pytest.mark.django_db


class TestResolvePublicCredentials:
    def test_stripe_returns_only_the_stripe_block(self, settings):
        use_providers(
            settings,
            STRIPE_PUBLISHABLE_KEY="pk_test_stripe",
            MERCADOPAGO_PUBLIC_KEY="pub_test_mercadopago",
        )

        credentials = resolve_public_credentials(PaymentProviders.STRIPE)

        assert credentials == PublicProviderCredentials(
            provider=PaymentProviders.STRIPE,
            stripe_publishable_key="pk_test_stripe",
            mercadopago_public_key=None,
        )

    def test_mercadopago_returns_only_the_mercadopago_block(self, settings):
        use_providers(
            settings,
            STRIPE_PUBLISHABLE_KEY="pk_test_stripe",
            MERCADOPAGO_PUBLIC_KEY="pub_test_mercadopago",
        )

        credentials = resolve_public_credentials(PaymentProviders.MERCADOPAGO)

        assert credentials == PublicProviderCredentials(
            provider=PaymentProviders.MERCADOPAGO,
            stripe_publishable_key=None,
            mercadopago_public_key="pub_test_mercadopago",
        )

    def test_raises_when_the_matching_key_is_empty(self, settings):
        use_providers(settings, STRIPE_PUBLISHABLE_KEY="")

        with pytest.raises(PaymentProviderNotConfiguredError):
            resolve_public_credentials(PaymentProviders.STRIPE)

    def test_raises_for_an_unknown_provider_slug(self, settings):
        use_providers(
            settings,
            STRIPE_PUBLISHABLE_KEY="pk_test_stripe",
            MERCADOPAGO_PUBLIC_KEY="pub_test_mercadopago",
        )

        with pytest.raises(PaymentProviderNotConfiguredError):
            resolve_public_credentials("not-a-real-provider")


class TestPaymentProviderResolver:
    def test_resolve_for_organization_returns_the_pin_when_set(self):
        organization = baker.make(Organization, parent=None, can_invite_organizations=False)
        billing_address = baker.make("vinta_billing.BillingAddress")
        baker.make(
            BillingProfile,
            organization=organization,
            contact_email="billing@example.com",
            document_type="CPF",
            document_number="12345678900",
            billing_address=billing_address,
            payment_provider=PaymentProviders.MERCADOPAGO,
        )

        resolver = PaymentProviderResolver()

        assert resolver.resolve_for_organization(organization) == PaymentProviders.MERCADOPAGO

    def test_resolve_for_organization_returns_the_default_when_unpinned(self, settings):
        use_providers(settings, default_provider=PaymentProviders.STRIPE)
        organization = baker.make(Organization, parent=None, can_invite_organizations=False)
        billing_address = baker.make("vinta_billing.BillingAddress")
        baker.make(
            BillingProfile,
            organization=organization,
            contact_email="billing@example.com",
            document_type="CPF",
            document_number="12345678900",
            billing_address=billing_address,
            payment_provider="",
        )

        resolver = PaymentProviderResolver()

        assert resolver.resolve_for_organization(organization) == PaymentProviders.STRIPE

    def test_resolve_for_organization_returns_the_default_with_no_billing_profile(self, settings):
        use_providers(settings, default_provider=PaymentProviders.STRIPE)
        organization = baker.make(Organization, parent=None, can_invite_organizations=False)

        resolver = PaymentProviderResolver()

        assert resolver.resolve_for_organization(organization) == PaymentProviders.STRIPE

    def test_resolve_default_reads_settings(self, settings):
        use_providers(settings, default_provider=PaymentProviders.MERCADOPAGO)

        resolver = PaymentProviderResolver()

        assert resolver.resolve_default() == PaymentProviders.MERCADOPAGO
