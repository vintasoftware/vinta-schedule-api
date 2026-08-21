"""Integration tests for ``vinta_billing.views.PaymentProviderViewSet``, mounted
through ``vinta_billing.routing.get_extra_patterns()``: the unauthenticated
system-default endpoint and the authenticated, tenant-scoped organization endpoint.

**Both answer 503 for an unconfigured provider.** They disagreed until
``vinta-django-billing`` 0.6.0 -- the organization endpoint had a local ``except``
returning a hardcoded 409 while its sibling in the same module returned 503 -- and
the tests below pinned that asymmetry. 0.6.0 settled it at 503 in both places, for
the reason ``common.exception_handlers.vinta_exception_handler`` spells out: an
unconfigured provider is a deployment fault, not something the caller can fix by
sending a different request.
"""

from typing import ClassVar

from django.core.cache import cache
from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.throttling import ScopedRateThrottle
from vinta_billing.constants import PaymentProviders
from vinta_billing.models import BillingAddress, BillingProfile

from organizations.models import Organization
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN
from organizations.tests.helpers import make_membership
from payments.tests.provider_settings import use_providers


pytestmark = pytest.mark.django_db


def default_provider_url() -> str:
    return reverse("payment-provider-default")


def org_provider_url() -> str:
    return reverse("payment-provider")


@pytest.fixture
def organization():
    return baker.make(Organization, parent=None, can_invite_organizations=False)


@pytest.fixture
def admin_membership(user, organization):
    return make_membership(
        user=user,
        organization=organization,
        groups=[GROUP_ORGANIZATION_ADMIN],
        is_active=True,
    )


def make_billing_profile(organization: Organization, payment_provider: str = "") -> BillingProfile:
    billing_address: BillingAddress = baker.make(BillingAddress)
    return baker.make(
        BillingProfile,
        organization=organization,
        contact_email="billing@example.com",
        document_type="CPF",
        document_number="12345678900",
        billing_address=billing_address,
        payment_provider=payment_provider,
    )


class TestDefaultPaymentProviderEndpoint:
    def test_reachable_with_no_session_at_all(self, anonymous_client, settings):
        use_providers(
            settings,
            default_provider=PaymentProviders.STRIPE,
            STRIPE_PUBLISHABLE_KEY="pk_test_default",
            MERCADOPAGO_PUBLIC_KEY="pub_test_default",
        )

        response = anonymous_client.get(default_provider_url())

        assert response.status_code == status.HTTP_200_OK
        assert response.data == {
            "provider": PaymentProviders.STRIPE,
            "stripe": {"publishable_key": "pk_test_default"},
            "mercadopago": None,
        }

    def test_503_when_default_provider_has_no_public_credentials(self, anonymous_client, settings):
        use_providers(settings, default_provider=PaymentProviders.STRIPE, STRIPE_PUBLISHABLE_KEY="")

        response = anonymous_client.get(default_provider_url())

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE

    def test_throttled_past_the_scoped_rate(self, anonymous_client, settings, monkeypatch):
        use_providers(
            settings,
            default_provider=PaymentProviders.STRIPE,
            STRIPE_PUBLISHABLE_KEY="pk_test_default",
        )
        # `ScopedRateThrottle.THROTTLE_RATES` is bound to `settings.REST_FRAMEWORK
        # ["DEFAULT_THROTTLE_RATES"]` once, at import time -- reassigning
        # `settings.REST_FRAMEWORK` later (even via the `settings` fixture, which
        # correctly fires `setting_changed` and reloads `api_settings`) does not
        # change the *already-bound* dict a throttle class attribute captured
        # earlier. Mutate that dict in place instead, so `get_rate()`'s
        # `self.THROTTLE_RATES[self.scope]` lookup sees "1/min" this test only.
        monkeypatch.setitem(ScopedRateThrottle.THROTTLE_RATES, "payment-provider", "1/min")
        cache.clear()

        first = anonymous_client.get(default_provider_url())
        second = anonymous_client.get(default_provider_url())

        assert first.status_code == status.HTTP_200_OK
        assert second.status_code == status.HTTP_429_TOO_MANY_REQUESTS


