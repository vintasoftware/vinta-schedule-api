"""Genuine concurrency around ``check_limit(lock=True)``.

These tests run against a **real** database with ``transaction=True`` and two OS
threads holding two separate connections. They are not a restatement of the unit
tests: the thing being proven can only fail when two transactions are open at the
same time.

The scenario is the one that actually loses money and breaks the invariant: an
organization with exactly one seat of headroom, and two requests racing to take
it. Without a row lock, both transactions read the same pre-insert count, both
conclude there is room, and the organization ends up over its ceiling with no
error raised anywhere. ``test_without_the_lock_the_race_overshoots`` deliberately
demonstrates that failure so the passing test above it cannot be mistaken for a
test that would pass regardless of the locking.

Both threads sleep between the check and the insert. That is what makes the race
deterministic rather than timing-dependent: the window a real request has between
"may I?" and "done" is simulated explicitly instead of hoped for.
"""

import datetime
import threading
from concurrent.futures import ThreadPoolExecutor

from django.db import connection, transaction
from django.utils import timezone

import pytest
from model_bakery import baker

from organizations.models import Organization, OrganizationMembership
from payments.billing_constants import BillingState, LimitedResource, LimitKind
from payments.models import BillingPlan, Subscription, SubscriptionPlanLimit
from payments.services.entitlement_service import EntitlementService
from users.models import User


# This module builds its own Subscription rows (OneToOne with Organization), so it
# opts out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription


BARRIER_TIMEOUT_SECONDS = 10
THREAD_JOIN_TIMEOUT_SECONDS = 30
# Long enough that a non-locking implementation reliably interleaves its read with
# the other thread's, short enough not to slow the suite noticeably.
RACE_WINDOW_SECONDS = 0.5


def _build_organization_at_one_seat_of_headroom(seat_limit: int = 3):
    """An organization whose seat pool has exactly one unit left."""
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
        resource_key=LimitedResource.ORGANIZATION_MEMBERS,
        limit_value=seat_limit,
        kind=LimitKind.PREPAID,
    )
    baker.make(
        OrganizationMembership,
        organization=organization,
        is_active=True,
        _quantity=seat_limit - 1,
    )
    return organization


def _run_two_racing_seat_claims(organization: Organization, lock: bool) -> list[bool]:
    """Two threads each try to claim the last seat. Returns each thread's
    ``allowed`` verdict, in thread order.

    Each thread mirrors what a guarded service method does under
    ``ATOMIC_REQUESTS``: open a transaction, ask ``check_limit``, and — if allowed
    — perform the create inside that same transaction. Doing the create in a
    *different* transaction than the check would make the lock pointless, which is
    exactly the mistake this harness has to avoid modelling.
    """
    service = EntitlementService()
    start_barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT_SECONDS)
    # Created on the main thread so the race window covers only the check + create,
    # not incidental user setup.
    users = [baker.make(User, email=f"racer-{index}@example.com") for index in (0, 1)]

    def claim_a_seat(index: int) -> bool:
        try:
            start_barrier.wait()
            with transaction.atomic():
                result = service.check_limit(
                    organization, LimitedResource.ORGANIZATION_MEMBERS, lock=lock
                )
                # Simulate the work a real caller does between the check and the
                # write. Under `lock=True` the other thread is blocked on the
                # subscription row for this whole window.
                threading.Event().wait(RACE_WINDOW_SECONDS)
                if result.allowed:
                    OrganizationMembership.objects.create(
                        organization=organization, user=users[index], is_active=True
                    )
                return result.allowed
        finally:
            # Each thread owns its own connection; leaking it holds the row lock
            # past the test and wedges the next one.
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim_a_seat, index) for index in (0, 1)]
        # `result(timeout=...)` re-raises whatever the thread raised, on this
        # thread, and turns a deadlock into a failure instead of a hung suite.
        return [future.result(timeout=THREAD_JOIN_TIMEOUT_SECONDS) for future in futures]


@pytest.mark.django_db(transaction=True)
def test_two_threads_racing_for_the_last_seat_serialize_and_only_one_wins():
    """The guarantee every pre-paid enforcement phase rests on."""
    organization = _build_organization_at_one_seat_of_headroom(seat_limit=3)

    verdicts = _run_two_racing_seat_claims(organization, lock=True)

    assert sorted(verdicts) == [False, True], (
        f"exactly one thread must see capacity, got {verdicts}"
    )
    assert (
        OrganizationMembership.objects.filter(organization=organization, is_active=True).count()
        == 3
    )


@pytest.mark.django_db(transaction=True)
def test_without_the_lock_the_race_overshoots():
    """Proof that the test above exercises real concurrency.

    With ``lock=False`` the identical harness lets both threads read the same
    pre-insert count and both create — the organization ends up with 4 seats
    against a ceiling of 3. If this test ever starts failing because both threads
    no longer overshoot, the harness has stopped racing and the test above has
    stopped proving anything.
    """
    organization = _build_organization_at_one_seat_of_headroom(seat_limit=3)

    verdicts = _run_two_racing_seat_claims(organization, lock=False)

    assert verdicts == [True, True]
    assert (
        OrganizationMembership.objects.filter(organization=organization, is_active=True).count()
        == 4
    )


@pytest.mark.django_db(transaction=True)
def test_lock_does_not_block_when_there_is_room_for_both():
    """Serializing must not turn into denying: with two seats free, both threads
    win. A lock that made the second caller lose regardless would pass the test
    above for the wrong reason."""
    organization = _build_organization_at_one_seat_of_headroom(seat_limit=4)
    # seat_limit 4 with 3 existing members leaves one seat; add one more of room.
    OrganizationMembership.objects.filter(organization=organization).order_by("pk").first().delete()

    verdicts = _run_two_racing_seat_claims(organization, lock=True)

    assert verdicts == [True, True]
    assert (
        OrganizationMembership.objects.filter(organization=organization, is_active=True).count()
        == 4
    )
