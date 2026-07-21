"""Phase 9: upgrade / downgrade / add-on purchase orchestration on
``SubscriptionService``.

Every test here drives a hand-written ``FakePaymentService`` double rather than
mocking individual adapter calls -- what matters to this suite is *when* the
provider is driven and *when* capacity is granted, not the wire shape of any one
provider (that is the adapter tests' job, e.g.
``test_mercadopago_subscription_adapter.py``/``test_stripe_subscription_adapter.py``).
"""

import datetime
from dataclasses import dataclass, field
from decimal import Decimal

from django.utils import timezone

import pytest
from model_bakery import baker

from organizations.models import Organization
from payments.billing_constants import (
    BillingInterval,
    BillingState,
    Entitlement,
    LimitedResource,
    LimitKind,
)
from payments.constants import PaymentProviders, PaymentStatuses
from payments.exceptions import (
    AddOnNotPurchasableError,
    IncompleteBillingPlanError,
    PaymentTokenRequiredError,
    UnconfirmedPlanChangeError,
)
from payments.models import (
    BillingPlan,
    Payment,
    PaymentMethod,
    PlanEntitlement,
    PlanLimit,
    Subscription,
    SubscriptionAddOn,
    SubscriptionPlanLimit,
)
from payments.services.dataclasses import CreatedPlan
from payments.services.entitlement_service import EntitlementService
from payments.services.subscription_service import SubscriptionService


# This module builds its own Subscription rows (OneToOne with Organization), so it
# opts out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription


def make_complete_plan(
    limit_values: dict[str, int | None] | None = None,
    *,
    monthly_price: Decimal = Decimal("0"),
    annual_price: Decimal | None = None,
    grace_period_days: int | None = None,
    overage_unit_price: Decimal | None = None,
) -> BillingPlan:
    """A catalog plan carrying a ``PlanLimit`` row for every ``LimitedResource``
    member -- what ``assert_plan_is_complete`` requires. Mirrors
    ``test_subscription_service.py``'s helper of the same name."""
    limit_values = limit_values or {}
    plan = baker.make(
        BillingPlan,
        is_default_for_new_organizations=False,
        monthly_price=monthly_price,
        annual_price=annual_price,
        grace_period_days=grace_period_days,
    )
    for resource_key in LimitedResource.values:
        baker.make(
            PlanLimit,
            plan=plan,
            resource_key=resource_key,
            limit_value=limit_values.get(resource_key, 0),
            kind=LimitKind.PREPAID,
            overage_unit_price=overage_unit_price if resource_key in limit_values else None,
        )
    return plan


@pytest.fixture
def organization():
    return baker.make(Organization, parent=None, can_invite_organizations=False)


@pytest.fixture
def billing_profile(organization):
    billing_address = baker.make(
        "payments.BillingAddress",
        street_name="Test Street",
        street_number="123",
        city="Test City",
        state="Test State",
        country="Test Country",
        zip_code="12345",
    )
    return baker.make(
        "payments.BillingProfile",
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
    billing_interval: str = BillingInterval.MONTHLY,
    external_id: str = "",
    billing_state: str = BillingState.FREE,
) -> Subscription:
    """Build a ``Subscription`` on ``plan`` with real ``SubscriptionPlanLimit``/
    ``SubscriptionEntitlement`` copies -- goes through
    ``SubscriptionService.create_subscription_for_organization`` rather than
    ``baker.make(Subscription, ...)`` directly, since the copies (not the
    ``Subscription.plan`` FK) are what ``EntitlementService``/``purchase_add_on``
    actually read.
    """
    subscription = SubscriptionService().create_subscription_for_organization(
        organization, plan=plan
    )
    assert subscription is not None
    subscription.billing_interval = billing_interval
    subscription.external_id = external_id
    subscription.billing_state = billing_state
    subscription.save(update_fields=["billing_interval", "external_id", "billing_state"])
    return subscription


