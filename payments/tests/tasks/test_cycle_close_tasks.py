"""The scheduled cycle-close sweep.

Two properties worth a test beyond the service's own unit tests:

- the fan-out selects only subscriptions whose period has **ended** (and only
  billing roots), and dispatches one task each — the beat task decides "who is due"
  once, so a redelivery of a per-subscription task does not re-scan;
- a per-subscription close that raises is **caught and logged**, not re-raised, so
  one failing subscription never aborts the sweep for the rest (the best-effort
  rule) and a poison task never spins.
"""

import datetime
from unittest.mock import patch

from django.utils import timezone

import pytest

from organizations.models import Organization
from payments.models import Subscription
from payments.tasks import close_billing_periods, close_subscription_billing_period


PERIOD_START = datetime.datetime(2025, 6, 1, 0, 0, tzinfo=datetime.UTC)


@pytest.fixture
def organization(db) -> Organization:
    return Organization.objects.create(name="Close Task Org", should_sync_rooms=False)


@pytest.fixture
def subscription(organization: Organization) -> Subscription:
    return Subscription.objects.get(organization=organization)


@pytest.mark.django_db
class TestCloseBillingPeriodsFanOut:
    def test_only_subscriptions_with_an_ended_period_are_dispatched(
        self, subscription: Subscription
    ):
        """One subscription is past its period end, another is not — only the ended
        one is dispatched."""
        # This subscription's period has ended.
        subscription.current_period_start = PERIOD_START
        subscription.current_period_end = PERIOD_START + datetime.timedelta(days=30)
        subscription.save(update_fields=["current_period_start", "current_period_end"])

        # A second subscription whose period is comfortably in the future.
        other_org = Organization.objects.create(name="Future Org", should_sync_rooms=False)
        other = Subscription.objects.get(organization=other_org)
        other.current_period_start = timezone.now()
        other.current_period_end = timezone.now() + datetime.timedelta(days=30)
        other.save(update_fields=["current_period_start", "current_period_end"])

        with patch("payments.tasks.close_subscription_billing_period.delay") as dispatched:
            close_billing_periods()

        dispatched_ids = {call.args[0] for call in dispatched.call_args_list}
        assert subscription.pk in dispatched_ids
        assert other.pk not in dispatched_ids


@pytest.mark.django_db
class TestCloseSubscriptionBillingPeriodTask:
    def test_a_deleted_subscription_is_skipped(self, subscription: Subscription):
        subscription_id = subscription.pk
        subscription.delete()

        # Must not raise.
        close_subscription_billing_period(subscription_id)

    def test_a_close_failure_is_caught_and_does_not_propagate(self, subscription: Subscription):
        """Best-effort: a failing close is logged, not re-raised, so the sweep of
        other subscriptions is unaffected."""
        with patch(
            "payments.services.cycle_close_service.CycleCloseService.close_subscription",
            side_effect=RuntimeError("provider exploded"),
        ):
            # Must not raise despite the underlying failure.
            close_subscription_billing_period(subscription.pk)
