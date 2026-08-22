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
"""

from __future__ import annotations

import signal
from collections.abc import Iterator
from contextlib import contextmanager

import pytest


#: Not a performance budget -- a hang guard, like the global one. A replay that
#: takes ten minutes is stuck, not slow.
MIGRATION_REPLAY_TIMEOUT_SECONDS = 600

#: Marks a test that replays migrations against the shared per-worker database.
#: Apply to the class or the function; see the module docstring.
migration_replay = pytest.mark.timeout(MIGRATION_REPLAY_TIMEOUT_SECONDS)


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