@dataclass
class FakePaymentService:
    """A hand-written double over the ``PaymentService`` surface
    ``SubscriptionService`` drives -- precise about *when* each call happens,
    which is exactly what this phase's tests need to prove."""

    plan_external_id: str = "ext-plan-1"
    subscription_external_id: str = "ext-sub-1"
    payment_external_id: str = "ext-payment-1"
    calls: list[str] = field(default_factory=list)
    #: Every idempotency key forwarded to a provider-driving call, in order, so
    #: tests can assert the client-supplied key actually reaches the provider.
    idempotency_keys: list[str] = field(default_factory=list)

    def create_subscription_plan(self, plan) -> CreatedPlan:
        self.calls.append("create_subscription_plan")
        return CreatedPlan(
            id=plan.id,
            name=plan.name,
            value=plan.value,
            currency=plan.currency,
            billing_day=plan.billing_day,
            billing_interval=plan.billing_interval,
            external_id=self.plan_external_id,
        )

    def process_subscription(
        self, subscription: Subscription, payment_token: str, idempotency_key: str = ""
    ) -> Subscription:
        self.calls.append("process_subscription")
        self.idempotency_keys.append(idempotency_key)
        subscription.external_id = self.subscription_external_id
        subscription.save(update_fields=["external_id"])
        return subscription

    def change_subscription_plan(
        self, subscription: Subscription, new_plan: CreatedPlan, idempotency_key: str = ""
    ) -> None:
        self.calls.append("change_subscription_plan")
        self.idempotency_keys.append(idempotency_key)

    def create_payment(
        self,
        *,
        organization: Organization,
        currency: str,
        amount: Decimal,
        description: str,
        payment_method: str,
        payment_token: str,
        idempotency_key: str = "",
    ) -> Payment:
        self.calls.append("create_payment")
        self.idempotency_keys.append(idempotency_key)
        return baker.make(
            "payments.Payment",
            billing_profile=organization.billing_profile,
            currency=currency,
            value=amount,
            description=description,
            payment_method=payment_method,
            status=PaymentStatuses.PENDING,
            payment_provider=PaymentProviders.MERCADOPAGO,
            external_id=self.payment_external_id,
        )

    def cancel_subscription(self, subscription: Subscription) -> None:
        self.calls.append("cancel_subscription")


@pytest.fixture
def fake_payment_service():
    return FakePaymentService()


@pytest.fixture
def service(fake_payment_service):
    return SubscriptionService(payment_service=fake_payment_service)


