"""Genuine concurrency around ``AvailabilityService.batch_modify_available_times``'s
delete-credit read.

Mirrors ``payments/tests/services/test_limit_concurrency.py``: a real database with
``transaction=True`` and two OS threads holding two separate connections, so the
race being proven can only fail when two transactions are genuinely open at once.

The scenario: an organization sitting exactly at its ``availability_windows``
ceiling, with one existing row two concurrent batches both try to replace via
``[{"action": "delete", "id": shared.id}, {"action": "create", ...}]`` -- ordinary
replace-semantics, which both the REST batch serializer and the GraphQL mutation
allow. Each batch computes ``credited_delete_count=1`` (the shared row is live) and
``delta = create_count - credited_delete_count = 0``, which is exactly the case
that skipped ``check_limit`` -- and, before the fix, skipped the guard lock too,
since the lock used to be taken only when ``delta`` was truthy. Without a lock
serializing the two reads, both transactions see the shared row as live, both
compute ``delta == 0``, both skip the guard, and both creates land: the org ends
up one *availability_windows* row over its ceiling with no error raised anywhere.

Both threads sleep inside the delete-credit read (patched in, not production code)
between reading the row's live/dead state and the batch's actual delete/create.
That is what makes the race deterministic rather than timing-dependent: the window
a real pair of concurrent requests has between "is the shared row still live?" and
"delete it and create the replacement" is simulated explicitly instead of hoped for.
"""

import datetime
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.db import connection
from django.utils import timezone

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarType
from calendar_integration.models import AvailableTime, Calendar
from calendar_integration.querysets import AvailableTimeQuerySet
from calendar_integration.services.calendar_service import CalendarService
from organizations.models import Organization
from payments.billing_constants import BillingState, LimitedResource, LimitKind
from payments.exceptions import OverLimitError
from payments.models import BillingPlan, Subscription, SubscriptionPlanLimit
from payments.services.entitlement_service import EntitlementService


BARRIER_TIMEOUT_SECONDS = 10
THREAD_JOIN_TIMEOUT_SECONDS = 30
# Long enough that a non-locking implementation reliably interleaves the two
# threads' delete-credit reads; short enough not to slow the suite noticeably.
RACE_WINDOW_SECONDS = 0.5


def _organization_at_the_ceiling_with_one_shared_row() -> tuple[
    Organization, Calendar, AvailableTime
]:
    """An organization at its exact ``availability_windows`` ceiling (3 of 3), with
    one existing row (``shared``) two concurrent batches will both try to replace."""
    organization = baker.make(Organization, parent=None, can_invite_organizations=False)
    now = timezone.now()
    subscription = baker.make(
        Subscription,
        organization=organization,
        plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
        billing_state=BillingState.FREE,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )
    baker.make(
        SubscriptionPlanLimit,
        subscription=subscription,
        resource_key=LimitedResource.AVAILABILITY_WINDOWS,
        limit_value=3,
        kind=LimitKind.PREPAID,
    )
    calendar = baker.make(
        Calendar,
        organization=organization,
        calendar_type=CalendarType.RESOURCE,
        manage_available_windows=True,
    )
    rows = [
        baker.make(AvailableTime, organization=organization, calendar=calendar, timezone="UTC")
        for _ in range(3)
    ]
    return organization, calendar, rows[0]


def _create_op(day: int) -> dict:
    return {
        "action": "create",
        "start_time": datetime.datetime(2026, 1, day, 9, 0, tzinfo=datetime.UTC),
        "end_time": datetime.datetime(2026, 1, day, 10, 0, tzinfo=datetime.UTC),
        "timezone": "UTC",
    }


