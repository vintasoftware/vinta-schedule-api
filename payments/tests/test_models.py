import datetime

from django.core.exceptions import ValidationError
from django.db import IntegrityError

import pytest
from model_bakery import baker

from organizations.models import Organization
from payments.constants import PaymentProviders, PaymentStatuses, RefundStatuses
from payments.models import (
    BillingAddress,
    BillingPlan,
    BillingProfile,
    Payment,
    Refund,
    RefundStatusUpdate,
    Subscription,
)


# This module builds its own Subscription rows (OneToOne with Organization), so it
# opts out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription


@pytest.fixture
def organization():
    return baker.make(Organization)


@pytest.fixture
def billing_address():
    return baker.make(BillingAddress)


@pytest.fixture
def billing_profile(organization, billing_address):
    return baker.make(
        BillingProfile,
        organization=organization,
        billing_address=billing_address,
    )


@pytest.fixture
def billing_plan():
    return baker.make(BillingPlan)


@pytest.mark.django_db
class TestBillingPlan:
    def test_only_one_default_plan_allowed(self):
        """The `uniq_default_billing_plan` partial unique constraint enforces at
        most one `is_default_for_new_organizations=True` row. The plan catalog seed
        migration already put `unlimited` in that slot, so a second default row must
        be rejected without this test creating the first one itself."""
        assert BillingPlan.objects.filter(is_default_for_new_organizations=True).exists()

        with pytest.raises(IntegrityError):
            baker.make(BillingPlan, is_default_for_new_organizations=True)

    def test_multiple_non_default_plans_allowed(self):
        """The partial constraint only applies to `True` rows."""
        before = BillingPlan.objects.filter(is_default_for_new_organizations=False).count()

        baker.make(BillingPlan, is_default_for_new_organizations=False)
        baker.make(BillingPlan, is_default_for_new_organizations=False)

        assert (
            BillingPlan.objects.filter(is_default_for_new_organizations=False).count() == before + 2
        )


@pytest.mark.django_db
class TestBillingProfile:
    def test_billing_profile_reachable_from_organization(self, organization, billing_profile):
        """The organization is the primary key: `organization.billing_profile` round-trips."""
        assert organization.billing_profile == billing_profile
        assert billing_profile.organization == organization
        assert billing_profile.pk == organization.pk

    def test_billing_address_organization_property(self, billing_profile):
        assert billing_profile.billing_address.organization == billing_profile.organization


@pytest.mark.django_db
class TestSubscription:
    def test_plan_resolves_without_raising(self, organization, billing_plan):
        """`Subscription.plan` is now a real field, not the old broken property."""
        now = datetime.datetime.now(tz=datetime.UTC)
        subscription = baker.make(
            Subscription,
            organization=organization,
            plan=billing_plan,
            current_period_start=now,
            current_period_end=now + datetime.timedelta(days=30),
            payment_provider=PaymentProviders.MERCADOPAGO,
        )

        assert subscription.plan == billing_plan

    def test_organization_round_trips(self, organization, billing_plan):
        now = datetime.datetime.now(tz=datetime.UTC)
        subscription = baker.make(
            Subscription,
            organization=organization,
            plan=billing_plan,
            current_period_start=now,
            current_period_end=now + datetime.timedelta(days=30),
            payment_provider=PaymentProviders.MERCADOPAGO,
        )

        assert organization.subscription == subscription


@pytest.mark.django_db
class TestPayment:
    def test_organization_property(self, billing_profile):
        payment = baker.make(Payment, billing_profile=billing_profile)

        assert payment.organization == billing_profile.organization


@pytest.mark.django_db
class TestRefundStatusUpdate:
    def test_str_does_not_raise(self, billing_profile):
        payment = baker.make(Payment, billing_profile=billing_profile)
        refund = baker.make(Refund, payment=payment)
        status_update = baker.make(
            RefundStatusUpdate,
            refund=refund,
            status=RefundStatuses.PENDING,
        )

        # The old __str__ referenced `self.payment`, which does not exist on
        # RefundStatusUpdate — this must not raise AttributeError.
        rendered = str(status_update)

        assert str(refund) in rendered

    def test_status_field_uses_refund_statuses_choices(self):
        """The old bug used PaymentStatuses (which includes e.g. `in_mediation`,
        not a valid RefundStatuses member) for this field."""
        field = RefundStatusUpdate._meta.get_field("status")

        assert set(field.choices) == set(RefundStatuses.choices)

    def test_full_clean_rejects_payment_only_status(self, billing_profile):
        """A `PaymentStatuses`-only value (e.g. `in_mediation`) is not a valid
        `RefundStatuses` member and must fail `full_clean()`."""
        payment = baker.make(Payment, billing_profile=billing_profile)
        refund = baker.make(Refund, payment=payment)
        status_update = RefundStatusUpdate(
            refund=refund,
            status=PaymentStatuses.IN_MEDIATION,
        )

        with pytest.raises(ValidationError):
            status_update.full_clean()