@pytest.mark.django_db
class TestUpgrade:
    def test_upgrade_flips_plan_immediately_but_grants_no_capacity(
        self, service, fake_payment_service, organization, billing_profile
    ):
        free_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 3}, monthly_price=Decimal("0")
        )
        pro_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 50}, monthly_price=Decimal("50")
        )
        subscription = _subscription_for(organization, free_plan)

        result = service.request_plan_change(
            subscription, pro_plan, BillingInterval.MONTHLY, payment_token="tok-1"
        )

        result.refresh_from_db()
        assert result.plan_id == pro_plan.pk
        # Capacity is NOT granted synchronously -- an initiated-but-unconfirmed
        # upgrade must not lift the ceiling.
        limit = result.limits.get(resource_key=LimitedResource.ORGANIZATION_MEMBERS)
        assert limit.limit_value == 3
        assert result.billing_state == BillingState.FREE
        # First-ever paid plan: create the provider-side plan, then attach the
        # card via `process_subscription` (no existing external_id to move).
        assert fake_payment_service.calls == ["create_subscription_plan", "process_subscription"]
        assert result.external_id == fake_payment_service.subscription_external_id

    def test_upgrade_without_a_token_when_none_on_file_raises_and_writes_nothing(
        self, service, fake_payment_service, organization, billing_profile
    ):
        free_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 3}, monthly_price=Decimal("0")
        )
        pro_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 50}, monthly_price=Decimal("50")
        )
        subscription = _subscription_for(organization, free_plan)

        with pytest.raises(PaymentTokenRequiredError):
            service.request_plan_change(subscription, pro_plan, BillingInterval.MONTHLY)

        subscription.refresh_from_db()
        assert subscription.plan_id == free_plan.pk
        assert fake_payment_service.calls == []

    def test_second_upgrade_reuses_the_existing_instrument_no_token_needed(
        self, service, fake_payment_service, organization, billing_profile
    ):
        free_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 3}, monthly_price=Decimal("0")
        )
        pro_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 50}, monthly_price=Decimal("50")
        )
        premium_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 200}, monthly_price=Decimal("200")
        )
        subscription = _subscription_for(organization, free_plan, external_id="already-on-file")

        result = service.request_plan_change(subscription, pro_plan, BillingInterval.MONTHLY)
        service.confirm_plan_change(result)

        result = service.request_plan_change(subscription, premium_plan, BillingInterval.MONTHLY)

        assert fake_payment_service.calls == [
            "create_subscription_plan",
            "change_subscription_plan",
            "create_subscription_plan",
            "change_subscription_plan",
        ]

    def test_confirm_plan_change_grants_capacity_and_activates(
        self, service, fake_payment_service, organization, billing_profile
    ):
        free_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 3}, monthly_price=Decimal("0")
        )
        pro_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 50}, monthly_price=Decimal("50")
        )
        baker.make(
            PlanEntitlement, plan=pro_plan, entitlement_key=Entitlement.PARTNER_API, is_enabled=True
        )
        subscription = _subscription_for(organization, free_plan)

        subscription = service.request_plan_change(
            subscription, pro_plan, BillingInterval.MONTHLY, payment_token="tok-1"
        )
        subscription = service.confirm_plan_change(subscription)

        limit = subscription.limits.get(resource_key=LimitedResource.ORGANIZATION_MEMBERS)
        assert limit.limit_value == 50
        entitlement = subscription.entitlements.get(entitlement_key=Entitlement.PARTNER_API)
        assert entitlement.is_enabled is True
        assert subscription.billing_state == BillingState.ACTIVE

    def test_confirm_plan_change_is_idempotent_across_repeated_calls(
        self, service, organization, billing_profile
    ):
        """A routine renewal charge re-runs this on every approved payment, not
        only the first one after an upgrade -- must not raise or duplicate rows
        on a second call."""
        free_plan = make_complete_plan({LimitedResource.ORGANIZATION_MEMBERS: 3})
        pro_plan = make_complete_plan({LimitedResource.ORGANIZATION_MEMBERS: 50})
        subscription = _subscription_for(organization, free_plan)
        subscription.plan = pro_plan
        subscription.save(update_fields=["plan"])

        service.confirm_plan_change(subscription)
        service.confirm_plan_change(subscription)

        assert (
            subscription.limits.filter(resource_key=LimitedResource.ORGANIZATION_MEMBERS).count()
            == 1
        )

    def test_upgrade_onto_an_incomplete_plan_is_refused(
        self, service, organization, billing_profile
    ):
        free_plan = make_complete_plan({LimitedResource.ORGANIZATION_MEMBERS: 3})
        incomplete_plan = baker.make(
            BillingPlan, is_default_for_new_organizations=False, monthly_price=Decimal("999")
        )
        subscription = _subscription_for(organization, free_plan)

        with pytest.raises(IncompleteBillingPlanError):
            service.request_plan_change(
                subscription, incomplete_plan, BillingInterval.MONTHLY, payment_token="tok-1"
            )

    def test_already_on_the_target_plan_is_a_no_op(
        self, service, fake_payment_service, organization, billing_profile
    ):
        plan = make_complete_plan({LimitedResource.ORGANIZATION_MEMBERS: 3})
        subscription = _subscription_for(organization, plan)

        result = service.request_plan_change(subscription, plan, BillingInterval.MONTHLY)

        assert result.pk == subscription.pk
        assert fake_payment_service.calls == []

    def test_upgrade_forwards_idempotency_key_to_the_provider(
        self, service, fake_payment_service, organization, billing_profile
    ):
        """BLOCKER 2 (provider-side guard): the client-supplied key must reach the
        provider drive, so a crash-after-charge / retry cannot re-drive it into a
        second subscription. Asserted at the seam regardless of transaction
        settings -- it does not depend on any inner atomic committing."""
        free_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 3}, monthly_price=Decimal("0")
        )
        pro_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 50}, monthly_price=Decimal("50")
        )
        subscription = _subscription_for(organization, free_plan)

        service.request_plan_change(
            subscription,
            pro_plan,
            BillingInterval.MONTHLY,
            payment_token="tok-1",
            idempotency_key="idem-upgrade-1",
        )

        # First-upgrade path drives `process_subscription`; the key rode along.
        assert "idem-upgrade-1" in fake_payment_service.idempotency_keys

    def test_retrying_the_same_upgrade_does_not_redrive_the_provider(
        self, service, fake_payment_service, organization, billing_profile
    ):
        """A second request for the plan already initiated (e.g. a double-click,
        or a client retry) is a no-op under the row-lock re-check -- it must not
        drive the provider a second time."""
        free_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 3}, monthly_price=Decimal("0")
        )
        pro_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 50}, monthly_price=Decimal("50")
        )
        subscription = _subscription_for(organization, free_plan)

        service.request_plan_change(
            subscription,
            pro_plan,
            BillingInterval.MONTHLY,
            payment_token="tok-1",
            idempotency_key="idem-upgrade-1",
        )
        calls_after_first = list(fake_payment_service.calls)

        service.request_plan_change(
            subscription,
            pro_plan,
            BillingInterval.MONTHLY,
            payment_token="tok-1",
            idempotency_key="idem-upgrade-1",
        )

        assert fake_payment_service.calls == calls_after_first

    def test_second_upgrade_to_a_different_plan_while_unconfirmed_is_rejected(
        self, service, fake_payment_service, organization, billing_profile
    ):
        """SHOULD-FIX 2: initiating a *different* upgrade before the first's charge
        confirms would make the first webhook grant the later tier's capacity --
        rejected until the in-flight change settles."""
        free_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 3}, monthly_price=Decimal("0")
        )
        pro_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 50}, monthly_price=Decimal("50")
        )
        premium_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 200}, monthly_price=Decimal("200")
        )
        subscription = _subscription_for(organization, free_plan)

        service.request_plan_change(
            subscription,
            pro_plan,
            BillingInterval.MONTHLY,
            payment_token="tok-1",
            idempotency_key="idem-upgrade-1",
        )
        calls_after_first = list(fake_payment_service.calls)

        with pytest.raises(UnconfirmedPlanChangeError):
            service.request_plan_change(
                subscription,
                premium_plan,
                BillingInterval.MONTHLY,
                idempotency_key="idem-upgrade-2",
            )

        # The rejected second attempt drove no provider call.
        assert fake_payment_service.calls == calls_after_first

    def test_a_new_upgrade_is_allowed_once_the_prior_one_confirms(
        self, service, fake_payment_service, organization, billing_profile
    ):
        """The unconfirmed-change guard clears on confirmation, so a genuinely
        sequential upgrade still works (regression guard for the reject above)."""
        free_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 3}, monthly_price=Decimal("0")
        )
        pro_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 50}, monthly_price=Decimal("50")
        )
        premium_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 200}, monthly_price=Decimal("200")
        )
        subscription = _subscription_for(organization, free_plan, external_id="already-on-file")

        result = service.request_plan_change(subscription, pro_plan, BillingInterval.MONTHLY)
        service.confirm_plan_change(result)
        # Does not raise now that the first change confirmed.
        service.request_plan_change(subscription, premium_plan, BillingInterval.MONTHLY)