def _run_two_racing_net_zero_batches(
    organization: Organization, calendar: Calendar, shared_id: int
) -> list[str]:
    """Two threads each try to replace the *same* row. Returns each thread's outcome
    (``"ok"``, ``"over_limit"``, or ``"value_error"`` -- the shared row already gone
    by the time this thread's own delete runs), in thread order.

    Mirrors what a real caller does: open a request, call
    ``batch_modify_available_times`` (which opens and holds its own transaction via
    ``@transaction.atomic()``), and let the guard lock -- if taken -- serialize the
    other thread on the subscription row for the sleep below.
    """
    start_barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT_SECONDS)

    original_count_counted_windows = AvailableTimeQuerySet.count_counted_windows_in_calendar

    def _slow_count_counted_windows(self, calendar_id, ids):
        result = original_count_counted_windows(self, calendar_id, ids)
        # Simulate the work a real caller does between reading whether the row it is
        # about to replace is still live and actually replacing it.
        time.sleep(RACE_WINDOW_SECONDS)
        return result

    def replace_shared_row(day: int) -> str:
        try:
            start_barrier.wait()
            service = CalendarService()
            service.initialize_without_provider(organization=organization)
            try:
                service.batch_modify_available_times(
                    calendar=calendar,
                    operations=[
                        {"action": "delete", "id": shared_id},
                        _create_op(day),
                    ],
                )
                return "ok"
            except OverLimitError:
                return "over_limit"
            except ValueError:
                return "value_error"
        finally:
            # Each thread owns its own connection; leaking it holds the row lock
            # past the test and wedges the next one.
            connection.close()

    with patch.object(
        AvailableTimeQuerySet,
        "count_counted_windows_in_calendar",
        _slow_count_counted_windows,
    ):
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(replace_shared_row, day) for day in (2, 3)]
            # `result(timeout=...)` re-raises whatever the thread raised, on this
            # thread, and turns a deadlock into a failure instead of a hung suite.
            return [future.result(timeout=THREAD_JOIN_TIMEOUT_SECONDS) for future in futures]


@pytest.mark.django_db(transaction=True)
def test_two_net_zero_batches_on_the_same_delete_serialize_and_do_not_overshoot():
    """The guarantee this SHOULD-FIX closes: a net-zero replace batch must not be
    the one case that skips the guard lock."""
    organization, calendar, shared = _organization_at_the_ceiling_with_one_shared_row()

    outcomes = _run_two_racing_net_zero_batches(organization, calendar, shared.id)

    # Exactly one thread's replacement lands. The loser's delete-credit read runs
    # only after the winner has committed (the lock serializes them), so it sees the
    # shared row already gone -- either its own delete raises (the row vanished
    # between the two reads a `ValueError` names) or, since the row is no longer
    # "credited", its delta becomes 1 against a ceiling with no room and
    # `check_limit` blocks it. Either way, nothing overshoots.
    assert outcomes.count("ok") == 1
    assert set(outcomes) <= {"ok", "over_limit", "value_error"}
    assert AvailableTime.objects.filter(organization=organization, calendar=calendar).count() == 3


@pytest.mark.django_db(transaction=True)
def test_without_the_lock_the_net_zero_race_overshoots():
    """Proof that the test above exercises real concurrency.

    With ``EntitlementService.lock_billing_root`` stubbed to a no-op -- restoring
    the pre-fix behavior of taking no lock at all whenever ``delta == 0`` -- both
    threads read the shared row as live, both compute ``delta == 0``, both skip
    ``check_limit`` entirely, and both creates land: the organization ends up with 4
    ``availability_windows`` rows against a ceiling of 3. If this test ever starts
    failing because both threads no longer overshoot, the harness has stopped racing
    and the test above has stopped proving anything.
    """
    organization, calendar, shared = _organization_at_the_ceiling_with_one_shared_row()

    with patch.object(EntitlementService, "lock_billing_root", lambda self, organization: None):
        outcomes = _run_two_racing_net_zero_batches(organization, calendar, shared.id)

    assert outcomes == ["ok", "ok"]
    assert AvailableTime.objects.filter(organization=organization, calendar=calendar).count() == 4
