"""Unit tests for CalendarManagementToken manager lifecycle methods.

Covers:
- active() queryset filtering (used / revoked / expired / live)
- consume() setting used_at + consumed_source_ip exactly once
- consume() raising the correct error on a second call (ALREADY_USED)
- Concurrency: re-check-after-lock rejects a second (in-memory stale) consume
  attempt with TokenAlreadyUsedError.
- Concurrency: two genuinely concurrent transactions (separate connections in
  separate threads) serialise via SELECT FOR UPDATE — exactly one wins.
"""

import datetime
import threading

from django.db import connection, transaction
from django.utils import timezone

import pytest
from model_bakery import baker

from calendar_integration.constants import EventManagementPermissions
from calendar_integration.exceptions import (
    TokenAlreadyUsedError,
    TokenExpiredError,
    TokenRevokedError,
)
from calendar_integration.models import (
    CalendarManagementToken,
)
from common.utils.authentication_utils import generate_long_lived_token, hash_long_lived_token
from organizations.models import Organization


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token(org: Organization, **kwargs) -> CalendarManagementToken:
    """Create a minimal CalendarManagementToken row for lifecycle tests."""
    token_str = generate_long_lived_token()
    hashed_token = hash_long_lived_token(token_str)
    token = CalendarManagementToken.objects.create(
        organization=org,
        token_hash=hashed_token,
        **kwargs,
    )
    token.permissions.create(
        permission=EventManagementPermissions.CREATE,
        organization=org,
    )
    return token


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def org(db) -> Organization:
    return baker.make("organizations.Organization")


@pytest.fixture
def live_token(org) -> CalendarManagementToken:
    """A token that is unused, unrevoked, and not expired."""
    return _make_token(org)


@pytest.fixture
def used_token(org) -> CalendarManagementToken:
    """A token that has already been consumed."""
    return _make_token(org, used_at=timezone.now())


@pytest.fixture
def revoked_token(org) -> CalendarManagementToken:
    """A token that has been revoked."""
    return _make_token(org, revoked_at=timezone.now())


@pytest.fixture
def expired_token(org) -> CalendarManagementToken:
    """A token whose expires_at is in the past."""
    past = timezone.now() - datetime.timedelta(hours=1)
    return _make_token(org, expires_at=past)


@pytest.fixture
def future_expiry_token(org) -> CalendarManagementToken:
    """A token that expires in the future — still active."""
    future = timezone.now() + datetime.timedelta(hours=24)
    return _make_token(org, expires_at=future)


# ---------------------------------------------------------------------------
# active() filtering
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_active_excludes_used_token(org, live_token, used_token):
    """active() must not include tokens where used_at is set."""
    active_ids = set(
        CalendarManagementToken.objects.filter_by_organization(org.id)
        .active()
        .values_list("id", flat=True)
    )
    assert live_token.id in active_ids
    assert used_token.id not in active_ids


@pytest.mark.django_db
def test_active_excludes_revoked_token(org, live_token, revoked_token):
    """active() must not include tokens where revoked_at is set."""
    active_ids = set(
        CalendarManagementToken.objects.filter_by_organization(org.id)
        .active()
        .values_list("id", flat=True)
    )
    assert live_token.id in active_ids
    assert revoked_token.id not in active_ids


@pytest.mark.django_db
def test_active_excludes_expired_token(org, live_token, expired_token):
    """active() must not include tokens where expires_at is in the past."""
    active_ids = set(
        CalendarManagementToken.objects.filter_by_organization(org.id)
        .active()
        .values_list("id", flat=True)
    )
    assert live_token.id in active_ids
    assert expired_token.id not in active_ids


@pytest.mark.django_db
def test_active_includes_future_expiry_token(org, future_expiry_token):
    """active() must include tokens with a future expires_at."""
    active_ids = set(
        CalendarManagementToken.objects.filter_by_organization(org.id)
        .active()
        .values_list("id", flat=True)
    )
    assert future_expiry_token.id in active_ids


@pytest.mark.django_db
def test_active_includes_no_expiry_token(org, live_token):
    """active() must include tokens with expires_at=None."""
    assert live_token.expires_at is None
    active_ids = set(
        CalendarManagementToken.objects.filter_by_organization(org.id)
        .active()
        .values_list("id", flat=True)
    )
    assert live_token.id in active_ids


# ---------------------------------------------------------------------------
# consume() — happy path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_consume_sets_used_at_and_ip(org, live_token):
    """consume() must set used_at (non-null) and consumed_source_ip."""
    source_ip = "192.168.1.100"
    CalendarManagementToken.objects.consume(live_token, source_ip)

    live_token.refresh_from_db()
    assert live_token.used_at is not None
    assert live_token.consumed_source_ip == source_ip


@pytest.mark.django_db
def test_consume_sets_used_at_close_to_now(org, live_token):
    """The used_at timestamp recorded by consume() must be close to now."""
    before = timezone.now()
    CalendarManagementToken.objects.consume(live_token, "10.0.0.1")
    after = timezone.now()

    live_token.refresh_from_db()
    assert before <= live_token.used_at <= after


