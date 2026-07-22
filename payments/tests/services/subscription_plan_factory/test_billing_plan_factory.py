import datetime
from decimal import Decimal

import pytest
from model_bakery import baker

from organizations.models import Organization
from payments.billing_constants import BillingInterval
from payments.constants import PaymentProviders
from payments.models import BillingPlan, Subscription
from payments.services.subscription_plan_factory.billing_plan_factory import BillingPlanFactory


# This module builds its own Subscription rows (OneToOne with Organization), so it
# opts out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription


@pytest.fixture
def organization():
    return baker.make(Organization)


@pytest.fixture
def billing_plan():
    return baker.make(
        BillingPlan,
        name="Pro",
        monthly_price=Decimal("100"),
        annual_price=Decimal("1000"),
        currency="USD",
    )


@pytest.fixture
def subscription(organization, billing_plan):
    now = datetime.datetime(2026, 3, 15, tzinfo=datetime.UTC)
    return baker.make(
        Subscription,
        organization=organization,
        plan=billing_plan,
        billing_interval=BillingInterval.MONTHLY,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
        payment_provider=PaymentProviders.MERCADOPAGO,
        plan_external_id="ext-plan-123",
    )


@pytest.mark.django_db
class TestBillingPlanFactory:
    def test_make_plan_from_subscription_resolves_monthly_price(self, subscription, billing_plan):
        created_plan = BillingPlanFactory().make_plan_from_subscription(subscription)

        assert created_plan.id == billing_plan.pk
        assert created_plan.name == billing_plan.name
        assert created_plan.value == billing_plan.monthly_price
        assert created_plan.currency == billing_plan.currency
        assert created_plan.billing_day == subscription.current_period_start.day
        assert created_plan.external_id == subscription.plan_external_id

    def test_make_plan_from_subscription_resolves_annual_price(self, organization, billing_plan):
        now = datetime.datetime(2026, 1, 5, tzinfo=datetime.UTC)
        annual_subscription = baker.make(
            Subscription,
            organization=organization,
            plan=billing_plan,
            billing_interval=BillingInterval.ANNUAL,
            current_period_start=now,
            current_period_end=now + datetime.timedelta(days=365),
            payment_provider=PaymentProviders.MERCADOPAGO,
        )

        created_plan = BillingPlanFactory().make_plan_from_subscription(annual_subscription)

        assert created_plan.value == billing_plan.annual_price
        assert created_plan.billing_day == 5

    def test_billing_day_is_clamped_to_28(self, organization, billing_plan):
        """Providers commonly reject or mishandle billing_day > 28 for monthly
        recurrence (not every month has a 29th/30th/31st). A period anchored on
        one of those days must still resolve to a billable day."""
        now = datetime.datetime(2026, 1, 31, tzinfo=datetime.UTC)
        subscription = baker.make(
            Subscription,
            organization=organization,
            plan=billing_plan,
            billing_interval=BillingInterval.MONTHLY,
            current_period_start=now,
            current_period_end=now + datetime.timedelta(days=30),
            payment_provider=PaymentProviders.MERCADOPAGO,
        )

        created_plan = BillingPlanFactory().make_plan_from_subscription(subscription)

        assert created_plan.billing_day == 28

    def test_falls_back_to_monthly_price_when_annual_price_is_missing(self, organization):
        plan_without_annual_price = baker.make(
            BillingPlan, monthly_price=Decimal("50"), annual_price=None, currency="USD"
        )
        now = datetime.datetime(2026, 1, 5, tzinfo=datetime.UTC)
        annual_subscription = baker.make(
            Subscription,
            organization=organization,
            plan=plan_without_annual_price,
            billing_interval=BillingInterval.ANNUAL,
            current_period_start=now,
            current_period_end=now + datetime.timedelta(days=365),
            payment_provider=PaymentProviders.MERCADOPAGO,
        )

        created_plan = BillingPlanFactory().make_plan_from_subscription(annual_subscription)

        assert created_plan.value == plan_without_annual_price.monthly_price
