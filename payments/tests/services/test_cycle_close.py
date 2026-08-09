"""Unit tests for ``CycleCloseService`` — the settlement half of post-paid billing.

Money leaves the building here, so every assertion is about a number that ends up
on an invoice. The properties under test:

- only occurrences **outside** the allowance are charged, priced at the stamped
  ``unit_price`` (not ``count * current_price``);
- an **unlimited** allowance charges nothing (so no real money moves today —
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

from organizations.models import Organization, OrganizationMembership
from payments.billing_constants import BillingInterval, BillingState, LimitedResource, LimitKind
from payments.constants import PaymentProviders, PaymentStatuses
from payments.models import (
    BillingPeriodResourceUsage,
    BillingPeriodSummary,
    BillingPlan,
    BillingProfile,
    MeteredOccurrence,
    Payment,
    PlanLimit,
    Subscription,
)
from payments.services.cycle_close_service import CycleCloseService, overage_idempotency_key
from payments.services.entitlement_service import EntitlementService


PERIOD_START = datetime.datetime(2025, 6, 1, 0, 0, tzinfo=datetime.UTC)
PERIOD_END = datetime.datetime(2025, 7, 1, 0, 0, tzinfo=datetime.UTC)
AFTER_PERIOD = datetime.datetime(2025, 7, 2, 0, 0, tzinfo=datetime.UTC)


class FakePaymentService:
    """Records every ``create_payment`` call so a test can assert *what* was
    charged and *with which idempotency key* — the two things that decide whether
    a double-run double-charges.

    Returns a **real, persisted** ``Payment`` row (via ``baker.make``), not a bare
    stand-in: ``CycleCloseService._persist_statement`` (Phase 2) links the charge
    onto ``BillingPeriodSummary.payment``, a genuine foreign key Postgres
    enforces referential integrity on regardless of what Django's ORM validated in
    Python — a dangling id would raise ``IntegrityError`` at ``check_constraints``
    (deferred-constraint checking on transaction-wrapped test teardown), not at the
    point of assignment, which is a confusing place to first discover it.

    The ``Payment`` is built against ``organization``'s *own* ``BillingProfile``
    -- reusing it if one already exists, creating one for that exact organization
    otherwise -- rather than letting ``baker.make(Payment)`` auto-generate an
    unrelated ``BillingProfile`` (which would auto-create a *different*
    ``Organization``, and via conftest's autouse subscription-provisioning signal,
    a whole extra ``Subscription``). Without this, ``Payment.organization`` would
    resolve to a different tenant than the one the charge was actually issued
    against, and a statement's ``payment`` would silently point at another
    organization's billing profile.
    """

    def __init__(self) -> None:
        self.charges: list[dict] = []
        #: The real ``Payment`` row returned by each call, in call order -- so a
        #: test can assert a statement's ``payment_id`` against ``.payments[i].pk``
        #: rather than a hard-coded, sequence-dependent literal.
        self.payments: list[Payment] = []

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
        try:
            billing_profile = organization.billing_profile
        except BillingProfile.DoesNotExist:
            billing_profile = baker.make(BillingProfile, organization=organization)
        payment = baker.make(
            Payment,
            billing_profile=billing_profile,
            currency=currency,
            value=amount,
            payment_provider=PaymentProviders.MERCADOPAGO,
            status=PaymentStatuses.APPROVED,
            original_status=PaymentStatuses.APPROVED,
            payment_method=payment_method,
            description=description,
            external_id=idempotency_key,
        )
        self.payments.append(payment)
        return payment


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

    def test_catch_up_charges_each_elapsed_period_with_a_distinct_key(
        self,
        cycle_close_service: CycleCloseService,
        subscription: Subscription,
        organization: Organization,
        fake_payment_service: FakePaymentService,
    ):
        """The catch-up path must NOT collapse N elapsed periods into one charge, nor
        double-charge a period: it settles each period on its own
        ``(subscription, period_start)`` idempotency key, for that period's own
        stamped overage total.

        Three months elapsed with a *finite* allowance and overage rows stamped into
        each month at distinct totals (0.50 / 0.75 / 0.25). One close call must issue
        exactly three charges, with three distinct keys (one per ``period_start``),
        each equal to that period's stamped overage — proving the per-period-distinct-key
        property the ``unlimited`` catch-up test (which charges nothing) cannot."""
        period_one_start = PERIOD_START
        period_two_start = PERIOD_START + relativedelta(months=1)
        period_three_start = PERIOD_START + relativedelta(months=2)

        # A finite allowance so the real-money path is exercised. The charge amount is
        # the sum of the *stamped* overage rows, not the limit value.
        _set_allowance(subscription, 0, "0.2500")
        # Period 1 -> 0.50, period 2 -> 0.75, period 3 -> 0.25 (distinct per period).
        _meter_row(
            subscription,
            organization,
            event_id=1,
            within=False,
            price="0.2500",
            period_start=period_one_start,
        )
        _meter_row(
            subscription,
            organization,
            event_id=2,
            within=False,
            price="0.2500",
            period_start=period_one_start,
        )
        _meter_row(
            subscription,
            organization,
            event_id=3,
            within=False,
            price="0.2500",
            period_start=period_two_start,
        )
        _meter_row(
            subscription,
            organization,
            event_id=4,
            within=False,
            price="0.2500",
            period_start=period_two_start,
        )
        _meter_row(
            subscription,
            organization,
            event_id=5,
            within=False,
            price="0.2500",
            period_start=period_two_start,
        )
        _meter_row(
            subscription,
            organization,
            event_id=6,
            within=False,
            price="0.2500",
            period_start=period_three_start,
        )

        three_months_later = PERIOD_START + relativedelta(months=3, days=1)
        closed = cycle_close_service.close_subscription(subscription, now=three_months_later)

        assert len(closed) == 3
        # (a) exactly N charges — one per elapsed period, not one collapsed charge.
        assert len(fake_payment_service.charges) == 3
        # (b) N *distinct* idempotency keys, one per period_start.
        keys_to_amounts = {c["idempotency_key"]: c["amount"] for c in fake_payment_service.charges}
        assert len(keys_to_amounts) == 3
        # (c) each period's charge equals that period's stamped overage total.
        assert keys_to_amounts == {
            overage_idempotency_key(subscription, period_one_start): Decimal("0.5000"),
            overage_idempotency_key(subscription, period_two_start): Decimal("0.7500"),
            overage_idempotency_key(subscription, period_three_start): Decimal("0.2500"),
        }


@pytest.mark.django_db
class TestDeferredBoundaryActions:
    def test_cancelled_subscription_reverts_to_free_at_period_close(
        self,
        cycle_close_service: CycleCloseService,
        subscription: Subscription,
        organization: Organization,
        fake_payment_service: FakePaymentService,
    ):
        """A CANCELLED subscription runs to the end of its paid cycle, then the
        period-close sweep moves it to FREE — but its *final* period's overage must
        be charged before the flip, not dropped on the floor.

        Finite allowance + overage rows stamped into the closing period: the single
        ``close_subscription`` pass that flips the state to FREE must ALSO issue that
        period's one overage charge. Charge-before-roll ordering means the money is
        settled in the same pass the cancellation takes effect."""
        subscription.billing_state = BillingState.CANCELLED
        subscription.save(update_fields=["billing_state"])
        _set_allowance(subscription, 0, "0.2500")
        _meter_row(subscription, organization, event_id=1, within=False, price="0.2500")
        _meter_row(subscription, organization, event_id=2, within=False, price="0.2500")

        cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)

        # The final period's overage was charged (not dropped by the cancellation)...
        assert len(fake_payment_service.charges) == 1
        assert fake_payment_service.charges[0]["amount"] == Decimal("0.5000")
        assert fake_payment_service.charges[0]["idempotency_key"] == overage_idempotency_key(
            subscription, PERIOD_START
        )
        # ...and only then did the subscription flip to FREE.
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
        the pending markers clear. This is the deferred flip."""
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


