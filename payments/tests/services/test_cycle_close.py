"""Unit tests for ``CycleCloseService`` — the settlement half of post-paid billing.

Money leaves the building here, so every assertion is about a number that ends up
on an invoice. The properties under test:

- only occurrences **outside** the allowance are charged, priced at the stamped
  ``unit_price`` (not ``count * current_price``);
- an **unlimited** allowance charges nothing (the inert-today real-money gate —
  the state every organization is in for the whole rollout);
- overage settles **monthly** even for an annually-billed plan;
- rolling the period forward resets the postpaid counter (period-scoped) and is
  the durable "already closed" marker.
"""

import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from dateutil.relativedelta import relativedelta
from model_bakery import baker

from organizations.models import Organization
from payments.billing_constants import BillingInterval, BillingState, LimitedResource, LimitKind
from payments.models import BillingPlan, MeteredOccurrence, PlanLimit, Subscription
from payments.services.cycle_close_service import CycleCloseService, overage_idempotency_key


PERIOD_START = datetime.datetime(2025, 6, 1, 0, 0, tzinfo=datetime.UTC)
PERIOD_END = datetime.datetime(2025, 7, 1, 0, 0, tzinfo=datetime.UTC)
AFTER_PERIOD = datetime.datetime(2025, 7, 2, 0, 0, tzinfo=datetime.UTC)


class FakePaymentService:
    """Records every ``create_payment`` call so a test can assert *what* was
    charged and *with which idempotency key* — the two things that decide whether
    a double-run double-charges."""

    def __init__(self) -> None:
        self.charges: list[dict] = []

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
    ) -> SimpleNamespace:
        self.charges.append(
            {
                "organization": organization,
                "currency": currency,
                "amount": amount,
                "description": description,
                "payment_method": payment_method,
                "idempotency_key": idempotency_key,
            }
        )
        return SimpleNamespace(pk=len(self.charges))


@pytest.fixture
def organization(db) -> Organization:
    return Organization.objects.create(name="Cycle Close Org", should_sync_rooms=False)


@pytest.fixture
def subscription(organization: Organization) -> Subscription:
    """The auto-provisioned subscription on the seeded ``unlimited`` plan, pinned to
    a known monthly cycle."""
    subscription = Subscription.objects.get(organization=organization)
    subscription.current_period_start = PERIOD_START
    subscription.current_period_end = PERIOD_END
    subscription.save(update_fields=["current_period_start", "current_period_end", "modified"])
    return subscription


@pytest.fixture
def fake_payment_service() -> FakePaymentService:
    return FakePaymentService()


@pytest.fixture
def cycle_close_service(fake_payment_service: FakePaymentService) -> CycleCloseService:
    from di_core.containers import container

    assert container is not None
    return CycleCloseService(
        metering_service=container.metering_service(),
        subscription_service=container.subscription_service(),
        payment_service=fake_payment_service,  # type: ignore[arg-type]
        entitlement_service=container.entitlement_service(),
    )


def _set_allowance(
    subscription: Subscription, limit_value: int | None, unit_price: str | None
) -> None:
    subscription.limits.filter(resource_key=LimitedResource.EVENT_OCCURRENCES).update(
        limit_value=limit_value,
        overage_unit_price=None if unit_price is None else Decimal(unit_price),
    )


def _make_complete_plan(slug: str) -> BillingPlan:
    """A catalog plan carrying a ``PlanLimit`` row for every ``LimitedResource`` —
    what ``assert_plan_is_complete`` (called by ``change_plan``) requires."""
    plan = baker.make(
        BillingPlan,
        slug=slug,
        is_default_for_new_organizations=False,
        monthly_price=Decimal("0"),
        annual_price=None,
        grace_period_days=None,
    )
    for resource_key in LimitedResource.values:
        baker.make(
            PlanLimit,
            plan=plan,
            resource_key=resource_key,
            limit_value=None,
            kind=(
                LimitKind.POSTPAID
                if resource_key == LimitedResource.EVENT_OCCURRENCES
                else LimitKind.PREPAID
            ),
            overage_unit_price=None,
        )
    return plan


def _meter_row(
    subscription: Subscription,
    organization: Organization,
    *,
    event_id: int,
    within: bool,
    price: str,
    period_start: datetime.datetime = PERIOD_START,
) -> MeteredOccurrence:
    return MeteredOccurrence.objects.create(
        organization=organization,
        subscription=subscription,
        event_id=event_id,
        occurrence_start=period_start + datetime.timedelta(days=event_id),
        billing_period_start=period_start,
        is_within_allowance=within,
        unit_price=Decimal(price),
    )


