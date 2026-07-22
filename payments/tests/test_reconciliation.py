"""Integration test: a full simulated cycle reconciles to zero drift.

Drives the *real* meter over a recurring series, then closes the cycle, and
asserts two things that must agree:

- ``reconcile_period`` reports **zero drift** — the metered set matches a fresh
  recomputation of the calendar, which is what catches silent revenue drift;
- the overage **charged** equals the overage the meter **stamped**, which equals
  what reconciliation audits the identity of — one derivation over
  ``MeteredOccurrence.for_billing_period``, never two.

The ``DedupingPaymentService`` is the "recorded provider fixture": it captures the
one charge the close issues so the test can compare it against the metered total.
"""

import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from calendar_integration.constants import CalendarProvider, RecurrenceFrequency
from calendar_integration.factories import CalendarEventFactory
from calendar_integration.models import Calendar, CalendarEvent
from organizations.models import Organization
from payments.billing_constants import LimitedResource
from payments.models import MeteredOccurrence, Subscription
from payments.services.cycle_close_service import CycleCloseService


PERIOD_START = datetime.datetime(2025, 6, 1, 0, 0, tzinfo=datetime.UTC)
PERIOD_END = datetime.datetime(2025, 7, 1, 0, 0, tzinfo=datetime.UTC)
FIRST_MONDAY = datetime.datetime(2025, 6, 2, 10, 0, tzinfo=datetime.UTC)
AFTER_PERIOD = datetime.datetime(2025, 7, 2, 0, 0, tzinfo=datetime.UTC)


class DedupingPaymentService:
    def __init__(self) -> None:
        self.charges: list[dict] = []

    def create_payment(self, *, idempotency_key: str = "", **kwargs) -> SimpleNamespace:
        self.charges.append({"idempotency_key": idempotency_key, **kwargs})
        return SimpleNamespace(pk=len(self.charges))


@pytest.fixture
def organization(db) -> Organization:
    return Organization.objects.create(name="Reconcile Org", should_sync_rooms=False)


@pytest.fixture
def subscription(organization: Organization) -> Subscription:
    subscription = Subscription.objects.get(organization=organization)
    subscription.current_period_start = PERIOD_START
    subscription.current_period_end = PERIOD_END
    subscription.save(update_fields=["current_period_start", "current_period_end", "modified"])
    return subscription


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Reconcile Calendar",
        description="",
        external_id="reconcile_cal_1",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )


@pytest.fixture
def weekly_event(calendar: Calendar) -> CalendarEvent:
    """A weekly Monday series — five occurrences in June 2025."""
    return CalendarEventFactory.create_recurring_event(
        calendar=calendar,
        title="Weekly Sync",
        description="",
        start_time=FIRST_MONDAY,
        end_time=FIRST_MONDAY + datetime.timedelta(hours=1),
        frequency=RecurrenceFrequency.WEEKLY,
        by_weekday="MO",
        external_id="weekly_master_reconcile",
    )


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


@pytest.fixture
def metering_service():
    from di_core.containers import container

    assert container is not None
    return container.metering_service()


@pytest.mark.django_db
def test_a_full_simulated_cycle_reconciles_to_zero_drift(
    cycle_close_service: CycleCloseService,
    metering_service,
    subscription: Subscription,
    weekly_event: CalendarEvent,
    payment_service: DedupingPaymentService,
):
    """Meter the whole cycle, then close it: the charge equals the metered overage
    and reconciliation reports zero drift."""
    # Allowance of 2 at 0.25 overage: 5 occurrences -> 2 included, 3 overage.
    subscription.limits.filter(resource_key=LimitedResource.EVENT_OCCURRENCES).update(
        limit_value=2, overage_unit_price=Decimal("0.2500")
    )

    metering_service.meter_occurrences_for_period(subscription, PERIOD_START, PERIOD_END)
    assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 5

    metered_overage = MeteredOccurrence.objects.for_billing_period(
        subscription.pk, PERIOD_START
    ).overage_total()

    closed = cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)

    assert len(closed) == 1
    report = closed[0].reconciliation
    assert report.is_clean, f"unexpected drift: {report.drift}"
    assert report.expected_count == report.metered_count == 5

    # One charge, and it equals the metered overage — one derivation, not two.
    assert metered_overage == Decimal("0.7500")
    assert closed[0].overage_total == metered_overage
    assert len(payment_service.charges) == 1
    assert payment_service.charges[0]["amount"] == metered_overage


@pytest.mark.django_db
def test_an_unlimited_cycle_reconciles_clean_and_charges_nothing(
    cycle_close_service: CycleCloseService,
    metering_service,
    subscription: Subscription,
    weekly_event: CalendarEvent,
    payment_service: DedupingPaymentService,
):
    """The rollout state: on the default ``unlimited`` plan the meter stamps every
    occurrence as within-allowance at zero, the close charges nothing, and
    reconciliation is clean."""
    metering_service.meter_occurrences_for_period(subscription, PERIOD_START, PERIOD_END)

    closed = cycle_close_service.close_subscription(subscription, now=AFTER_PERIOD)

    assert closed[0].reconciliation.is_clean
    assert closed[0].charged is False
    assert payment_service.charges == []