class TestOrganizationPaymentProviderEndpoint:
    def test_returns_the_pin_for_a_pinned_organization(
        self, auth_client, admin_membership, organization, settings
    ):
        use_providers(
            settings,
            default_provider=PaymentProviders.STRIPE,
            STRIPE_PUBLISHABLE_KEY="pk_test_org",
            MERCADOPAGO_PUBLIC_KEY="pub_test_org",
        )
        make_billing_profile(organization, payment_provider=PaymentProviders.MERCADOPAGO)

        response = auth_client.get(org_provider_url())

        assert response.status_code == status.HTTP_200_OK
        assert response.data == {
            "provider": PaymentProviders.MERCADOPAGO,
            "stripe": None,
            "mercadopago": {"public_key": "pub_test_org"},
        }

    def test_returns_the_default_for_an_unpinned_organization(
        self, auth_client, admin_membership, organization, settings
    ):
        use_providers(
            settings,
            default_provider=PaymentProviders.STRIPE,
            STRIPE_PUBLISHABLE_KEY="pk_test_org",
        )
        make_billing_profile(organization, payment_provider="")

        response = auth_client.get(org_provider_url())

        assert response.status_code == status.HTTP_200_OK
        assert response.data == {
            "provider": PaymentProviders.STRIPE,
            "stripe": {"publishable_key": "pk_test_org"},
            "mercadopago": None,
        }

    def test_returns_the_default_for_an_organization_with_no_billing_profile(
        self, auth_client, admin_membership, organization, settings
    ):
        use_providers(
            settings,
            default_provider=PaymentProviders.STRIPE,
            STRIPE_PUBLISHABLE_KEY="pk_test_org",
        )
        assert not BillingProfile.objects.filter(organization=organization).exists()

        response = auth_client.get(org_provider_url())

        assert response.status_code == status.HTTP_200_OK
        assert response.data["provider"] == PaymentProviders.STRIPE

    def test_403_with_no_active_organization(self, auth_client):
        response = auth_client.get(org_provider_url())

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_401_when_fully_unauthenticated(self, anonymous_client):
        """The org endpoint (``PaymentProviderViewSet``) and the unauthenticated default
        endpoint (``DefaultPaymentProviderView``) are now split into separate views with
        their own class-level auth; this endpoint keeps the normal DRF auth stack and must
        still reject a genuinely anonymous request."""
        response = anonymous_client.get(org_provider_url())

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_503_when_the_pinned_provider_has_no_public_credentials(
        self, auth_client, admin_membership, organization, settings
    ):
        use_providers(settings, MERCADOPAGO_PUBLIC_KEY="")
        make_billing_profile(organization, payment_provider=PaymentProviders.MERCADOPAGO)

        response = auth_client.get(org_provider_url())

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE

    def test_503_does_not_fall_back_to_the_default(
        self, auth_client, admin_membership, organization, settings
    ):
        """An org pinned to a provider with no public key must not silently receive the
        *other* provider's credentials -- a card token minted for one provider is
        meaningless at the other."""
        use_providers(
            settings,
            default_provider=PaymentProviders.STRIPE,
            STRIPE_PUBLISHABLE_KEY="pk_test_org",
            MERCADOPAGO_PUBLIC_KEY="",
        )
        make_billing_profile(organization, payment_provider=PaymentProviders.MERCADOPAGO)

        response = auth_client.get(org_provider_url())

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert "pk_test_org" not in str(response.content)

    def test_503_for_a_pin_naming_a_provider_absent_from_the_registry(
        self, auth_client, admin_membership, organization, settings
    ):
        """A pin holding a slug that is not a member of ``PaymentProviders`` at all (e.g.
        a provider retired after the org was pinned to it) must 503, not silently fall
        back to the default -- ``choices`` is form/admin-level validation, not a DB
        constraint, so this row is constructible in the database even though no API
        surface can write it today.
        """
        use_providers(
            settings,
            default_provider=PaymentProviders.STRIPE,
            STRIPE_PUBLISHABLE_KEY="pk_test_org",
            MERCADOPAGO_PUBLIC_KEY="pub_test_org",
        )
        make_billing_profile(organization, payment_provider="legacy_provider_removed")

        response = auth_client.get(org_provider_url())

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert "pk_test_org" not in str(response.content)


class TestNoSecretLeak:
    """The single way this feature could cause real harm is serializing a secret onto a
    public, unauthenticated response. Distinctive sentinel values stand in for every
    secret setting so a leak cannot hide behind a value that also happens to appear
    elsewhere in the response.
    """

    SENTINELS: ClassVar[dict[str, str]] = {
        "STRIPE_SECRET_KEY": "sk_live_SECRET_SENTINEL_STRIPE",
        "STRIPE_WEBHOOK_SECRET": "whsec_SECRET_SENTINEL_STRIPE",
        "MERCADOPAGO_ACCESS_TOKEN": "APP_USR_SECRET_SENTINEL_MERCADOPAGO",
        "MERCADOPAGO_WEBHOOK_SECRET": "SECRET_SENTINEL_MERCADOPAGO_WEBHOOK",
    }

    def _set_sentinels(self, settings) -> None:
        use_providers(settings, **self.SENTINELS)

    def _assert_no_sentinel_leak(self, response) -> None:
        body = str(response.content)
        for value in self.SENTINELS.values():
            assert value not in body

    def test_default_endpoint_never_leaks_a_secret(self, anonymous_client, settings):
        use_providers(
            settings,
            default_provider=PaymentProviders.STRIPE,
            STRIPE_PUBLISHABLE_KEY="pk_test_default",
            MERCADOPAGO_PUBLIC_KEY="pub_test_default",
        )
        self._set_sentinels(settings)

        response = anonymous_client.get(default_provider_url())

        assert response.status_code == status.HTTP_200_OK
        self._assert_no_sentinel_leak(response)

    def test_org_endpoint_never_leaks_a_secret(
        self, auth_client, admin_membership, organization, settings
    ):
        use_providers(
            settings,
            default_provider=PaymentProviders.STRIPE,
            STRIPE_PUBLISHABLE_KEY="pk_test_org",
            MERCADOPAGO_PUBLIC_KEY="pub_test_org",
        )
        self._set_sentinels(settings)
        make_billing_profile(organization, payment_provider=PaymentProviders.MERCADOPAGO)

        response = auth_client.get(org_provider_url())

        assert response.status_code == status.HTTP_200_OK
        self._assert_no_sentinel_leak(response)
