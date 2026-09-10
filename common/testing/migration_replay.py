"""Replaying migrations against the per-worker test database, safely.

A handful of tests drive ``MigrationExecutor`` over the database the whole
worker shares -- stepping the graph backwards to reach a state a live model can
no longer express, exercising a data migration there, then restoring every app
to its leaf node. ``organizations/tests/test_slug_backfill.py``,
``organizations/tests/test_membership_group_migration_executor.py`` and
``payments/tests/test_table_move_migration.py`` are the three.

They share one hazard, and it is not the migration: it is ``pytest.ini``'s
``timeout = 10`` hang guard, with ``timeout_method = signal``. Ten seconds is a
sensible ceiling for an ordinary test and far below what a replay costs -- the
Python-side project-state rendering alone walks a hundred migrations, and since
``payments/migrations/0024_move_billing_to_vinta_billing`` any reverse past
``organizations.0028`` also copies twenty tables in each direction. Under
``pytest -n auto`` on a loaded machine these routinely run for a minute.

When the alarm fires, it fires *wherever the test happens to be* -- which is
most often inside the ``finally`` block's restore, because that is where most
of the wall time goes. The test then fails, which is fine, and leaves the
worker's database stopped mid-graph, which is not: every test scheduled after
it on that worker hits a schema no live model matches, and one slow test is
reported as thirty broken ones with nothing pointing back at the cause. That is
what made this look like an xdist-specific flake.

Two things close it, and both are needed:

- :func:`migration_replay` marks the test as one of these, with a ceiling high
  enough that reaching it means something is genuinely wrong rather than merely
  slow.
- :func:`uninterruptible` wraps the restore so the alarm cannot land in the
  middle of it. A replay that overruns then fails on its own and leaves the
  database consistent for everything after it.

Autovacuum can deadlock a replay
--------------------------------
A replay runs on the test's one connection, so it cannot deadlock with itself.
Autovacuum is a second session in the same database, and the suite feeds it:
every ``transaction=True`` flush and every bulk insert pushes tables past the
autoanalyze threshold, so an ``autovacuum: ANALYZE`` of a table the replay is
about to alter is a matter of timing, not of anything the test did wrong.

Postgres 15.9 / 16.5 / 17.1 changed how the two collide. Those releases fixed
lost "in-place" catalog updates by making an in-place writer wait for any
in-progress transaction that has already updated the same ``pg_class`` row.
ANALYZE refreshes ``pg_class`` in place for the table and each of its indexes.
A migration that renames an index and later adds a constraint on the same
table, in one transaction, therefore produces this cycle: the rename updates
the index's ``pg_class`` row; ANALYZE, already holding ``SHARE UPDATE
EXCLUSIVE`` on the table, blocks on the migration's transaction; the ``ADD
CONSTRAINT`` then blocks on ANALYZE's table lock. Postgres picks a victim and
reports ``deadlock detected``. CI saw exactly this on shard 4 while
``calendar_integration.0062`` was being re-applied inside a replay's
``finally`` (the ``postgres:15`` service tag floats to the latest minor, which
is why it appeared without a change on our side).

The deadlock aborts only the current migration's transaction, and Django
records a migration only after it completes, so re-running the same ``migrate``
call resumes where it stopped. :func:`migrate_to` and :func:`restore_leaf_nodes`
retry a step on ``DeadlockDetected`` -- and on nothing else -- a few times.
The project's non-atomic migrations are written to be resumable (see the
resumability guards in ``calendar_integration`` 0054 and 0057), so a retry
after a partial non-atomic step is the recovery path they were designed for.
Every replay test should go through these two helpers rather than build its
own ``MigrationExecutor``.
"""

from __future__ import annotations

import signal
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.utils import OperationalError

import pytest
from psycopg.errors import DeadlockDetected


#: Not a performance budget -- a hang guard, like the global one. A replay that
#: takes ten minutes is stuck, not slow.
MIGRATION_REPLAY_TIMEOUT_SECONDS = 600

#: Marks a test that replays migrations against the shared per-worker database.
#: Apply to the class or the function; see the module docstring.
migration_replay = pytest.mark.timeout(MIGRATION_REPLAY_TIMEOUT_SECONDS)

#: How many times one replay step may hit ``deadlock detected`` before the
#: failure is reported. The aborted transaction unblocks the ANALYZE that was
#: waiting on it, so the second attempt almost always runs clear; three covers
#: a second autovacuum worker arriving in between.
DEADLOCK_ATTEMPTS = 3
DEADLOCK_RETRY_PAUSE_SECONDS = 0.5


@contextmanager
def uninterruptible() -> Iterator[None]:
    """Run a block that must finish, or the worker's database is left unusable.

    Cancels any pending ``SIGALRM`` for the duration and restores the remaining
    time afterwards, so pytest-timeout still fires -- just not *here*. Wrap the
    restore half of a migration replay in it, never the part under test: the
    point is to keep a timeout from turning one test's failure into every later
    test's failure, not to make a test unable to time out.

    A no-op where ``SIGALRM`` does not exist or no alarm is pending, so it costs
    nothing under ``timeout_method = thread`` or with the plugin disabled.
    """
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    # `alarm(0)` cancels the pending alarm and returns the seconds that were
    # left on it -- 0 when none was set.
    remaining = signal.alarm(0)
    try:
        yield
    finally:
        if remaining:
            signal.alarm(remaining)


def _is_deadlock(exc: OperationalError) -> bool:
    # Django re-raises psycopg's error as its own ``OperationalError`` with the
    # original as ``__cause__``; SQLSTATE 40P01 is ``DeadlockDetected`` there.
    return isinstance(exc.__cause__, DeadlockDetected)


def run_replay_step[T](step: Callable[[], T]) -> T:
    """Run one ``migrate`` call, retrying it if autovacuum deadlocks it.

    Only ``deadlock detected`` is retried -- with a single test connection the
    other party can only be autovacuum, and the failed step left nothing
    recorded. Any other error propagates on the first attempt. See the module
    docstring's "Autovacuum can deadlock a replay".
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return step()
        except OperationalError as exc:
            if attempt >= DEADLOCK_ATTEMPTS or not _is_deadlock(exc):
                raise
            time.sleep(DEADLOCK_RETRY_PAUSE_SECONDS)


def migrate_to(*targets: tuple[str, str]) -> MigrationExecutor:
    """Step the worker's database to ``targets`` -- ``(app_label, migration_name)``
    pairs -- and return the executor with its graph rebuilt, for callers that
    need the historical ``project_state`` afterwards."""

    def step() -> MigrationExecutor:
        executor = MigrationExecutor(connection)
        executor.migrate(list(targets))
        executor.loader.build_graph()
        return executor

    return run_replay_step(step)


def _leaf_step() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    executor.loader.build_graph()


def migrate_to_leaf_nodes() -> None:
    """Bring every app forward to its leaf migration as a step *of* a test.

    Interruptible, like :func:`migrate_to`, so a stuck replay still times out.
    The ``finally`` restore is :func:`restore_leaf_nodes`.
    """
    run_replay_step(_leaf_step)


def restore_leaf_nodes() -> None:
    """Put every app back at its leaf migration.

    This is the half of a replay that must complete -- call it from the
    ``finally`` -- so it runs under :func:`uninterruptible` and retries a
    deadlock like :func:`migrate_to` does.
    """
    with uninterruptible():
        migrate_to_leaf_nodes()