@pytest.mark.django_db
class TestStatementPersistence:
    """Phase 2 of the billing usage summary & ledger plan: closing a period
    persists a durable ``BillingPeriodSummary`` (+ one ``BillingPeriodResourceUsage``
    per ``LimitedResource`` member) in the same transaction as the charge.
    """

    def test_closing_writes_one_summary_with_one_resource_row_per_limited_resource(
        self,
        cycle_close_service: CycleCloseService,
        subscription: Subscription,
        organization: Organization,
        fake_payment_service: FakePaymentService,
    ):
        _set_allowance(subscription, 2, "0.2500")
        _meter_row(subscription, organization, event_id=1, within=True, price="0.0000")
        _meter_row(subscription, organization, event_id=2, within=True, price="0.0000")
        _meter_row(subscription, organization, event_id=3, within=False, price="0.2500")

        closed = cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)

        assert BillingPeriodSummary.objects.count() == 1
        summary = BillingPeriodSummary.objects.get()
        assert summary.subscription_id == subscription.pk
        assert summary.organization_id == organization.pk
        assert summary.billing_period_start == PERIOD_START
        assert summary.billing_period_end == PERIOD_END
        assert summary.overage_total == closed[0].overage_total == Decimal("0.2500")
        assert summary.charged is True
        assert fake_payment_service.charges  # a charge was actually made
        # `payment_id` links the pk of the real `Payment` row the fake payment
        # service created for the one charge this test issued.
        assert summary.payment_id == fake_payment_service.payments[0].pk
        # T3: the linked payment must belong to the *same* organization the
        # statement is for, not a different tenant the payment fixture happened
        # to fabricate.
        assert summary.payment.organization.pk == summary.organization_id

        resources = list(summary.resources.all())
        assert len(resources) == len(LimitedResource.values)
        assert {row.resource_key for row in resources} == set(LimitedResource.values)
        event_occurrences_row = next(
            row for row in resources if row.resource_key == LimitedResource.EVENT_OCCURRENCES
        )
        assert event_occurrences_row.kind == LimitKind.POSTPAID
        assert event_occurrences_row.limit_value == 2
        assert event_occurrences_row.total == 3

    def test_payment_is_null_when_charged_is_false(
        self,
        cycle_close_service: CycleCloseService,
        subscription: Subscription,
        organization: Organization,
        fake_payment_service: FakePaymentService,
    ):
        """The default ``unlimited`` allowance charges nothing — the statement still
        gets written, with ``charged=False`` and no linked payment."""
        _meter_row(subscription, organization, event_id=1, within=True, price="0.0000")

        cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)

        assert fake_payment_service.charges == []
        summary = BillingPeriodSummary.objects.get()
        assert summary.charged is False
        assert summary.payment_id is None

    def test_catch_up_over_three_periods_writes_three_statements(
        self,
        cycle_close_service: CycleCloseService,
        subscription: Subscription,
        fake_payment_service: FakePaymentService,
    ):
        three_months_later = PERIOD_START + relativedelta(months=3, days=1)

        closed = cycle_close_service.close_subscription(subscription, now=three_months_later)

        assert len(closed) == 3
        assert BillingPeriodSummary.objects.count() == 3
        starts = set(BillingPeriodSummary.objects.values_list("billing_period_start", flat=True))
        assert starts == {
            PERIOD_START,
            PERIOD_START + relativedelta(months=1),
            PERIOD_START + relativedelta(months=2),
        }

    def test_pending_plan_change_effective_at_boundary_stamps_the_outgoing_plan(
        self,
        cycle_close_service: CycleCloseService,
        subscription: Subscription,
    ):
        """``_apply_pending_plan_change_if_due`` flips ``subscription.plan`` only
        after every period in the pass has already closed (see
        ``close_subscription``), so the statement for the period that boundary
        belongs to must carry the plan that was in force *during* it, not the one
        the subscription moves onto afterward."""
        outgoing_plan = subscription.plan
        pending_plan = _make_complete_plan("downgrade-target-statement")
        assert outgoing_plan.pk != pending_plan.pk
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

        cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)

        subscription.refresh_from_db()
        assert subscription.plan_id == pending_plan.pk  # the flip did happen

        summary = BillingPeriodSummary.objects.get()
        assert summary.plan_slug == outgoing_plan.slug
        assert summary.plan_name == outgoing_plan.name
        assert summary.plan_slug != pending_plan.slug

    def test_prepaid_by_organization_matches_get_usage_breakdown_for_pooled_subtree(
        self,
        cycle_close_service: CycleCloseService,
        subscription: Subscription,
        organization: Organization,
    ):
        """A reseller root with two children contributing seats: the persisted
        ``by_organization`` breakdown for a prepaid resource must equal what
        ``EntitlementService.get_usage_breakdown`` reports for the same pool."""
        organization.can_invite_organizations = True
        organization.save(update_fields=["can_invite_organizations"])
        child_a = Organization.objects.create(
            name="Statement Child A", parent=organization, should_sync_rooms=False
        )
        child_b = Organization.objects.create(
            name="Statement Child B", parent=organization, should_sync_rooms=False
        )
        baker.make(OrganizationMembership, organization=child_a, is_active=True, _quantity=2)
        baker.make(OrganizationMembership, organization=child_b, is_active=True, _quantity=1)

        entitlement_service = EntitlementService()
        expected_breakdown = entitlement_service.get_usage_breakdown(
            organization, LimitedResource.ORGANIZATION_MEMBERS
        )
        assert expected_breakdown  # sanity: both children contributed

        cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)

        summary = BillingPeriodSummary.objects.get()
        members_row = summary.resources.get(resource_key=LimitedResource.ORGANIZATION_MEMBERS)
        persisted_breakdown = {int(k): v for k, v in members_row.by_organization.items()}
        assert persisted_breakdown == expected_breakdown
        assert members_row.total == sum(expected_breakdown.values())

    def test_event_occurrences_total_matches_the_charge_when_pool_membership_changes_before_close(
        self,
        cycle_close_service: CycleCloseService,
        subscription: Subscription,
        organization: Organization,
    ):
        """Reviewer BLOCKER 1 scenario: rows are metered against the pool as it
        existed *at meter time*, but pool membership can change before close (a
        child is promoted to its own billing root). The statement's
        ``event_occurrences`` total must equal the full, unfiltered row set
        ``_charge_overage``/``reconcile_period`` read -- never a narrower,
        close-time-pooled subset -- or the statement contradicts the invoice."""
        organization.can_invite_organizations = True
        organization.save(update_fields=["can_invite_organizations"])
        child_a = Organization.objects.create(
            name="Pool Child A", parent=organization, should_sync_rooms=False
        )
        child_b = Organization.objects.create(
            name="Pool Child B", parent=organization, should_sync_rooms=False
        )
        _meter_row(subscription, child_a, event_id=1, within=True, price="0.0000")
        _meter_row(subscription, child_a, event_id=2, within=True, price="0.0000")
        _meter_row(subscription, child_b, event_id=3, within=True, price="0.0000")

        # Between meter time and close time, child_b is promoted to its own
        # billing root -- `_get_pooled_organization_ids(organization)` no longer
        # returns it, even though its rows were metered under this subscription.
        child_b.can_invite_organizations = True
        child_b.save(update_fields=["can_invite_organizations"])

        cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)

        charged_count = MeteredOccurrence.objects.for_billing_period(
            subscription.pk, PERIOD_START
        ).count()
        assert charged_count == 3  # the full, unfiltered row set `_charge_overage` reads

        summary = BillingPeriodSummary.objects.get()
        event_row = summary.resources.get(resource_key=LimitedResource.EVENT_OCCURRENCES)
        assert sum(event_row.by_organization.values()) == charged_count
        assert event_row.total == charged_count
        assert set(event_row.by_organization.keys()) == {str(child_a.pk), str(child_b.pk)}

    def test_persistence_failure_does_not_roll_back_the_charge_or_block_the_roll(
        self,
        cycle_close_service: CycleCloseService,
        subscription: Subscription,
        organization: Organization,
        fake_payment_service: FakePaymentService,
        monkeypatch,
    ):
        """A failure while persisting the statement is logged and swallowed — it
        must not undo the already-committed overage charge, nor prevent the period
        from rolling forward. Proven under **test** settings only: see the project's
        ``ATOMIC_REQUESTS`` trap note and ``_persist_statement``'s own docstring —
        this does not by itself prove the ordering holds under production settings.
        """
        _set_allowance(subscription, 0, "0.2500")
        _meter_row(subscription, organization, event_id=1, within=False, price="0.2500")

        def raising_usage_breakdown(self, root, resource_key, subscription, **kwargs):
            raise RuntimeError("simulated persistence failure")

        monkeypatch.setattr(EntitlementService, "_usage_breakdown", raising_usage_breakdown)

        closed = cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)

        # The charge went through and the caller sees a normal, successful close --
        # the persistence failure never propagates.
        assert len(closed) == 1
        assert closed[0].charged is True
        assert len(fake_payment_service.charges) == 1
        # No partial statement was left behind: the whole write is one savepoint.
        assert BillingPeriodSummary.objects.count() == 0
        assert BillingPeriodResourceUsage.objects.count() == 0
        # The period still rolled forward.
        subscription.refresh_from_db()
        assert subscription.current_period_start == PERIOD_END

    def test_persistence_failure_from_a_db_integrity_error_does_not_roll_back_the_charge(
        self,
        cycle_close_service: CycleCloseService,
        subscription: Subscription,
        organization: Organization,
        fake_payment_service: FakePaymentService,
        monkeypatch,
    ):
        """T2: the RuntimeError case above only proves Python-level unwinding out of
        the savepoint -- Django's ``Atomic.__exit__`` branches on ``exc_type is
        None`` / ``connection.needs_rollback``, not on the exception's class, so
        that code path is byte-identical for any Python exception, ``IntegrityError``
        included, as long as it is *raised in Python* without ever reaching
        Postgres. What a genuine database-level failure adds, and what this test
        exercises that the ``RuntimeError`` one cannot, is that Postgres itself
        leaves the connection in ``InFailedSqlTransaction`` once it rejects a
        statement, so ``SAVEPOINT ROLLBACK`` is genuinely required to make the
        connection usable again -- not just plausible in theory. The post-rollback
        ``subscription.refresh_from_db()`` below only succeeds if that recovery
        actually happened.

        Tripped via a real constraint: ``BillingPeriodResourceUsage.total`` is a
        ``PositiveIntegerField``, for which Django emits a database-level
        ``CHECK (total >= 0)`` on Postgres. One usage counter is monkeypatched to
        report a negative count for a single resource, so ``bulk_create`` runs for
        real (unmocked) and Postgres itself raises ``IntegrityError`` when it
        evaluates that constraint. A duplicate-key violation can no longer serve
        this purpose: ``bulk_create(..., ignore_conflicts=True)`` swallows those at
        the database level, leaving the CHECK constraint as the one left to trip.
        """
        _set_allowance(subscription, 0, "0.2500")
        _meter_row(subscription, organization, event_id=1, within=False, price="0.2500")

        def negative_total_usage_breakdown(self, root, resource_key, subscription, **kwargs):
            # Only one resource goes negative -- enough to trip the CHECK
            # constraint on `bulk_create` without contaminating every row.
            if resource_key == LimitedResource.ORGANIZATION_MEMBERS:
                return {organization.pk: -1}
            return {}

        monkeypatch.setattr(EntitlementService, "_usage_breakdown", negative_total_usage_breakdown)

        closed = cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)

        # The charge went through and the caller sees a normal, successful close --
        # the persistence failure never propagates.
        assert len(closed) == 1
        assert closed[0].charged is True
        assert len(fake_payment_service.charges) == 1
        # No partial statement was left behind: the whole write is one savepoint.
        assert BillingPeriodSummary.objects.count() == 0
        assert BillingPeriodResourceUsage.objects.count() == 0
        # The period still rolled forward.
        subscription.refresh_from_db()
        assert subscription.current_period_start == PERIOD_END


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