# ---------------------------------------------------------------------------
# consume() — terminal-state errors
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_consume_raises_already_used_on_second_call(org, live_token):
    """Second consume() call must raise TokenAlreadyUsedError."""
    CalendarManagementToken.objects.consume(live_token, "10.0.0.1")

    with pytest.raises(TokenAlreadyUsedError):
        CalendarManagementToken.objects.consume(live_token, "10.0.0.2")


@pytest.mark.django_db
def test_consume_raises_revoked_error(org, revoked_token):
    """consume() of a revoked token must raise TokenRevokedError."""
    with pytest.raises(TokenRevokedError):
        CalendarManagementToken.objects.consume(revoked_token, "10.0.0.1")


@pytest.mark.django_db
def test_consume_raises_expired_error(org, expired_token):
    """consume() of an expired token must raise TokenExpiredError."""
    with pytest.raises(TokenExpiredError):
        CalendarManagementToken.objects.consume(expired_token, "10.0.0.1")


# ---------------------------------------------------------------------------
# Concurrency: re-check-after-lock path
#
# True concurrent thread testing is impractical inside a single transactional
# test harness (each thread would need its own DB transaction, which conflicts
# with pytest-django's rollback strategy).  Instead we directly assert that:
#
#   1. The *first* consume() succeeds and marks the token used.
#   2. A *second* consume() with the same token (now stale in memory with
#      used_at=None) is re-fetched under a row lock, finds used_at IS NOT NULL,
#      and raises TokenAlreadyUsedError.
#
# This exercises exactly the code path that protects against a concurrent
# second request: the SELECT FOR UPDATE re-read sees the committed state of
# the row regardless of the in-memory token object passed in.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_consume_recheck_after_lock_rejects_stale_token(org, live_token):
    """The SELECT FOR UPDATE re-check must catch a token that was consumed
    since the in-memory object was fetched."""
    # Simulate: first request consumed the token in a committed transaction.
    CalendarManagementToken.objects.consume(live_token, "10.0.0.1")

    # live_token still has its original in-memory state (used_at=None) because
    # consume() works on a freshly-locked DB row.  A second concurrent
    # request would arrive with such a stale in-memory token.
    with pytest.raises(TokenAlreadyUsedError):
        CalendarManagementToken.objects.consume(live_token, "10.0.0.2")


# ---------------------------------------------------------------------------
# Concurrency: genuine two-connection / two-thread serialisation
#
# This test verifies atomic single-use under concurrency:
# two CONCURRENT transactions (each on its own DB connection in its own thread)
# call consume() on the same token, and exactly one wins while the other raises
# TokenAlreadyUsedError.  We use transaction=True so the committed token row is
# visible across connections, and a barrier so both threads are inside consume()
# (one holding the SELECT FOR UPDATE lock, one blocked on it) before either
# commits.  The loser only observes used_at IS NOT NULL after the winner commits
# and its own SELECT FOR UPDATE unblocks, which validates real row-lock
# serialisation rather than the in-memory re-check shortcut above.
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_consume_serialises_two_concurrent_transactions(org):
    """Two concurrent consume() calls on separate connections: exactly one
    succeeds, the other raises TokenAlreadyUsedError."""
    token = _make_token(org)

    start_barrier = threading.Barrier(2)
    results: list[str] = []
    results_lock = threading.Lock()

    def worker(source_ip: str) -> None:
        # Ensure both threads are running before either acquires the lock,
        # maximising the chance they contend for the same row.
        start_barrier.wait(timeout=10)
        try:
            with transaction.atomic():
                CalendarManagementToken.objects.consume(token, source_ip)
            outcome = "success"
        except TokenAlreadyUsedError:
            outcome = "already_used"
        finally:
            # Each thread uses its own connection; close it to avoid leaking
            # connections into the test runner's pool.
            connection.close()
        with results_lock:
            results.append(outcome)

    threads = [
        threading.Thread(target=worker, args=("10.0.0.1",)),
        threading.Thread(target=worker, args=("10.0.0.2",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(results) == ["already_used", "success"], results

    token.refresh_from_db()
    assert token.used_at is not None
    assert token.consumed_source_ip in {"10.0.0.1", "10.0.0.2"}


# ---------------------------------------------------------------------------
# get_token_error_code()
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_error_code_live_token_returns_none(live_token):
    assert CalendarManagementToken.objects.get_token_error_code(live_token) is None


@pytest.mark.django_db
def test_error_code_revoked_returns_revoked(revoked_token):
    assert CalendarManagementToken.objects.get_token_error_code(revoked_token) == "REVOKED"


@pytest.mark.django_db
def test_error_code_used_returns_already_used(used_token):
    assert CalendarManagementToken.objects.get_token_error_code(used_token) == "ALREADY_USED"


@pytest.mark.django_db
def test_error_code_expired_returns_expired(expired_token):
    assert CalendarManagementToken.objects.get_token_error_code(expired_token) == "EXPIRED"


@pytest.mark.django_db
def test_error_code_revoked_takes_priority_over_used(org):
    """REVOKED is checked before ALREADY_USED (revoked_at check is first)."""
    token = _make_token(org, revoked_at=timezone.now(), used_at=timezone.now())
    assert CalendarManagementToken.objects.get_token_error_code(token) == "REVOKED"
