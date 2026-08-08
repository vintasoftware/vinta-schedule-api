"""Payment Provider Selection, Phase 4 data migrations.

- ``0018_repoint_subscription_payment_provider`` -- brings every existing
  ``Subscription.payment_provider`` into agreement with its organization's own
  resolution (pin -> ``DEFAULT_PAYMENT_PROVIDER``), undoing the hardcoded
  ``mercadopago`` that ``create_subscription_for_organization`` and
  ``payments.0009`` stamped on every row.
- ``0019_backfill_subscription_payment_provider_on_payments`` -- stamps the
  owning subscription's provider onto the subscription-charge ``Payment`` rows
  ``receive_subscription_payment_update`` created with an empty one.

Same deviation from strict migration isolation as ``test_backfill_migration.py``
and ``test_plan_seed_migration.py``, for the same reason and with the same
caveat: these call the migrations' own functions with the *live* app registry
(``django.apps.apps``) rather than the historical state a real ``RunPython``
receives. Safe here because both functions touch only ``Subscription``,
``BillingProfile``, and ``Payment``, whose shapes at 0017/0018 are identical to
their current ones. If a later migration changes any of those, switch these to a
``MigrationExecutor``-driven historical fixture.
"""

import datetime
import importlib
from decimal import Decimal

from django.apps import apps

import pytest
from model_bakery import baker

from organizations.models import Organization
from payments.constants import PaymentProviders, PaymentStatuses
from payments.models import BillingPlan, Payment, Subscription


# These modules build their own Subscription rows (OneToOne with Organization),
# so they opt out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription


_repoint_module = importlib.import_module(
    "payments.migrations.0018_repoint_subscription_payment_provider"
)
repoint_to_organization_provider = _repoint_module.repoint_to_organization_provider
restore_hardcoded_mercadopago = _repoint_module.restore_hardcoded_mercadopago

_payment_backfill_module = importlib.import_module(
    "payments.migrations.0019_backfill_subscription_payment_provider_on_payments"
)
backfill_from_subscription = _payment_backfill_module.backfill_from_subscription
unset_on_subscription_payments = _payment_backfill_module.unset_on_subscription_payments


def _billing_profile_for(organization: Organization, provider: str):
    return baker.make(
        "payments.BillingProfile",
        organization=organization,
        contact_email="billing@example.com",
        document_type="CPF",
        document_number="12345678900",
        billing_address=baker.make("payments.BillingAddress"),
        payment_provider=provider,
    )


def _subscription(organization: Organization, plan: BillingPlan, provider: str) -> Subscription:
    """A ``Subscription`` stamped ``provider`` -- built directly so the starting
    value is unambiguously the test's own, not whatever a service resolved."""
    return baker.make(
        Subscription,
        organization=organization,
        plan=plan,
        payment_provider=provider,
        current_period_start=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        current_period_end=datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC),
    )


@pytest.fixture
def plan():
    return baker.make(BillingPlan, is_default_for_new_organizations=False)


@pytest.mark.django_db
class TestRepointSubscriptionPaymentProvider:
    def test_stripe_pinned_org_subscription_is_repointed_off_mercadopago(self, plan, settings):
        settings.DEFAULT_PAYMENT_PROVIDER = PaymentProviders.STRIPE
        org = baker.make(Organization, parent=None)
        _billing_profile_for(org, PaymentProviders.STRIPE)
        subscription = _subscription(org, plan, PaymentProviders.MERCADOPAGO)

        repoint_to_organization_provider(apps, None)

        subscription.refresh_from_db()
        assert subscription.payment_provider == PaymentProviders.STRIPE

    def test_mercadopago_pinned_org_subscription_is_left_alone(self, plan, settings):
        """The discriminating half: an organization genuinely pinned to
        MercadoPago keeps ``mercadopago``, so the migration is a *repoint*, not a
        blanket rewrite to the default."""
        settings.DEFAULT_PAYMENT_PROVIDER = PaymentProviders.STRIPE
        org = baker.make(Organization, parent=None)
        _billing_profile_for(org, PaymentProviders.MERCADOPAGO)
        subscription = _subscription(org, plan, PaymentProviders.MERCADOPAGO)

        repoint_to_organization_provider(apps, None)

        subscription.refresh_from_db()
        assert subscription.payment_provider == PaymentProviders.MERCADOPAGO

    def test_unpinned_and_profileless_orgs_take_the_system_default(self, plan, settings):
        settings.DEFAULT_PAYMENT_PROVIDER = PaymentProviders.STRIPE
        unpinned_org = baker.make(Organization, parent=None)
        _billing_profile_for(unpinned_org, "")
        unpinned = _subscription(unpinned_org, plan, PaymentProviders.MERCADOPAGO)
        profileless_org = baker.make(Organization, parent=None)
        profileless = _subscription(profileless_org, plan, PaymentProviders.MERCADOPAGO)

        repoint_to_organization_provider(apps, None)

        unpinned.refresh_from_db()
        profileless.refresh_from_db()
        assert unpinned.payment_provider == PaymentProviders.STRIPE
        assert profileless.payment_provider == PaymentProviders.STRIPE

    def test_reverse_restores_the_pre_migration_hardcoded_value(self, plan, settings):
        """Every pre-migration row said ``mercadopago`` -- the hardcode in
        ``create_subscription_for_organization`` and ``payments.0009`` are the
        only two writers there ever were -- so that is what a reverse restores."""
        settings.DEFAULT_PAYMENT_PROVIDER = PaymentProviders.STRIPE
        org = baker.make(Organization, parent=None)
        _billing_profile_for(org, PaymentProviders.STRIPE)
        subscription = _subscription(org, plan, PaymentProviders.MERCADOPAGO)

        repoint_to_organization_provider(apps, None)
        subscription.refresh_from_db()
        assert subscription.payment_provider == PaymentProviders.STRIPE

        restore_hardcoded_mercadopago(apps, None)

        subscription.refresh_from_db()
        assert subscription.payment_provider == PaymentProviders.MERCADOPAGO

    def test_is_idempotent(self, plan, settings):
        settings.DEFAULT_PAYMENT_PROVIDER = PaymentProviders.STRIPE
        org = baker.make(Organization, parent=None)
        _billing_profile_for(org, PaymentProviders.STRIPE)
        subscription = _subscription(org, plan, PaymentProviders.MERCADOPAGO)

        repoint_to_organization_provider(apps, None)
        repoint_to_organization_provider(apps, None)

        subscription.refresh_from_db()
        assert subscription.payment_provider == PaymentProviders.STRIPE


