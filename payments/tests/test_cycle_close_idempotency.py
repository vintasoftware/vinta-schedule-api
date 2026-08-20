"""Integration test: closing the same period twice produces exactly one charge.

This is the single most important property of cycle close — a double-charge is
the high-severity, silent failure. Two mechanisms make "exactly once" hold, and
each has a test:

- **The durable marker.** A completed close rolls ``current_period_start`` forward,
  so a re-run's ``current_period_end <= now`` guard finds nothing to close. A
  second sweep over an already-closed subscription is a no-op.
- **Provider-side idempotency.** A crash *between* the charge and the period-roll
  leaves the period unrolled, so a retry re-attempts the charge — but with a key
  derived from ``(subscription, period_start)``, so the provider dedups it. The
  local roll is not what makes this safe; the stable key is.
"""

import datetime
from decimal import Decimal

import pytest
from dateutil.relativedelta import relativedelta
from model_bakery import baker
from vinta_billing.models import BillingPeriodSummary, MeteredOccurrence, Payment, Subscription
from vinta_billing.services.cycle_close_service import CycleCloseService

from organizations.models import Organization
from payments.seams.resource_keys import EVENT_OCCURRENCES


PERIOD_START = datetime.datetime(2025, 6, 1, 0, 0, tzinfo=datetime.UTC)
PERIOD_END = datetime.datetime(2025, 7, 1, 0, 0, tzinfo=datetime.UTC)
AFTER_PERIOD = datetime.datetime(2025, 7, 2, 0, 0, tzinfo=datetime.UTC)


