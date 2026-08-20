"""Host-side coverage of package-owned, package-untested guards on
``vinta_billing.services.subscription_service.SubscriptionService
.retry_failed_charge``.

This module exists because the behaviour it pins is documented at length in
the package (see ``retry_failed_charge``'s own docstring,
``vinta_billing/services/subscription_service.py:709-718`` in particular) but
is not exercised by any real test in either codebase. The package's
``tests/test_dunning.py`` drives the dunning ladder against a
``FakeSubscriptionService`` whose ``retry_failed_charge`` is a no-op recorder,
never the real method; a repo-wide grep of the package's ``tests/`` for
``retry_failed_charge`` returns only that fake and one docstring mention.

Trimmed from the host's deleted ``test_dunning_service.py`` (see
``git show ca95b59e^:payments/tests/services/test_dunning_service.py``) --
carrying forward only the fixtures and doubles the surviving tests actually
need. ``test_charge_declined_returns_unchanged_without_raising`` was
deliberately **not** restored: that outcome is covered end to end by
``payments/tests/test_dunning_schedule.py``'s
``TestDunningTickToleratesADeclinedStripeCharge`` against the real DI stack.

These are candidates to move upstream into ``vinta-django-billing`` in a
future release, once the package carries its own coverage of
``retry_failed_charge``.
"""

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.utils import timezone

import pytest
from model_bakery import baker

from organizations.models import Organization, OrganizationMembership
from payments.billing_constants import BillingState, LimitedResource, LimitKind
from payments.constants import PaymentProviders
from payments.exceptions import CollectionNotSupportedError, NoOutstandingBalanceError
from payments.models import BillingPlan, PaymentMethod, PlanLimit, Subscription
from payments.services.dataclasses import CreatedPlan
from payments.services.dunning_service import DunningService
from payments.services.entitlement_service import EntitlementService
from payments.services.subscription_service import SubscriptionService
from users.models import User


pytestmark = pytest.mark.no_auto_subscription


def make_complete_plan(
    limit_values: dict[str, int | None] | None = None,
    *,
    monthly_price: Decimal = Decimal("0"),
    grace_period_days: int | None = None,
) -> BillingPlan:
    """A catalog plan carrying a ``PlanLimit`` row for every ``LimitedResource``
    member -- what ``assert_plan_is_complete`` requires. Mirrors
    ``test_plan_change.py``'s helper of the same name."""
    limit_values = limit_values or {}
    plan = baker.make(
        BillingPlan,
        is_default_for_new_organizations=False,
        monthly_price=monthly_price,
        annual_price=None,
        grace_period_days=grace_period_days,
    )
    for resource_key in LimitedResource.values:
        baker.make(
            PlanLimit,
            plan=plan,
            resource_key=resource_key,
            limit_value=limit_values.get(resource_key, 0),
            kind=LimitKind.PREPAID,
        )
    return plan


@pytest.fixture
def organization():
    return baker.make(Organization, parent=None, can_invite_organizations=False)


@pytest.fixture
def billing_profile(organization):
    billing_address = baker.make(
        "vinta_billing.BillingAddress",
        street_name="Test Street",
        street_number="123",
        city="Test City",
        state="Test State",
        country="Test Country",
        zip_code="12345",
    )
    return baker.make(
        "vinta_billing.BillingProfile",
        organization=organization,
        contact_email="billing@example.com",
        document_type="CPF",
        document_number="12345678900",
        billing_address=billing_address,
    )


def _subscription_for(
    organization: Organization,
    plan: BillingPlan,
    *,
    billing_state: str = BillingState.ACTIVE,
    external_id: str = "already-on-file",
    grace_period_ends_at=None,
) -> Subscription:
    subscription = SubscriptionService().create_subscription_for_organization(
        organization, plan=plan
    )
    assert subscription is not None
    subscription.billing_state = billing_state
    subscription.external_id = external_id
    subscription.grace_period_ends_at = grace_period_ends_at
    subscription.save(update_fields=["billing_state", "external_id", "grace_period_ends_at"])
    return subscription


def _seed_members(organization: Organization, count: int) -> None:
    """``count`` seat-occupying members, e.g. to push usage over the seeded
    ``free`` plan's ``organization_members`` limit (5) so
    ``DunningService.check_free_fallback`` does not short-circuit a test that
    means to exercise the retry path instead."""
    for _ in range(count):
        baker.make(
            OrganizationMembership,
            organization=organization,
            user=baker.make(User),
            is_active=True,
        )