@pytest.mark.django_db
class TestBackfillSubscriptionPaymentProviderOnPayments:
    def _payment(self, billing_profile, subscription, provider: str, external_id: str) -> Payment:
        return baker.make(
            Payment,
            billing_profile=billing_profile,
            subscription=subscription,
            value=Decimal("50"),
            currency="BRL",
            status=PaymentStatuses.APPROVED,
            payment_method="visa",
            payment_provider=provider,
            external_id=external_id,
        )

    def test_empty_provider_rows_take_their_subscriptions_provider(self, plan):
        org = baker.make(Organization, parent=None)
        billing_profile = _billing_profile_for(org, PaymentProviders.MERCADOPAGO)
        subscription = _subscription(org, plan, PaymentProviders.MERCADOPAGO)
        payment = self._payment(billing_profile, subscription, "", "mp-sub-payment-1")

        backfill_from_subscription(apps, None)

        payment.refresh_from_db()
        assert payment.payment_provider == PaymentProviders.MERCADOPAGO

    def test_rows_that_already_carry_a_provider_are_untouched(self, plan):
        """Including one that disagrees with its subscription: an already-stamped
        row is the authority on where its own charge was made (Rule A), and this
        migration exists only to fill in the ones that were left blank."""
        org = baker.make(Organization, parent=None)
        billing_profile = _billing_profile_for(org, PaymentProviders.STRIPE)
        subscription = _subscription(org, plan, PaymentProviders.STRIPE)
        payment = self._payment(
            billing_profile, subscription, PaymentProviders.MERCADOPAGO, "mp-legacy-1"
        )

        backfill_from_subscription(apps, None)

        payment.refresh_from_db()
        assert payment.payment_provider == PaymentProviders.MERCADOPAGO

    def test_non_subscription_payments_are_untouched(self, plan):
        """A one-off charge with no subscription has nothing to derive a provider
        from; guessing would be worse than the loud error it already gets."""
        org = baker.make(Organization, parent=None)
        billing_profile = _billing_profile_for(org, PaymentProviders.MERCADOPAGO)
        _subscription(org, plan, PaymentProviders.MERCADOPAGO)
        payment = baker.make(
            Payment,
            billing_profile=billing_profile,
            subscription=None,
            value=Decimal("10"),
            currency="BRL",
            status=PaymentStatuses.APPROVED,
            payment_method="visa",
            payment_provider="",
            external_id="one-off-1",
        )

        backfill_from_subscription(apps, None)

        payment.refresh_from_db()
        assert payment.payment_provider == ""

    def test_reverse_restores_the_pre_migration_empty_provider(self, plan):
        org = baker.make(Organization, parent=None)
        billing_profile = _billing_profile_for(org, PaymentProviders.MERCADOPAGO)
        subscription = _subscription(org, plan, PaymentProviders.MERCADOPAGO)
        payment = self._payment(billing_profile, subscription, "", "mp-sub-payment-1")

        backfill_from_subscription(apps, None)
        payment.refresh_from_db()
        assert payment.payment_provider == PaymentProviders.MERCADOPAGO

        unset_on_subscription_payments(apps, None)

        payment.refresh_from_db()
        assert payment.payment_provider == ""

    def test_is_idempotent(self, plan):
        org = baker.make(Organization, parent=None)
        billing_profile = _billing_profile_for(org, PaymentProviders.MERCADOPAGO)
        subscription = _subscription(org, plan, PaymentProviders.MERCADOPAGO)
        payment = self._payment(billing_profile, subscription, "", "mp-sub-payment-1")

        backfill_from_subscription(apps, None)
        backfill_from_subscription(apps, None)

        payment.refresh_from_db()
        assert payment.payment_provider == PaymentProviders.MERCADOPAGO