@pytest.mark.django_db
class TestDowngrade:
    def test_downgrade_applies_lower_limits_immediately(
        self, service, fake_payment_service, organization, billing_profile
    ):
        pro_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 50}, monthly_price=Decimal("50")
        )
        free_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 3}, monthly_price=Decimal("0")
        )
        subscription = _subscription_for(organization, pro_plan, external_id="already-on-file")

        result = service.request_plan_change(subscription, free_plan, BillingInterval.MONTHLY)

        limit = result.limits.get(resource_key=LimitedResource.ORGANIZATION_MEMBERS)
        assert limit.limit_value == 3
        # No cash refund / no provider round trip for a downgrade.
        assert fake_payment_service.calls == []

    def test_downgrade_does_not_flip_plan_until_the_boundary(
        self, service, organization, billing_profile
    ):
        pro_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 50}, monthly_price=Decimal("50")
        )
        free_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 3}, monthly_price=Decimal("0")
        )
        subscription = _subscription_for(organization, pro_plan, external_id="already-on-file")

        result = service.request_plan_change(subscription, free_plan, BillingInterval.MONTHLY)

        assert result.plan_id == pro_plan.pk
        assert result.pending_plan_id == free_plan.pk
        assert result.pending_plan_effective_at == result.current_period_end

    def test_downgrade_stamps_a_grace_window(self, service, organization, billing_profile):
        pro_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 50}, monthly_price=Decimal("50")
        )
        free_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 3},
            monthly_price=Decimal("0"),
            grace_period_days=14,
        )
        subscription = _subscription_for(organization, pro_plan, external_id="already-on-file")
        before = timezone.now()

        result = service.request_plan_change(subscription, free_plan, BillingInterval.MONTHLY)

        assert result.grace_period_ends_at is not None
        assert result.grace_period_ends_at >= before + datetime.timedelta(days=14)

    def test_confirm_during_the_grace_window_keeps_the_lower_limits(
        self, service, organization, billing_profile
    ):
        """SHOULD-FIX 1: a subscription payment confirmed ``APPROVED`` while a
        scheduled downgrade is in its grace window must NOT restore the old
        higher plan's limits. `subscription.plan` is still the paid higher plan,
        but `confirm_plan_change` must sync from the pending (lower) plan."""
        pro_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 50}, monthly_price=Decimal("50")
        )
        free_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 3}, monthly_price=Decimal("0")
        )
        subscription = _subscription_for(organization, pro_plan, external_id="already-on-file")
        service.request_plan_change(subscription, free_plan, BillingInterval.MONTHLY)
        subscription.refresh_from_db()

        # A provider re-sends APPROVED (fresh event id) mid-downgrade.
        service.confirm_plan_change(subscription)

        limit = SubscriptionPlanLimit.objects.get(
            subscription=subscription, resource_key=LimitedResource.ORGANIZATION_MEMBERS
        )
        # Stays at the lower (downgrade-target) ceiling, not the paid plan's 50.
        assert limit.limit_value == 3
        # The plan itself is untouched until the Phase 13 boundary sweep.
        subscription.refresh_from_db()
        assert subscription.plan_id == pro_plan.pk
        assert subscription.pending_plan_id == free_plan.pk

    def test_downgrade_at_the_exact_ceiling_still_enforces_immediately(
        self, service, organization, billing_profile
    ):
        """Existing over-count resources are not evicted (`check_limit` never
        deletes), but a *new* create above the lower ceiling must be blocked
        right away -- proven directly against `SubscriptionPlanLimit`, the row
        `EntitlementService` reads."""
        pro_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 50}, monthly_price=Decimal("50")
        )
        free_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 1}, monthly_price=Decimal("0")
        )
        subscription = _subscription_for(organization, pro_plan, external_id="already-on-file")

        service.request_plan_change(subscription, free_plan, BillingInterval.MONTHLY)

        limit = SubscriptionPlanLimit.objects.get(
            subscription=subscription, resource_key=LimitedResource.ORGANIZATION_MEMBERS
        )
        assert limit.limit_value == 1


