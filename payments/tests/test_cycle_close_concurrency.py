"""Genuine concurrency around ``CycleCloseService.close_subscription``.

The idempotency tests (``test_cycle_close_idempotency.py``) are sequential — a
re-run and a crash-retry. Neither exercises the property the close docstring leans
on hardest: two sweeps of the *same* subscription running at the *same time*
serialise on ``SELECT ... FOR UPDATE`` so the period is charged **once**, without
relying on the provider's crash-safety dedup (a different failure mode). That is
the highest-severity failure cycle close can have — a double-charge under a race.

This runs against a **real** database with ``transaction=True`` and two OS threads
holding two separate connections, the pattern
``payments/tests/services/test_limit_concurrency.py`` established. The row lock is inside
``close_subscription``'s own ``transaction.atomic()``, so the two transactions must
actually be open at once for the block to be observable.

The test is self-validating: under a correct lock exactly one thread finds a period
to close (the other re-reads the already-rolled period and does nothing), so the
per-thread verdict is ``[0, 1]``. A broken lock would let both threads read the
un-rolled period and both close it — ``[1, 1]`` with two charge attempts — which the
assertion fails on loudly. A separate "without the lock" negative control would have
to monkeypatch ``select_for_update`` out of the service (fragile), and because the
overage key is derived from ``period_start`` the provider would still dedup a
lock-less double-attempt to one *distinct* key anyway — so the fact that matters is
"exactly one create_payment *call*", which the positive assertion proves directly.
"""

import datetime
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

from django.db import connection

import pytest
from dateutil.relativedelta import relativedelta
from model_bakery import baker

from organizations.models import Organization
from payments.billing_constants import BillingInterval, BillingState, LimitedResource, LimitKind
from payments.models import BillingPlan, MeteredOccurrence, Subscription, SubscriptionPlanLimit
from payments.services.cycle_close_service import CycleCloseService, overage_idempotency_key


# This module builds its own Subscription rows (OneToOne with Organization), so it
# opts out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription


PERIOD_START = datetime.datetime(2025, 6, 1, 0, 0, tzinfo=datetime.UTC)
PERIOD_END = datetime.datetime(2025, 7, 1, 0, 0, tzinfo=datetime.UTC)
AFTER_PERIOD = datetime.datetime(2025, 7, 2, 0, 0, tzinfo=datetime.UTC)

BARRIER_TIMEOUT_SECONDS = 10
THREAD_JOIN_TIMEOUT_SECONDS = 30
# The lock winner holds the subscription row for this window (it sleeps mid-charge),
# so the other thread is reliably blocked on `select_for_update` rather than racing
# past it — this is what makes the two transactions provably overlap.
RACE_WINDOW_SECONDS = 0.5


class DedupingPaymentService:
    """Models the provider: a repeated idempotency key resolves to the same charge.
    ``calls`` counts every ``create_payment`` invocation; ``settled_keys`` the
    *distinct* charges the provider actually took.

    ``create_payment`` sleeps ``race_window_seconds`` before recording, so the thread
    that wins the row lock holds it across the sleep — widening the window the losing
    thread must block on."""

    def __init__(self, race_window_seconds: float = 0.0) -> None:
        self._race_window = race_window_seconds
        self._lock = threading.Lock()
        self.calls: list[str] = []
        self._by_key: dict[str, SimpleNamespace] = {}

    def create_payment(self, *, idempotency_key: str = "", **kwargs) -> SimpleNamespace:
        if self._race_window:
            threading.Event().wait(self._race_window)
        with self._lock:
            self.calls.append(idempotency_key)
            if idempotency_key not in self._by_key:
                self._by_key[idempotency_key] = SimpleNamespace(pk=len(self._by_key) + 1)
            return self._by_key[idempotency_key]

    @property
    def settled_keys(self) -> set[str]:
        return set(self._by_key)


@pytest.fixture
def organization(db) -> Organization:
    return baker.make(
        Organization, parent=None, can_invite_organizations=False, should_sync_rooms=False
    )


@pytest.fixture
def subscription(organization: Organization) -> Subscription:
    """A billing-root subscription on a finite ``event_occurrences`` allowance, pinned
    to a known monthly cycle that has already ended (so a close has work to do)."""
    plan = baker.make(BillingPlan, is_default_for_new_organizations=False)
    sub = baker.make(
        Subscription,
        organization=organization,
        plan=plan,
        billing_state=BillingState.FREE,
        billing_interval=BillingInterval.MONTHLY,
        current_period_start=PERIOD_START,
        current_period_end=PERIOD_END,
    )
    baker.make(
        SubscriptionPlanLimit,
        subscription=sub,
        resource_key=LimitedResource.EVENT_OCCURRENCES,
        limit_value=0,
        kind=LimitKind.POSTPAID,
        overage_unit_price=Decimal("0.5000"),
    )
    return sub


def _seed_overage(subscription: Subscription, organization: Organization, count: int) -> None:
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


def _run_two_concurrent_closes(service: CycleCloseService, subscription: Subscription) -> list[int]:
    """Two threads call ``close_subscription`` for the same subscription at once.
    Returns each thread's count of periods closed, in thread order."""
    start_barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT_SECONDS)

    def close_once(_index: int) -> int:
        try:
            start_barrier.wait()
            closed = service.close_subscription(subscription, now=AFTER_PERIOD)
            return len(closed)
        finally:
            # Each thread owns its own connection; leaking it holds the row lock past
            # the test and wedges the next one.
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(close_once, index) for index in (0, 1)]
        return [future.result(timeout=THREAD_JOIN_TIMEOUT_SECONDS) for future in futures]


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_closes_charge_the_period_exactly_once(
    subscription: Subscription,
    organization: Organization,
):
    """The load-bearing concurrency claim: two sweeps racing on the same subscription
    serialise on the row lock, so the period is charged exactly once — one thread
    closes it, the other re-reads the rolled period and does nothing."""
    _seed_overage(subscription, organization, 2)  # 2 x 0.50 = 1.00

    from di_core.containers import container

    assert container is not None
    payment_service = DedupingPaymentService(race_window_seconds=RACE_WINDOW_SECONDS)
    service = CycleCloseService(
        metering_service=container.metering_service(),
        subscription_service=container.subscription_service(),
        payment_service=payment_service,  # type: ignore[arg-type]
        entitlement_service=container.entitlement_service(),
    )

    closed_counts = _run_two_concurrent_closes(service, subscription)

    # Exactly one thread found the period to close; the other serialised behind the
    # lock and re-read the already-rolled period. `[1, 1]` here would mean the lock
    # failed and both closed the same period.
    assert sorted(closed_counts) == [0, 1], f"expected one closer, got {closed_counts}"
    # The provider was asked to charge exactly once — not deduped-down from two
    # attempts, but a single call — with one distinct key.
    assert len(payment_service.calls) == 1
    assert payment_service.settled_keys == {overage_idempotency_key(subscription, PERIOD_START)}
    # The period rolled exactly one month, not two.
    subscription.refresh_from_db()
    assert subscription.current_period_start == PERIOD_END
    assert subscription.current_period_end == PERIOD_END + relativedelta(months=1)