@dataclass
class FakePaymentService:
    """Same hand-written double ``test_plan_change.py`` uses -- precise about
    *when* the provider is driven, not its wire shape.

    ``raise_collection_not_supported``/``raise_no_outstanding_balance`` let a
    test drive ``pay_outstanding_invoice`` down the two error branches
    ``SubscriptionService.retry_failed_charge`` must tolerate: MercadoPago's
    typed refusal (falls back to ``change_subscription_plan``) and "nothing
    owed right now" (swallowed, the tick is a no-op).
    """

    plan_external_id: str = "ext-plan-1"
    calls: list[str] = field(default_factory=list)
    create_subscription_plan_providers: list[str] = field(default_factory=list)
    pay_outstanding_invoice_calls: list[tuple[str, str]] = field(default_factory=list)
    raise_collection_not_supported: bool = False
    raise_no_outstanding_balance: bool = False

    def create_subscription_plan(self, plan, provider: str) -> CreatedPlan:
        self.calls.append("create_subscription_plan")
        self.create_subscription_plan_providers.append(provider)
        return CreatedPlan(
            id=plan.id,
            name=plan.name,
            value=plan.value,
            currency=plan.currency,
            billing_day=plan.billing_day,
            billing_interval=plan.billing_interval,
            external_id=self.plan_external_id,
        )

    def change_subscription_plan(self, subscription, new_plan, idempotency_key: str = "") -> None:
        self.calls.append("change_subscription_plan")

    def pay_outstanding_invoice(
        self, subscription, payment_token: str = "", idempotency_key: str = ""
    ) -> None:
        self.calls.append("pay_outstanding_invoice")
        self.pay_outstanding_invoice_calls.append((payment_token, idempotency_key))
        if self.raise_collection_not_supported:
            raise CollectionNotSupportedError(
                subscription.id, "MercadoPago has no verified collection primitive"
            )
        if self.raise_no_outstanding_balance:
            raise NoOutstandingBalanceError(subscription.id)


@pytest.fixture
def fake_payment_service():
    return FakePaymentService()


@pytest.fixture
def subscription_service(fake_payment_service):
    return SubscriptionService(payment_service=fake_payment_service)


@pytest.fixture
def entitlement_service():
    return EntitlementService()


@pytest.fixture
def mock_notification_service():
    return MagicMock()


@pytest.fixture
def dunning_service(subscription_service, entitlement_service, mock_notification_service):
    return DunningService(
        subscription_service=subscription_service,
        entitlement_service=entitlement_service,
        notification_service=mock_notification_service,
    )


def _patch_on_commit():
    """Canonical pattern in this project for testing on_commit-wrapped side
    effects synchronously -- see
    ``calendar_integration/tests/services/test_change_request_notifications.py``."""
    return patch(
        "vinta_billing.services.dunning_service.transaction.on_commit",
        side_effect=lambda fn: fn(),
    )


@pytest.mark.django_db
class TestRetryFailedChargeToleratesCollectionNotSupported:
    """Pins ``retry_failed_charge``'s ``CollectionNotSupportedError`` handling
    -- see ``vinta_billing/services/subscription_service.py:709-718``."""

    def test_retry_drives_the_subscriptions_own_provider_not_the_organizations_pin(
        self, dunning_service, fake_payment_service, organization, billing_profile
    ):
        """MercadoPago's typed refusal falls back to
        ``_ensure_provider_plan`` + ``change_subscription_plan``, in that
        order, after the primary ``pay_outstanding_invoice`` attempt --
        MercadoPago's ladder is byte-identical to its behavior before
        ``pay_outstanding_invoice`` became the ladder's primary collection
        attempt."""
        fake_payment_service.raise_collection_not_supported = True
        billing_profile.payment_provider = PaymentProviders.STRIPE
        billing_profile.save(update_fields=["payment_provider"])
        plan = make_complete_plan()
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=BillingState.GRACE,
            grace_period_ends_at=timezone.now() + datetime.timedelta(days=5),
        )
        subscription.payment_provider = PaymentProviders.MERCADOPAGO
        subscription.save(update_fields=["payment_provider"])
        _seed_members(organization, 6)

        with _patch_on_commit():
            dunning_service.process_subscription(subscription)

        assert fake_payment_service.create_subscription_plan_providers == [
            PaymentProviders.MERCADOPAGO
        ]
        assert fake_payment_service.calls == [
            "pay_outstanding_invoice",
            "create_subscription_plan",
            "change_subscription_plan",
        ]

    def test_collection_not_supported_reraises_for_a_non_mercadopago_provider(
        self, dunning_service, fake_payment_service, organization, billing_profile
    ):
        """The ``CollectionNotSupportedError`` -> fallback path is a temporary
        concession pinned to MercadoPago's specific, unverified state (see
        ``retry_failed_charge``'s docstring) -- it must not also silently
        route a Stripe (or any other) subscription into
        ``_ensure_provider_plan`` + ``change_subscription_plan``, the exact
        operation a live Stripe probe proved collects **$0.00** against a
        real past-due invoice. Nothing raises ``CollectionNotSupportedError``
        for Stripe today (only ``MercadoPagoSubscriptionAdapter`` does), so
        this is the latent-but-real case: it must fail loudly rather than
        fall back on a guess."""
        fake_payment_service.raise_collection_not_supported = True
        plan = make_complete_plan()
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=BillingState.GRACE,
            grace_period_ends_at=timezone.now() + datetime.timedelta(days=5),
        )
        subscription.payment_provider = PaymentProviders.STRIPE
        subscription.save(update_fields=["payment_provider"])
        _seed_members(organization, 6)

        with _patch_on_commit(), pytest.raises(CollectionNotSupportedError):
            dunning_service.process_subscription(subscription)

        # No fallback drove `_ensure_provider_plan`/`change_subscription_plan`
        # against Stripe -- the $0.00-collection operation the fallback guard
        # exists to keep the ladder away from.
        assert fake_payment_service.calls == ["pay_outstanding_invoice"]