class DedupingPaymentService:
    """Models the provider: a repeated idempotency key resolves to the same charge
    *at the provider* -- ``settled_keys`` counts those distinct provider-side
    charges. The **local** ``Payment`` row is a different story: exactly like the
    real ``PaymentService.create_payment`` (see its docstring), a fresh row is
    created on every call regardless of the key, because the local row cannot
    itself dedupe across a rolled-back transaction -- only the provider can.
    ``BillingPeriodSummary.payment_id`` is a genuine foreign key, so returning
    a stable Python stand-in across calls (as an earlier version of this fake did)
    would link a statement to a ``Payment`` row a rolled-back attempt never
    actually persisted.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._distinct_keys: set[str] = set()

    def create_payment(self, *, idempotency_key: str = "", **kwargs) -> Payment:
        self.calls.append(idempotency_key)
        self._distinct_keys.add(idempotency_key)
        return baker.make(Payment, external_id=idempotency_key)

    @property
    def settled_keys(self) -> set[str]:
        return set(self._distinct_keys)


@pytest.fixture
def organization(db) -> Organization:
    return Organization.objects.create(name="Idempotency Org", should_sync_rooms=False)


@pytest.fixture
def subscription(organization: Organization) -> Subscription:
    subscription = Subscription.objects.get(organization=organization)
    subscription.current_period_start = PERIOD_START
    subscription.current_period_end = PERIOD_END
    subscription.save(update_fields=["current_period_start", "current_period_end", "modified"])
    # A finite allowance so the real-money path is exercised (the default unlimited
    # plan would charge nothing and this test would prove nothing).
    subscription.limits.filter(resource_key=EVENT_OCCURRENCES).update(
        limit_value=0, overage_unit_price=Decimal("0.5000")
    )
    return subscription


@pytest.fixture
def payment_service() -> DedupingPaymentService:
    return DedupingPaymentService()


@pytest.fixture
def cycle_close_service(payment_service: DedupingPaymentService) -> CycleCloseService:
    from di_core.containers import container

    assert container is not None
    return CycleCloseService(
        metering_service=container.metering_service(),
        subscription_service=container.subscription_service(),
        payment_service=payment_service,  # type: ignore[arg-type]
        entitlement_service=container.entitlement_service(),
    )


def _overage_rows(subscription: Subscription, organization: Organization, count: int) -> None:
    MeteredOccurrence.objects.bulk_create(
        MeteredOccurrence(
            organization=organization,
            subscription=subscription,
            event_id=i,
            occurrence_start=PERIOD_START + datetime.timedelta(days=i),
            billing_period_start=PERIOD_START,
            is_within_allowance=False,
            unit_price=Decimal("0.5000"),
        )
        for i in range(1, count + 1)
    )


@pytest.mark.django_db
def test_running_close_twice_produces_one_charge(
    cycle_close_service: CycleCloseService,
    subscription: Subscription,
    organization: Organization,
    payment_service: DedupingPaymentService,
):
    """The durable-marker path: the first close charges and rolls; the second finds
    the period already rolled and does nothing."""
    _overage_rows(subscription, organization, 3)  # 3 x 0.50 = 1.50

    first = cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)
    subscription.refresh_from_db()
    second = cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)

    assert len(first) == 1
    assert first[0].overage_total == Decimal("1.5000")
    assert second == []  # no-op: period already rolled
    assert payment_service.settled_keys == {f"overage-{subscription.pk}-{PERIOD_START.isoformat()}"}
    assert len(payment_service.settled_keys) == 1


@pytest.mark.django_db
def test_a_crash_between_charge_and_roll_does_not_double_charge(
    cycle_close_service: CycleCloseService,
    subscription: Subscription,
    organization: Organization,
    payment_service: DedupingPaymentService,
    monkeypatch,
):
    """The provider-idempotency path: the roll is forced to fail after the charge,
    rolling back the period. The retry re-charges — but with the *same* key, so the
    provider settles exactly one charge."""
    _overage_rows(subscription, organization, 2)  # 2 x 0.50 = 1.00

    original_roll = CycleCloseService._roll_period
    calls = {"n": 0}

    def failing_roll(sub, period_end):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash after charge, before roll")
        return original_roll(sub, period_end)

    monkeypatch.setattr(CycleCloseService, "_roll_period", staticmethod(failing_roll))

    with pytest.raises(RuntimeError):
        cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)

    # The period was NOT rolled (transaction rolled back with the crash).
    subscription.refresh_from_db()
    assert subscription.current_period_start == PERIOD_START

    # Retry: charges again with the same key, then rolls.
    cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)
    subscription.refresh_from_db()
    assert subscription.current_period_start == PERIOD_END

    # Two create_payment *calls* (crash + retry), but one *distinct* key — the
    # provider takes exactly one charge.
    assert len(payment_service.calls) == 2
    assert len(payment_service.settled_keys) == 1


@pytest.mark.django_db
def test_period_roll_and_counter_reset_are_idempotent(
    cycle_close_service: CycleCloseService,
    subscription: Subscription,
    organization: Organization,
    payment_service: DedupingPaymentService,
):
    """A second close pass after a completed close is a no-op: no second roll, no
    second charge. The rolled ``current_period_start`` is the durable marker."""
    _overage_rows(subscription, organization, 1)

    cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)
    subscription.refresh_from_db()
    rolled_start = subscription.current_period_start
    rolled_end = subscription.current_period_end

    cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)
    subscription.refresh_from_db()

    assert subscription.current_period_start == rolled_start == PERIOD_END
    assert subscription.current_period_end == rolled_end == PERIOD_END + relativedelta(months=1)
    assert len(payment_service.settled_keys) == 1


@pytest.mark.django_db
def test_rerunning_close_writes_no_second_statement(
    cycle_close_service: CycleCloseService,
    subscription: Subscription,
    organization: Organization,
    payment_service: DedupingPaymentService,
):
    """The ``BillingPeriodSummary`` statement is exactly as idempotent as
    the charge it is written alongside. A second ``close_subscription`` pass over
    an already-closed period is a no-op at the sweep-guard level (the rolled
    period is never re-entered), so it writes no second statement and raises
    nothing -- proving the same "exactly once" property this file's other tests
    prove for the charge also holds for the durable record of it."""
    _overage_rows(subscription, organization, 3)  # 3 x 0.50 = 1.50

    cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)
    assert BillingPeriodSummary.objects.count() == 1
    first_summary = BillingPeriodSummary.objects.get()

    cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)

    assert BillingPeriodSummary.objects.count() == 1
    second_summary = BillingPeriodSummary.objects.get()
    assert second_summary.pk == first_summary.pk
    assert second_summary.overage_total == Decimal("1.5000")


@pytest.mark.django_db
def test_calling_persist_statement_twice_repairs_missing_resource_rows(
    cycle_close_service: CycleCloseService,
    subscription: Subscription,
    organization: Organization,
    payment_service: DedupingPaymentService,
):
    """T4: ``close_subscription``'s sweep guard means ``_persist_statement`` is
    never actually re-entered for an already-closed period through the public
    entry point, so ``get_or_create``'s ``created is False`` branch has zero
    coverage there. Call ``_persist_statement`` directly a second time with the
    same ``period_start`` instead: the summary must not duplicate, and
    (SHOULD-FIX 2) resource rows missing because of a prior crash between the
    two writes must be repaired, not left as a statement with an empty
    ``resources`` list."""
    _overage_rows(subscription, organization, 3)  # 3 x 0.50 = 1.50
    period_start = subscription.current_period_start
    period_end = subscription.current_period_end
    report = cycle_close_service._metering_service.reconcile_period(subscription, period_start)

    cycle_close_service._persist_statement(
        subscription, period_start, period_end, Decimal("1.5000"), None, report
    )
    assert BillingPeriodSummary.objects.count() == 1
    summary = BillingPeriodSummary.objects.get()
    assert summary.resources.count() == 8

    # Simulate a crash between the summary write and its resource children: a
    # summary that already exists with zero resource rows must be repaired on
    # the next call, not treated as a completed no-op.
    summary.resources.all().delete()
    assert summary.resources.count() == 0

    cycle_close_service._persist_statement(
        subscription, period_start, period_end, Decimal("1.5000"), None, report
    )

    assert BillingPeriodSummary.objects.count() == 1  # still no duplicate summary
    summary.refresh_from_db()
    assert summary.resources.count() == 8  # ...but the missing children were repaired

    # A third call with the children already present is a pure no-op: the
    # unique (summary, resource_key) constraint absorbs the re-computed rows via
    # `ignore_conflicts=True` rather than raising.
    cycle_close_service._persist_statement(
        subscription, period_start, period_end, Decimal("1.5000"), None, report
    )
    assert BillingPeriodSummary.objects.count() == 1
    assert summary.resources.count() == 8
