"""Data migrations that repoint subscription payment providers.

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
(``payments.tests.historical_apps``) rather than the historical state a real ``RunPython``
receives. Safe here because both functions touch only ``Subscription``,
``BillingProfile``, and ``Payment``, whose shapes at 0017/0018 are identical to
their current ones. If a later migration changes any of those, switch these to a
``MigrationExecutor``-driven historical fixture.
"""

import datetime
import importlib
from decimal import Decimal

import pytest
from model_bakery import baker
from vinta_billing.constants import PaymentProviders, PaymentStatuses
from vinta_billing.models import BillingPlan, Payment, Subscription

from organizations.models import Organization
from payments.tests.historical_apps import historical_apps
from payments.tests.provider_settings import use_providers


# These modules build their own Subscription rows (OneToOne with Organization),
# so they opt out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription


_repoint_module = importlib.import_module(
    "payments.migrations.0018_repoint_subscription_payment_provider"
)
repoint_to_organization_provider = _repoint_module.repoint_to_organization_provider
restore_previous_payment_provider = _repoint_module.restore_previous_payment_provider

_payment_backfill_module = importlib.import_module(
    "payments.migrations.0019_backfill_subscription_payment_provider_on_payments"
)
backfill_from_subscription = _payment_backfill_module.backfill_from_subscription
unset_on_subscription_payments = _payment_backfill_module.unset_on_subscription_payments