@pytest.mark.django_db
class TestOverageCharge:
    def test_only_occurrences_outside_the_allowance_are_charged(
        self,
        cycle_close_service: CycleCloseService,
        subscription: Subscription,
        organization: Organization,
        fake_payment_service: FakePaymentService,
    ):
        """Two included occurrences (priced 0) and three overage occurrences
        (priced 0.25 each): the charge is 0.75, the sum of the *stamped* overage
        prices, and the within-allowance rows contribute nothing."""
        _set_allowance(subscription, 2, "0.2500")
        _meter_row(subscription, organization, event_id=1, within=True, price="0.0000")
        _meter_row(subscription, organization, event_id=2, within=True, price="0.0000")
        _meter_row(subscription, organization, event_id=3, within=False, price="0.2500")
        _meter_row(subscription, organization, event_id=4, within=False, price="0.2500")
        _meter_row(subscription, organization, event_id=5, within=False, price="0.2500")

        closed = cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)

        assert len(closed) == 1
        assert closed[0].overage_total == Decimal("0.7500")
        assert closed[0].charged is True
        assert len(fake_payment_service.charges) == 1
        assert fake_payment_service.charges[0]["amount"] == Decimal("0.7500")

    def test_total_is_stamped_price_times_count_not_current_price(
        self,
        cycle_close_service: CycleCloseService,
        subscription: Subscription,
        organization: Organization,
        fake_payment_service: FakePaymentService,
    ):
        """The rows were stamped at 0.25; the plan's *current* price is later moved
        to 0.99. The charge follows the stamps, not the current price — a repricing
        must not change a closed period's bill."""
        _set_allowance(subscription, 0, "0.2500")
        _meter_row(subscription, organization, event_id=1, within=False, price="0.2500")
        _meter_row(subscription, organization, event_id=2, within=False, price="0.2500")
        _meter_row(subscription, organization, event_id=3, within=False, price="0.2500")
        _meter_row(subscription, organization, event_id=4, within=False, price="0.2500")

        _set_allowance(subscription, 0, "0.9900")  # current price changes after metering

        cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)

        assert fake_payment_service.charges[0]["amount"] == Decimal("1.0000")

    def test_unlimited_allowance_charges_nothing_but_still_rolls_and_reconciles(
        self,
        cycle_close_service: CycleCloseService,
        subscription: Subscription,
        organization: Organization,
        fake_payment_service: FakePaymentService,
    ):
        """The inert-today real-money gate: the default ``unlimited`` plan (NULL
        ``event_occurrences`` limit) charges nothing even with metered rows present,
        but the period is still rolled forward and reconciliation is still run."""
        # Rows exist but the allowance is NULL (unlimited) — the state every
        # organization is in for the whole rollout.
        _meter_row(subscription, organization, event_id=1, within=True, price="0.0000")
        _meter_row(subscription, organization, event_id=2, within=True, price="0.0000")

        closed = cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)

        assert fake_payment_service.charges == []
        assert len(closed) == 1
        assert closed[0].charged is False
        assert closed[0].overage_total == Decimal("0")
        # Period rolled forward one month.
        subscription.refresh_from_db()
        assert subscription.current_period_start == PERIOD_END
        assert subscription.current_period_end == PERIOD_END + relativedelta(months=1)
        # Reconciliation ran (identity report present).
        assert closed[0].reconciliation.billing_period_start == PERIOD_START

    def test_zero_overage_on_a_finite_plan_charges_nothing(
        self,
        cycle_close_service: CycleCloseService,
        subscription: Subscription,
        organization: Organization,
        fake_payment_service: FakePaymentService,
    ):
        """A finite allowance with every metered occurrence inside it owes nothing —
        no zero-amount charge is issued."""
        _set_allowance(subscription, 5, "0.2500")
        _meter_row(subscription, organization, event_id=1, within=True, price="0.0000")
        _meter_row(subscription, organization, event_id=2, within=True, price="0.0000")

        closed = cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)

        assert fake_payment_service.charges == []
        assert closed[0].charged is False


@pytest.mark.django_db
class TestMonthlySettlement:
    def test_annually_billed_plan_settles_overage_monthly(
        self,
        cycle_close_service: CycleCloseService,
        subscription: Subscription,
        organization: Organization,
        fake_payment_service: FakePaymentService,
    ):
        """An annual plan's stored period is one month long (created monthly), and
        cycle close rolls it forward one **month**, not one year — overage settles
        monthly regardless of ``billing_interval`` (spec §4.2)."""
        subscription.billing_interval = BillingInterval.ANNUAL
        subscription.save(update_fields=["billing_interval"])
        _set_allowance(subscription, 0, "0.1000")
        _meter_row(subscription, organization, event_id=1, within=False, price="0.1000")

        cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)

        subscription.refresh_from_db()
        # +1 month, NOT +1 year.
        assert subscription.current_period_start == PERIOD_END
        assert subscription.current_period_end == PERIOD_END + relativedelta(months=1)
        assert fake_payment_service.charges[0]["amount"] == Decimal("0.1000")


