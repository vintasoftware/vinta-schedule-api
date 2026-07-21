"""Tests for the ``reconcile_billing_period`` management command (finance audit)."""

import datetime
from decimal import Decimal
from io import StringIO

from django.core.management import CommandError, call_command

import pytest

from organizations.models import Organization
from payments.models import MeteredOccurrence, Subscription


PERIOD_START = datetime.datetime(2025, 6, 1, 0, 0, tzinfo=datetime.UTC)
PERIOD_END = datetime.datetime(2025, 7, 1, 0, 0, tzinfo=datetime.UTC)


@pytest.fixture
def organization(db) -> Organization:
    return Organization.objects.create(name="Finance Org", should_sync_rooms=False)


@pytest.fixture
def subscription(organization: Organization) -> Subscription:
    subscription = Subscription.objects.get(organization=organization)
    subscription.current_period_start = PERIOD_START
    subscription.current_period_end = PERIOD_END
    subscription.save(update_fields=["current_period_start", "current_period_end", "modified"])
    return subscription


def _run(subscription_id: int, period: str) -> str:
    out = StringIO()
    call_command(
        "reconcile_billing_period",
        f"--subscription-id={subscription_id}",
        f"--period={period}",
        stdout=out,
    )
    return out.getvalue()


@pytest.mark.django_db
def test_reports_a_clean_period_and_always_prints_the_blind_spot_note(
    subscription: Subscription,
):
    """A period with no events and no metered rows reconciles clean; the blind-spot
    caveat is printed regardless so a clean report is never read as invoice-correct."""
    output = _run(subscription.pk, "2025-06-15")

    assert "reconciled clean" in output
    assert "drift: 0" in output
    assert "audits occurrence *identity* only, not pricing" in output


@pytest.mark.django_db
def test_reports_drift_and_the_overage_total(
    subscription: Subscription,
    organization: Organization,
):
    """Orphaned metered rows (no matching calendar expansion) surface as drift, and
    the overage owed is summed from the stamped unit prices."""
    MeteredOccurrence.objects.create(
        organization=organization,
        subscription=subscription,
        event_id=1,
        occurrence_start=PERIOD_START + datetime.timedelta(days=1),
        billing_period_start=PERIOD_START,
        is_within_allowance=False,
        unit_price=Decimal("0.5000"),
    )

    output = _run(subscription.pk, "2025-06-15")

    assert "DRIFT DETECTED" in output
    assert "orphaned=1" in output
    assert "overage owed" in output
    assert "0.5000" in output


@pytest.mark.django_db
def test_unknown_subscription_raises(db):
    with pytest.raises(CommandError):
        _run(999999, "2025-06-15")


@pytest.mark.django_db
def test_unparseable_period_raises(subscription: Subscription):
    with pytest.raises(CommandError):
        _run(subscription.pk, "not-a-date")