@pytest.mark.django_db
class TestDowngradeDrivesGraceForTheSweep:
    """Phase 11's fix for the Phase 10 dead-edge gap: before this,
    ``_schedule_downgrade`` stamped ``grace_period_ends_at`` but left
    ``billing_state`` untouched, so ``process_dunning``'s GRACE/RESTRICTED
    sweep never looked at the row and the deadline never expired. Now the
    downgrade drives ``billing_state`` into GRACE too, putting it on the one
    path the sweep already watches -- proven here at the driver; the sweep
    side (``DunningService._process_grace`` skipping the charge retry, and
    ``expire_grace`` resolving against the just-applied limits) is proven in
    ``test_dunning_service.py``.
    """

    def test_downgrade_from_active_moves_billing_state_to_grace(
        self, service, organization, billing_profile
    ):
        pro_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 50}, monthly_price=Decimal("50")
        )
        free_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 1}, monthly_price=Decimal("0")
        )
        subscription = _subscription_for(
            organization, pro_plan, external_id="already-on-file", billing_state=BillingState.ACTIVE
        )

        service.request_plan_change(subscription, free_plan, BillingInterval.MONTHLY)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.GRACE
        # The stamped deadline is now on a row process_dunning actually sweeps.
        assert subscription.grace_period_ends_at is not None

    def test_downgrade_while_restricted_does_not_crash_and_leaves_billing_state_unchanged(
        self, service, organization, billing_profile
    ):
        """RESTRICTED has no GRACE edge on the diagram
        (``LEGAL_BILLING_STATE_TRANSITIONS``) -- the downgrade itself (limits,
        ``pending_plan``, ``grace_period_ends_at``) must still apply; only the
        ``billing_state`` write is refused and swallowed."""
        pro_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 50}, monthly_price=Decimal("50")
        )
        free_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 1}, monthly_price=Decimal("0")
        )
        subscription = _subscription_for(
            organization,
            pro_plan,
            external_id="already-on-file",
            billing_state=BillingState.RESTRICTED,
        )

        result = service.request_plan_change(subscription, free_plan, BillingInterval.MONTHLY)

        result.refresh_from_db()
        assert result.billing_state == BillingState.RESTRICTED
        assert result.pending_plan_id == free_plan.pk
        assert result.grace_period_ends_at is not None
        limit = SubscriptionPlanLimit.objects.get(
            subscription=subscription, resource_key=LimitedResource.ORGANIZATION_MEMBERS
        )
        assert limit.limit_value == 1

    def test_downgrade_from_free_moves_billing_state_to_grace(
        self, service, organization, billing_profile
    ):
        """The ``(FREE, GRACE)`` edge, not only ``(ACTIVE, GRACE)`` -- a
        downgrade can be requested from FREE too (e.g. a reseller moving a
        pooled child's effective plan down)."""
        pro_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 50}, monthly_price=Decimal("50")
        )
        free_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 1}, monthly_price=Decimal("0")
        )
        subscription = _subscription_for(
            organization, pro_plan, external_id="already-on-file", billing_state=BillingState.FREE
        )

        service.request_plan_change(subscription, free_plan, BillingInterval.MONTHLY)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.GRACE

    def test_downgrading_twice_before_the_first_grace_window_resolves_is_idempotent(
        self, service, organization, billing_profile
    ):
        """A second downgrade requested while already GRACE from the first one
        must not raise (GRACE -> GRACE is the machine's same-state no-op)."""
        pro_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 50}, monthly_price=Decimal("50")
        )
        mid_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 10}, monthly_price=Decimal("10")
        )
        free_plan = make_complete_plan(
            {LimitedResource.ORGANIZATION_MEMBERS: 1}, monthly_price=Decimal("0")
        )
        subscription = _subscription_for(organization, pro_plan, external_id="already-on-file")

        service.request_plan_change(subscription, mid_plan, BillingInterval.MONTHLY)
        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.GRACE

        service.request_plan_change(subscription, free_plan, BillingInterval.MONTHLY)
        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.GRACE
        assert subscription.pending_plan_id == free_plan.pk