@pytest.mark.django_db
class TestRollAndCatchUp:
    def test_a_not_yet_ended_period_is_a_no_op(
        self,
        cycle_close_service: CycleCloseService,
        subscription: Subscription,
        fake_payment_service: FakePaymentService,
    ):
        """Closing before the period has ended does nothing — the guard is
        ``current_period_end <= now``."""
        before = datetime.datetime(2025, 6, 15, 0, 0, tzinfo=datetime.UTC)

        closed = cycle_close_service.close_subscription(subscription, now=before)

        assert closed == []
        assert fake_payment_service.charges == []
        subscription.refresh_from_db()
        assert subscription.current_period_start == PERIOD_START

    def test_multiple_elapsed_periods_are_caught_up(
        self,
        cycle_close_service: CycleCloseService,
        subscription: Subscription,
        fake_payment_service: FakePaymentService,
    ):
        """Three months elapsed with no sweep: one close call settles all three,
        rolling the period to the current month."""
        three_months_later = PERIOD_START + relativedelta(months=3, days=1)

        closed = cycle_close_service.close_subscription(subscription, now=three_months_later)

        assert len(closed) == 3
        subscription.refresh_from_db()
        assert subscription.current_period_start == PERIOD_START + relativedelta(months=3)


@pytest.mark.django_db
class TestDeferredBoundaryActions:
    def test_cancelled_subscription_reverts_to_free_at_period_close(
        self,
        cycle_close_service: CycleCloseService,
        subscription: Subscription,
    ):
        """A CANCELLED subscription runs to the end of its paid cycle, then the
        period-close sweep moves it to FREE."""
        subscription.billing_state = BillingState.CANCELLED
        subscription.save(update_fields=["billing_state"])

        cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.FREE

    def test_cancelled_subscription_before_period_end_stays_cancelled(
        self,
        cycle_close_service: CycleCloseService,
        subscription: Subscription,
    ):
        """No period boundary reached yet — the cancel has not taken effect."""
        subscription.billing_state = BillingState.CANCELLED
        subscription.save(update_fields=["billing_state"])
        before = datetime.datetime(2025, 6, 15, 0, 0, tzinfo=datetime.UTC)

        cycle_close_service.close_subscription(subscription, now=before)

        subscription.refresh_from_db()
        assert subscription.billing_state == BillingState.CANCELLED

    def test_pending_downgrade_is_applied_when_its_effective_moment_has_passed(
        self,
        cycle_close_service: CycleCloseService,
        subscription: Subscription,
    ):
        """A scheduled downgrade whose ``pending_plan_effective_at`` (the old period
        end) has passed is flipped at close: ``plan`` moves to the pending plan and
        the pending markers clear (Phase 9's deferred flip)."""
        pending_plan = _make_complete_plan("downgrade-target")
        original_plan_id = subscription.plan_id
        subscription.pending_plan = pending_plan
        subscription.pending_billing_interval = BillingInterval.MONTHLY
        subscription.pending_plan_effective_at = PERIOD_END
        subscription.save(
            update_fields=[
                "pending_plan",
                "pending_billing_interval",
                "pending_plan_effective_at",
            ]
        )
        assert original_plan_id != pending_plan.pk

        cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)

        subscription.refresh_from_db()
        assert subscription.plan_id == pending_plan.pk
        assert subscription.pending_plan_id is None
        assert subscription.pending_plan_effective_at is None
        assert subscription.pending_billing_interval == ""

    def test_a_future_pending_downgrade_is_not_applied_early(
        self,
        cycle_close_service: CycleCloseService,
        subscription: Subscription,
    ):
        """A downgrade whose effective moment is still in the future is left
        pending even though a period closed."""
        pending_plan = _make_complete_plan("downgrade-future")
        original_plan_id = subscription.plan_id
        subscription.pending_plan = pending_plan
        subscription.pending_billing_interval = BillingInterval.MONTHLY
        subscription.pending_plan_effective_at = AFTER_PERIOD + relativedelta(months=6)
        subscription.save(
            update_fields=[
                "pending_plan",
                "pending_billing_interval",
                "pending_plan_effective_at",
            ]
        )

        cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)

        subscription.refresh_from_db()
        assert subscription.plan_id == original_plan_id
        assert subscription.pending_plan_id == pending_plan.pk


def test_overage_idempotency_key_is_stable_for_a_period():
    """Derived only from ``(subscription.pk, period_start)`` so two attempts to
    close the same period produce the same key (provider-side dedup)."""
    sub = SimpleNamespace(pk=42)
    key_a = overage_idempotency_key(sub, PERIOD_START)  # type: ignore[arg-type]
    key_b = overage_idempotency_key(sub, PERIOD_START)  # type: ignore[arg-type]
    key_other_period = overage_idempotency_key(sub, PERIOD_END)  # type: ignore[arg-type]
    assert key_a == key_b
    assert key_a != key_other_period
    assert key_a == f"overage-42-{PERIOD_START.isoformat()}"
