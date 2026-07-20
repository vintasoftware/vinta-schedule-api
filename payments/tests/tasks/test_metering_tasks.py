"""The scheduled sweep that actually turns occurrences into billable rows.

Two things here are worth a test even though they look like plumbing, because both
have failed silently in this codebase before:

- ``@inject`` on a Celery task can be a **no-op** — dependency_injector cannot read
  ``Provide[...]`` out of a stringified annotation, and returns the function
  unpatched. The task then runs with ``None`` where a service should be. Nothing in
  a mocked test would notice; this drives the task for real and asserts rows appear.
- The sweep window is computed **once, by the beat task**, and passed to each
  per-subscription task explicitly. A task that recomputed it from ``now()`` would
  sweep a different stretch on every ``CELERY_TASK_ACKS_LATE`` redelivery, so a
  retry would not repeat the work it was retrying.
"""

import datetime
from unittest.mock import patch

from django.utils import timezone

import pytest

from calendar_integration.constants import CalendarProvider, RecurrenceFrequency
from calendar_integration.factories import CalendarEventFactory
from calendar_integration.models import Calendar
from organizations.models import Organization
from payments.models import MeteredOccurrence, Subscription
from payments.tasks import (
    METERING_SWEEP_WINDOW,
    meter_event_occurrences,
    meter_subscription_event_occurrences,
)


@pytest.fixture
def organization(db) -> Organization:
    return Organization.objects.create(name="Metering Task Org", should_sync_rooms=False)


@pytest.fixture
def subscription(organization: Organization) -> Subscription:
    return Subscription.objects.get(organization=organization)


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Metering Task Calendar",
        description="",
        external_id="metering_task_cal",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )


@pytest.mark.django_db
class TestMeterSubscriptionEventOccurrences:
    def test_the_task_meters_through_a_really_injected_service(
        self, subscription: Subscription, calendar: Calendar
    ):
        """No mocks: if ``@inject`` silently did nothing, this raises instead of passing."""
        occurred_at = timezone.now() - datetime.timedelta(hours=1)
        CalendarEventFactory.create_recurring_event(
            calendar=calendar,
            title="Recent",
            description="",
            start_time=occurred_at,
            end_time=occurred_at + datetime.timedelta(minutes=30),
            frequency=RecurrenceFrequency.WEEKLY,
            count=1,
            external_id="metering_task_event",
        )

        meter_subscription_event_occurrences(
            subscription.pk,
            (occurred_at - datetime.timedelta(hours=1)).isoformat(),
            (occurred_at + datetime.timedelta(hours=1)).isoformat(),
        )

        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 1

    def test_running_the_task_twice_records_one_row(
        self, subscription: Subscription, calendar: Calendar
    ):
        """``CELERY_TASK_ACKS_LATE`` means redelivery is expected, not exceptional."""
        occurred_at = timezone.now() - datetime.timedelta(hours=1)
        CalendarEventFactory.create_recurring_event(
            calendar=calendar,
            title="Recent",
            description="",
            start_time=occurred_at,
            end_time=occurred_at + datetime.timedelta(minutes=30),
            frequency=RecurrenceFrequency.WEEKLY,
            count=1,
            external_id="metering_task_event",
        )
        window = (
            (occurred_at - datetime.timedelta(hours=1)).isoformat(),
            (occurred_at + datetime.timedelta(hours=1)).isoformat(),
        )

        meter_subscription_event_occurrences(subscription.pk, *window)
        meter_subscription_event_occurrences(subscription.pk, *window)

        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 1

    def test_a_subscription_deleted_between_fan_out_and_execution_is_skipped(
        self, subscription: Subscription
    ):
        """A raising task would be redelivered and fail identically forever."""
        subscription_id = subscription.pk
        subscription.delete()

        meter_subscription_event_occurrences(
            subscription_id,
            (timezone.now() - datetime.timedelta(hours=1)).isoformat(),
            timezone.now().isoformat(),
        )

        assert MeteredOccurrence.objects.count() == 0


@pytest.mark.django_db
class TestMeterEventOccurrencesFanOut:
    def test_every_subscription_is_swept_over_one_shared_window(self, subscription: Subscription):
        Organization.objects.create(name="Second Org", should_sync_rooms=False)
        expected_ids = set(Subscription.objects.values_list("pk", flat=True))
        assert len(expected_ids) >= 2

        with patch("payments.tasks.meter_subscription_event_occurrences.delay") as dispatched:
            meter_event_occurrences()

        calls = dispatched.call_args_list
        assert {call.args[0] for call in calls} == expected_ids
        windows = {(call.args[1], call.args[2]) for call in calls}
        assert len(windows) == 1, "each run must sweep one window, not one per subscription"

    def test_the_window_is_wider_than_the_beat_interval(self, subscription: Subscription):
        """The overlap is the self-healing mechanism, so assert it exists.

        The beat entry runs every 15 minutes; a window narrower than that would
        leave stretches of time no run ever reads, and they would never be billed.
        """
        with patch("payments.tasks.meter_subscription_event_occurrences.delay") as dispatched:
            meter_event_occurrences()

        window_start, window_end = dispatched.call_args.args[1:3]
        span = datetime.datetime.fromisoformat(window_end) - datetime.datetime.fromisoformat(
            window_start
        )
        assert span == METERING_SWEEP_WINDOW
        assert span > datetime.timedelta(minutes=15)
