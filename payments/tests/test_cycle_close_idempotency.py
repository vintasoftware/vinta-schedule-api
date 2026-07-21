"""Integration test: closing the same period twice produces exactly one charge.

This is the single most important property in Phase 13 — a double-charge is the
high-severity, silent failure the spec rates worst. Two mechanisms make "exactly
once" hold, and each has a test:

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
from types import SimpleNamespace

import pytest
from dateutil.relativedelta import relativedelta

from organizations.models import Organization
from payments.billing_constants import LimitedResource
from payments.models import MeteredOccurrence, Subscription
from payments.services.cycle_close_service import CycleCloseService


PERIOD_START = datetime.datetime(2025, 6, 1, 0, 0, tzinfo=datetime.UTC)
PERIOD_END = datetime.datetime(2025, 7, 1, 0, 0, tzinfo=datetime.UTC)
AFTER_PERIOD = datetime.datetime(2025, 7, 2, 0, 0, tzinfo=datetime.UTC)


class DedupingPaymentService:
    """Models the provider: a repeated idempotency key resolves to the same charge,
    so ``settled_keys`` counts *distinct* charges the provider actually took."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._by_key: dict[str, SimpleNamespace] = {}

    def create_payment(self, *, idempotency_key: str = "", **kwargs) -> SimpleNamespace:
        self.calls.append(idempotency_key)
        if idempotency_key not in self._by_key:
            self._by_key[idempotency_key] = SimpleNamespace(pk=len(self._by_key) + 1)
        return self._by_key[idempotency_key]

    @property
    def settled_keys(self) -> set[str]:
        return set(self._by_key)


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
    subscription.limits.filter(resource_key=LimitedResource.EVENT_OCCURRENCES).update(
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