@pytest.mark.django_db
class TestRetryFailedChargeTolerantOfNonFatalOutcomes:
    """``retry_failed_charge`` is a background beat tick
    (``CELERY_TASK_ACKS_LATE``) -- it must never raise out of a legitimate
    "nothing to do" outcome, unlike ``retry_payment`` (the user-facing
    endpoint), which surfaces the analogous 409s instead. See
    ``payments/tests/views/test_billing_views.py``'s 409 coverage of that
    user-facing endpoint, which the package's ``retry_failed_charge``
    docstring explicitly distinguishes from this beat-tick path."""

    def test_blank_external_id_returns_unchanged_without_raising(
        self, subscription_service, fake_payment_service, organization, billing_profile
    ):
        plan = make_complete_plan()
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=BillingState.GRACE,
            external_id="",
            grace_period_ends_at=timezone.now() + datetime.timedelta(days=5),
        )

        result = subscription_service.retry_failed_charge(subscription, "dunning-retry-1-0")

        assert result == subscription
        assert fake_payment_service.calls == []

    def test_no_outstanding_balance_returns_unchanged_without_raising(
        self, subscription_service, fake_payment_service, organization, billing_profile
    ):
        fake_payment_service.raise_no_outstanding_balance = True
        plan = make_complete_plan()
        subscription = _subscription_for(
            organization,
            plan,
            billing_state=BillingState.GRACE,
            grace_period_ends_at=timezone.now() + datetime.timedelta(days=5),
        )

        result = subscription_service.retry_failed_charge(subscription, "dunning-retry-1-0")

        assert result == subscription
        assert fake_payment_service.calls == ["pay_outstanding_invoice"]
        # No fallback to `change_subscription_plan` here -- "nothing owed" is
        # not "the provider can't collect at all", it is simply a no-op tick.
        assert "change_subscription_plan" not in fake_payment_service.calls


@pytest.mark.django_db
class TestConstraint1PaymentMethodStaysTrueInGrace:
    def test_has_payment_method_survives_entering_grace(
        self, dunning_service, entitlement_service, organization, billing_profile
    ):
        """``enter_grace`` must never touch ``PaymentMethod`` -- a failed charge
        says nothing about whether the card is still attached. An organization
        with a card on file must keep reading ``has_payment_method() is True``
        after moving to GRACE, so it keeps accruing postpaid usage; the dunning
        ladder, not the postpaid guard, is what escalates it (Constraint 1).

        ``calendar_integration/tests/services/test_postpaid_enforcement.py``
        is not a substitute for this test -- it sets ``billing_state`` directly
        rather than going through ``enter_grace``, so a future package version
        that deactivated the card on entering grace would leave it green."""
        plan = make_complete_plan(grace_period_days=7)
        subscription = _subscription_for(organization, plan, billing_state=BillingState.ACTIVE)
        baker.make(
            PaymentMethod,
            organization=organization,
            provider=PaymentProviders.MERCADOPAGO,
            external_id="card-on-file",
            is_active=True,
        )
        assert entitlement_service.has_payment_method(organization) is True

        with _patch_on_commit():
            dunning_service.enter_grace(subscription)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.GRACE
        assert entitlement_service.has_payment_method(organization) is True
        assert PaymentMethod.objects.filter(organization=organization, is_active=True).count() == 1