@pytest.mark.django_db
class TestCancelSubscription:
    def test_cancel_moves_to_cancelled_and_drives_the_provider_when_attached(
        self, service, fake_payment_service, organization, billing_profile
    ):
        plan = make_complete_plan()
        subscription = _subscription_for(
            organization, plan, external_id="already-on-file", billing_state=BillingState.ACTIVE
        )

        result = service.cancel_subscription(subscription)

        assert result.billing_state == BillingState.CANCELLED
        assert fake_payment_service.calls == ["cancel_subscription"]

    def test_cancel_skips_the_provider_when_never_attached(
        self, service, fake_payment_service, organization, billing_profile
    ):
        plan = make_complete_plan()
        subscription = _subscription_for(organization, plan, billing_state=BillingState.FREE)

        result = service.cancel_subscription(subscription)

        assert result.billing_state == BillingState.CANCELLED
        assert fake_payment_service.calls == []


@pytest.mark.django_db
class TestPurchaseAddOn:
    def test_purchase_creates_an_inactive_add_on_and_charges_once(
        self, service, fake_payment_service, organization, billing_profile
    ):
        plan = make_complete_plan(
            {LimitedResource.RESOURCE_CALENDARS: 3}, overage_unit_price=Decimal("2.5000")
        )
        subscription = _subscription_for(organization, plan)

        add_on = service.purchase_add_on(
            subscription,
            LimitedResource.RESOURCE_CALENDARS,
            quantity=2,
            is_recurring=False,
            idempotency_key="idem-1",
            payment_token="tok-1",
        )

        assert add_on.is_active is False
        assert add_on.payment is not None
        assert add_on.payment.value == Decimal("5.0000")
        assert fake_payment_service.calls == ["create_payment"]
        # Initiated-but-unconfirmed purchase grants no capacity.
        effective_limit = EntitlementService().get_effective_limit(
            organization, LimitedResource.RESOURCE_CALENDARS
        )
        assert effective_limit.limit_value == 3

    def test_confirming_the_payment_activates_and_lifts_the_effective_limit(
        self, service, organization, billing_profile
    ):
        plan = make_complete_plan(
            {LimitedResource.RESOURCE_CALENDARS: 3}, overage_unit_price=Decimal("2.5000")
        )
        subscription = _subscription_for(organization, plan)
        add_on = service.purchase_add_on(
            subscription,
            LimitedResource.RESOURCE_CALENDARS,
            quantity=2,
            is_recurring=False,
            idempotency_key="idem-1",
            payment_token="tok-1",
        )

        service.activate_add_on(add_on)

        effective_limit = EntitlementService().get_effective_limit(
            organization, LimitedResource.RESOURCE_CALENDARS
        )
        assert effective_limit.limit_value == 5

    def test_same_idempotency_key_twice_yields_one_add_on_and_one_charge(
        self, service, fake_payment_service, organization, billing_profile
    ):
        plan = make_complete_plan(
            {LimitedResource.RESOURCE_CALENDARS: 3}, overage_unit_price=Decimal("2.5000")
        )
        subscription = _subscription_for(organization, plan)

        first = service.purchase_add_on(
            subscription,
            LimitedResource.RESOURCE_CALENDARS,
            quantity=2,
            is_recurring=False,
            idempotency_key="idem-1",
            payment_token="tok-1",
        )
        second = service.purchase_add_on(
            subscription,
            LimitedResource.RESOURCE_CALENDARS,
            quantity=2,
            is_recurring=False,
            idempotency_key="idem-1",
            payment_token="tok-1",
        )

        assert first.pk == second.pk
        assert SubscriptionAddOn.objects.filter(purchase_idempotency_key="idem-1").count() == 1
        assert fake_payment_service.calls == ["create_payment"]

    def test_purchase_forwards_idempotency_key_to_the_provider(
        self, service, fake_payment_service, organization, billing_profile
    ):
        """BLOCKER 1 (provider-side guard): the client key must reach
        `create_payment` -> the provider, so a charge that succeeded before a
        rollback is not re-issued when the dedup row vanishes and the retry
        `get_or_create`s a fresh one. Asserted at the seam, independent of whether
        the local dedup row committed."""
        plan = make_complete_plan(
            {LimitedResource.RESOURCE_CALENDARS: 3}, overage_unit_price=Decimal("2.5000")
        )
        subscription = _subscription_for(organization, plan)

        service.purchase_add_on(
            subscription,
            LimitedResource.RESOURCE_CALENDARS,
            quantity=2,
            is_recurring=False,
            idempotency_key="idem-add-on-key",
            payment_token="tok-1",
        )

        assert fake_payment_service.idempotency_keys == ["idem-add-on-key"]

    def test_purchasing_a_resource_with_no_overage_price_is_refused(
        self, service, organization, billing_profile
    ):
        plan = make_complete_plan({LimitedResource.RESOURCE_CALENDARS: 3})
        subscription = _subscription_for(organization, plan)

        with pytest.raises(AddOnNotPurchasableError):
            service.purchase_add_on(
                subscription,
                LimitedResource.RESOURCE_CALENDARS,
                quantity=2,
                is_recurring=False,
                idempotency_key="idem-1",
                payment_token="tok-1",
            )

        assert not SubscriptionAddOn.objects.filter(purchase_idempotency_key="idem-1").exists()

    def test_cancel_add_on_stops_recurrence_without_dropping_current_capacity(
        self, service, organization, billing_profile
    ):
        plan = make_complete_plan(
            {LimitedResource.RESOURCE_CALENDARS: 3}, overage_unit_price=Decimal("2.5000")
        )
        subscription = _subscription_for(organization, plan)
        add_on = service.purchase_add_on(
            subscription,
            LimitedResource.RESOURCE_CALENDARS,
            quantity=2,
            is_recurring=True,
            idempotency_key="idem-1",
            payment_token="tok-1",
        )
        service.activate_add_on(add_on)

        result = service.cancel_add_on(add_on)

        assert result.is_recurring is False
        assert result.is_active is True


@pytest.mark.django_db
class TestRecordPaymentMethod:
    def test_records_a_new_payment_method(self, service, organization):
        payment_method = service.record_payment_method(
            organization, PaymentProviders.MERCADOPAGO, "card-123"
        )

        assert payment_method is not None
        assert payment_method.is_active is True
        assert PaymentMethod.objects.filter(
            organization=organization, provider=PaymentProviders.MERCADOPAGO, external_id="card-123"
        ).exists()

    def test_reactivates_a_previously_deactivated_row(self, service, organization):
        existing = baker.make(
            PaymentMethod,
            organization=organization,
            provider=PaymentProviders.MERCADOPAGO,
            external_id="card-123",
            is_active=False,
        )

        service.record_payment_method(organization, PaymentProviders.MERCADOPAGO, "card-123")

        existing.refresh_from_db()
        assert existing.is_active is True

    def test_blank_external_id_records_nothing(self, service, organization):
        result = service.record_payment_method(organization, PaymentProviders.MERCADOPAGO, "")

        assert result is None
        assert not PaymentMethod.objects.filter(organization=organization).exists()