def _billing_profile_for(organization: Organization, provider: str):
    return baker.make(
        "vinta_billing.BillingProfile",
        organization=organization,
        contact_email="billing@example.com",
        document_type="CPF",
        document_number="12345678900",
        billing_address=baker.make("vinta_billing.BillingAddress"),
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
        use_providers(settings, default_provider=PaymentProviders.STRIPE)
        org = baker.make(Organization, parent=None)
        _billing_profile_for(org, PaymentProviders.STRIPE)
        subscription = _subscription(org, plan, PaymentProviders.MERCADOPAGO)

        repoint_to_organization_provider(historical_apps, None)

        subscription.refresh_from_db()
        assert subscription.payment_provider == PaymentProviders.STRIPE

    def test_mercadopago_pinned_org_subscription_is_left_alone(self, plan, settings):
        """The discriminating half: an organization genuinely pinned to
        MercadoPago keeps ``mercadopago``, so the migration is a *repoint*, not a
        blanket rewrite to the default."""
        use_providers(settings, default_provider=PaymentProviders.STRIPE)
        org = baker.make(Organization, parent=None)
        _billing_profile_for(org, PaymentProviders.MERCADOPAGO)
        subscription = _subscription(org, plan, PaymentProviders.MERCADOPAGO)

        repoint_to_organization_provider(historical_apps, None)

        subscription.refresh_from_db()
        assert subscription.payment_provider == PaymentProviders.MERCADOPAGO

    def test_unpinned_and_profileless_orgs_take_the_system_default(self, plan, settings):
        use_providers(settings, default_provider=PaymentProviders.STRIPE)
        unpinned_org = baker.make(Organization, parent=None)
        _billing_profile_for(unpinned_org, "")
        unpinned = _subscription(unpinned_org, plan, PaymentProviders.MERCADOPAGO)
        profileless_org = baker.make(Organization, parent=None)
        profileless = _subscription(profileless_org, plan, PaymentProviders.MERCADOPAGO)

        repoint_to_organization_provider(historical_apps, None)

        unpinned.refresh_from_db()
        profileless.refresh_from_db()
        assert unpinned.payment_provider == PaymentProviders.STRIPE
        assert profileless.payment_provider == PaymentProviders.STRIPE

    def test_reverse_restores_each_repointed_rows_own_pre_migration_value(self, plan, settings):
        """Every pre-migration row this test builds said ``mercadopago`` -- the
        hardcode in ``create_subscription_for_organization`` and
        ``payments.0009`` are the only two writers there ever were -- so that is
        what the reverse restores it to, per-row, via the ``meta`` stamp the
        forward pass leaves behind (not a single hardcoded literal)."""
        use_providers(settings, default_provider=PaymentProviders.STRIPE)
        org = baker.make(Organization, parent=None)
        _billing_profile_for(org, PaymentProviders.STRIPE)
        subscription = _subscription(org, plan, PaymentProviders.MERCADOPAGO)

        repoint_to_organization_provider(historical_apps, None)
        subscription.refresh_from_db()
        assert subscription.payment_provider == PaymentProviders.STRIPE

        restore_previous_payment_provider(historical_apps, None)

        subscription.refresh_from_db()
        assert subscription.payment_provider == PaymentProviders.MERCADOPAGO

    def test_reverse_leaves_a_row_the_forward_pass_did_not_touch_unchanged(self, plan, settings):
        """Second-pass Tier 4 fix: the reverse must be scoped to the rows the
        forward pass actually changed. Proof: a row created *after* the forward
        pass ran (simulating a new organization under a changed
        ``DEFAULT_PAYMENT_PROVIDER``, or one the forward pass left alone because
        it already agreed) must survive the reverse untouched -- the bug this
        fixes rewrote every ``Subscription`` in the table on reverse, which would
        also destroy the evidence needed to verify, on rollback, that no
        Stripe-provider Payment or Subscription rows were created in the
        window."""
        use_providers(settings, default_provider=PaymentProviders.STRIPE)
        repointed_org = baker.make(Organization, parent=None)
        _billing_profile_for(repointed_org, PaymentProviders.STRIPE)
        repointed = _subscription(repointed_org, plan, PaymentProviders.MERCADOPAGO)

        repoint_to_organization_provider(historical_apps, None)
        repointed.refresh_from_db()
        assert repointed.payment_provider == PaymentProviders.STRIPE

        # Built *after* the forward pass ran, already correctly stamped --
        # nothing for the forward pass to have touched.
        untouched_org = baker.make(Organization, parent=None)
        _billing_profile_for(untouched_org, PaymentProviders.STRIPE)
        untouched = _subscription(untouched_org, plan, PaymentProviders.STRIPE)

        restore_previous_payment_provider(historical_apps, None)

        repointed.refresh_from_db()
        untouched.refresh_from_db()
        assert repointed.payment_provider == PaymentProviders.MERCADOPAGO
        assert untouched.payment_provider == PaymentProviders.STRIPE

    def test_is_idempotent(self, plan, settings):
        use_providers(settings, default_provider=PaymentProviders.STRIPE)
        org = baker.make(Organization, parent=None)
        _billing_profile_for(org, PaymentProviders.STRIPE)
        subscription = _subscription(org, plan, PaymentProviders.MERCADOPAGO)

        repoint_to_organization_provider(historical_apps, None)
        repoint_to_organization_provider(historical_apps, None)

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

        backfill_from_subscription(historical_apps, None)

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

        backfill_from_subscription(historical_apps, None)

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

        backfill_from_subscription(historical_apps, None)

        payment.refresh_from_db()
        assert payment.payment_provider == ""

    def test_reverse_restores_the_pre_migration_empty_provider(self, plan):
        org = baker.make(Organization, parent=None)
        billing_profile = _billing_profile_for(org, PaymentProviders.MERCADOPAGO)
        subscription = _subscription(org, plan, PaymentProviders.MERCADOPAGO)
        payment = self._payment(billing_profile, subscription, "", "mp-sub-payment-1")

        backfill_from_subscription(historical_apps, None)
        payment.refresh_from_db()
        assert payment.payment_provider == PaymentProviders.MERCADOPAGO

        unset_on_subscription_payments(historical_apps, None)

        payment.refresh_from_db()
        assert payment.payment_provider == ""

    def test_reverse_leaves_a_row_the_forward_pass_did_not_touch_unchanged(self, plan):
        """Second-pass Tier 4 fix: the reverse must be scoped to the rows the
        forward pass actually filled. Proof: a ``Payment`` that already carried a
        provider before the forward pass ran (so the forward pass skipped it --
        see ``test_rows_that_already_carry_a_provider_are_untouched``) must
        survive the reverse untouched. The bug this fixes blanked
        ``payment_provider`` on every subscription-linked ``Payment`` on
        reverse -- combined with ``0018``'s (now-fixed) reverse, that would have
        rewritten every Stripe-provider subscription charge to ``mercadopago``
        on `migrate payments 0017`."""
        org = baker.make(Organization, parent=None)
        billing_profile = _billing_profile_for(org, PaymentProviders.MERCADOPAGO)
        subscription = _subscription(org, plan, PaymentProviders.MERCADOPAGO)
        filled = self._payment(billing_profile, subscription, "", "mp-sub-payment-1")

        stripe_org = baker.make(Organization, parent=None)
        stripe_billing_profile = _billing_profile_for(stripe_org, PaymentProviders.STRIPE)
        stripe_subscription = _subscription(stripe_org, plan, PaymentProviders.STRIPE)
        # Already carries a provider before the forward pass runs -- the forward
        # pass's `payment_provider=""` filter skips it, so it is untouched by
        # construction (not merely by coincidence of its final value).
        already_stamped = self._payment(
            stripe_billing_profile, stripe_subscription, PaymentProviders.STRIPE, "stripe-payment-1"
        )

        backfill_from_subscription(historical_apps, None)
        filled.refresh_from_db()
        assert filled.payment_provider == PaymentProviders.MERCADOPAGO

        unset_on_subscription_payments(historical_apps, None)

        filled.refresh_from_db()
        already_stamped.refresh_from_db()
        assert filled.payment_provider == ""
        assert already_stamped.payment_provider == PaymentProviders.STRIPE

    def test_is_idempotent(self, plan):
        org = baker.make(Organization, parent=None)
        billing_profile = _billing_profile_for(org, PaymentProviders.MERCADOPAGO)
        subscription = _subscription(org, plan, PaymentProviders.MERCADOPAGO)
        payment = self._payment(billing_profile, subscription, "", "mp-sub-payment-1")

        backfill_from_subscription(historical_apps, None)
        backfill_from_subscription(historical_apps, None)

        payment.refresh_from_db()
        assert payment.payment_provider == PaymentProviders.MERCADOPAGO
