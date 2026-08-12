"""Acceptance tests for the seeded-database migration path.

Phase 1b of the vinta-django-orgs migration (see ai-plans/2026-08-12-VINTA_
DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md) shipped a management command
(``tenancy.management.commands.rename_organizations_migration_history``) that
rewrites ``django_migrations.app`` from ``organizations`` to ``tenancy`` before
``migrate`` runs, so a database created before Phase 1a's app rename does not
have to be dropped and rebuilt. See that command's module docstring for why it
cannot be a Django migration.

**The guard, and why this module changed twice.** Phase 1c installed
``organizations.apps.OrganizationsConfig`` -- the vinta-django-orgs package's
own Django app -- which is installed for the lifetime of every checkout from
that phase onward. The command originally guarded on a **code** fact
(``CommandError`` whenever ``apps.is_installed("organizations")``), which
Phase 1c made permanently true and therefore refused the command
unconditionally on `main` -- including against the one database shape it
exists to fix. This module's Phase 1c version accepted that as a closed
window and deleted the happy-path acceptance tests rather than keep them
alive by mocking the guard off.

The Phase 1c **review** replaced that guard with a **data** fact instead --
``SELECT COUNT(*) FROM django_migrations WHERE app = 'tenancy'``: zero means a
genuinely pre-Phase-1a database (the label did not exist before Phase 1a, so
nothing could have been recorded under it), non-zero means every other case,
including "the package has since recorded its own rows under `organizations`
'from Phase 1c onward". That makes the guard reachable again for a genuinely
pre-rename database, so the happy-path acceptance tests are restored below --
they no longer need the guard mocked off.

Reaching "zero `tenancy` rows" against the shared, already-fully-migrated test
database is **bookkeeping-only**, not a real schema reversal: every currently
``tenancy``-labelled ``django_migrations`` row is either relabelled (the 22
pre-rename names, matching what a real pre-rename database's history actually
looked like -- Phase 1a pinned every table name, so no table content needs to
change to reproduce this) or removed outright (everything from ``0023_``
onward, which did not exist as a migration file before the rename, so a
genuinely pre-rename database never had a row for it under any label -- and
``audit.0002_backfill_subject_type_namespace``, the one migration anywhere in
the repo that declares a dependency on one of those, removed alongside it so
nothing is left "applied before its dependency"). The real schema is never
touched -- ``django_migrations`` carries no FK to it -- so restoring the
snapshotted rows in a ``finally`` block is a complete, safe undo regardless of
where an assertion fails. This is the same technique the original Phase 1b
tests used, extended to the larger post-1c ``tenancy`` graph, and it is why
this module does not attempt to actually re-invoke ``migrate`` for the
migrations it removes the bookkeeping row for: ``0024`` onward carry real,
non-idempotent schema changes (a primary-key swap, a NOT NULL alter) that the
live schema already has applied, so re-running them for real would fail
against a database that already matches their end state -- unlike ``0023``
and ``audit.0002``, which are idempotent data migrations the original tests
could and did re-run for real.

``@pytest.mark.django_db(transaction=True)`` throughout: direct DDL/DML
against ``django_migrations`` must actually commit to be visible to a fresh
``MigrationExecutor``, so the transactional-rollback default fixture is the
wrong tool here -- matching ``payments/tests/test_billing_period_summary_model
.py::TestBillingPeriodSummaryMigration``'s precedent for direct
``MigrationExecutor`` manipulation of the shared per-worker database.
"""

from __future__ import annotations

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.exceptions import InconsistentMigrationHistory
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader

import pytest

from tenancy.management.commands.rename_organizations_migration_history import (
    _PRE_RENAME_CUTOFF,
    _pre_rename_migration_names,
)


# Apps whose migration plan this test asserts on. Deliberately scoped rather
# than every leaf node in the project graph: `payments`' own migration test
# (`payments/tests/test_billing_period_summary_model.py::
# TestBillingPeriodSummaryMigration`) drives a real `MigrationExecutor`
# against the *same* shared per-worker test database and, under `pytest -n
# auto`, can interleave with this test on the same worker -- an unrelated,
# pre-existing, already-documented flake in that file's own scope (see
# ai-plans/TRACKING_VINTA_DJANGO_ORGS_MIGRATION.md), not something this
# module's diff touches or should chase. `organizations` is in the set from
# Phase 1c onward: the package's own migrations have to apply cleanly too.
_SCOPED_APPS = frozenset({"tenancy", "audit", "organizations"})


def _scoped_leaf_targets(executor: MigrationExecutor) -> list[tuple[str, str]]:
    return [
        (app_label, name)
        for (app_label, name) in executor.loader.graph.leaf_nodes()
        if app_label in _SCOPED_APPS
    ]


@pytest.mark.django_db(transaction=True)
class TestMigrateRunsCleanFromZero:
    def test_migrate_runs_clean_from_zero(self):
        """The test database itself is the "from zero" proof: pytest-django
        builds it by running every migration, from an empty database, before
        any test executes. If that had failed, no test in this suite would
        run at all. This test just makes the claim explicit and checks the
        plan is empty (nothing pending) for the three apps this module cares
        about, at the point this test runs.
        """
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        plan = executor.migration_plan(_scoped_leaf_targets(executor))

        assert plan == []


@pytest.mark.django_db(transaction=True)
class TestSeededDatabaseMigrationPath:
    """The restored happy path: a genuinely pre-Phase-1a database, simulated
    at the ``django_migrations`` bookkeeping level -- see the module
    docstring for exactly what that does and does not touch.
    """

    def test_migrate_runs_clean_from_a_simulated_pre_rename_state(self):
        pre_rename_names = _pre_rename_migration_names()
        # Sanity: the plan's Phase 1a Changes list describes exactly 22
        # migrations existing under the pre-rename app. A count far below
        # that would mean this test built its fixture wrong, not that the
        # command under test is broken.
        assert len(pre_rename_names) == 22
        assert all(name < _PRE_RENAME_CUTOFF for name in pre_rename_names)

        with connection.cursor() as cursor:
            # Snapshot every currently-applied `tenancy` row this simulation
            # is about to remove the bookkeeping for -- computed rather than
            # hardcoded, so this stays correct as the `tenancy` graph grows
            # past whatever it was when this test was written.
            cursor.execute(
                "SELECT name, applied FROM django_migrations WHERE app = 'tenancy' AND name >= %s",
                [_PRE_RENAME_CUTOFF],
            )
            post_rename_tenancy_rows = cursor.fetchall()
            assert post_rename_tenancy_rows, (
                "sanity: at least one post-Phase-1b tenancy migration must exist"
            )

            # `audit.0002_backfill_subject_type_namespace` is the one migration
            # anywhere in the repo that declares a dependency on
            # `tenancy.0023_move_content_types_to_tenancy` (verified by grep
            # across every `*/migrations/*.py` file) -- removed alongside it so
            # nothing is left "applied before its dependency" once `0023`'s own
            # bookkeeping row is gone. Nothing depends on `tenancy` 0024 or
            # later at all.
            cursor.execute(
                "SELECT name, applied FROM django_migrations "
                "WHERE app = 'audit' AND name = '0002_backfill_subject_type_namespace'"
            )
            audit_dependent_rows = cursor.fetchall()
            assert audit_dependent_rows, "sanity: audit.0002 must be applied already"

            # The package's own real, already-applied `organizations` rows --
            # its whole chain, not just the one name (`0001_initial`) that
            # happens to collide with a pre-rename `tenancy` migration (see
            # `TestTheNameScopeAloneWouldNotHaveBeenEnough`). A genuinely
            # pre-Phase-1a database could never have any of these: `migrate`
            # (which is what would apply them) has not run yet on it, that
            # being the whole scenario this command exists for -- so this
            # shared, already-fully-migrated test database has to have the
            # whole chain set aside for the simulation below to be faithful.
            # Setting aside only the colliding name would leave the package's
            # *own* later migrations (`0002` onward) looking applied while
            # their own dependency (`0001_initial`, now relabelled `tenancy`
            # by the fix) is not -- a second, self-inflicted
            # `InconsistentMigrationHistory` that has nothing to do with what
            # this test is proving.
            with connection.cursor() as inner_cursor:
                inner_cursor.execute(
                    "SELECT name, applied FROM django_migrations WHERE app = 'organizations'"
                )
                package_rows = inner_cursor.fetchall()
            assert package_rows, "sanity: the package's own app must be really installed"

        try:
            with connection.cursor() as cursor:
                # --- Step 0: set the package's whole real chain aside for the
                # duration of the simulation (see above). ---
                cursor.execute(
                    "UPDATE django_migrations SET app = '__pkg_snapshot__' "
                    "WHERE app = 'organizations'"
                )

                # --- Step 1: simulate the pre-rename seeded state. ---
                # The 22 pre-rename migrations: relabel onto 'organizations',
                # matching what a real pre-rename database's history actually
                # looked like.
                cursor.execute(
                    "UPDATE django_migrations SET app = 'organizations' "
                    "WHERE app = 'tenancy' AND name = ANY(%s)",
                    [pre_rename_names],
                )
                cursor.execute(
                    "SELECT COUNT(*) FROM django_migrations "
                    "WHERE app = 'organizations' AND name = ANY(%s)",
                    [pre_rename_names],
                )
                (relabelled_count,) = cursor.fetchone()
                assert relabelled_count == len(pre_rename_names)

                # Everything from `0023_` onward didn't exist as a migration
                # file before the rename, so a genuinely pre-rename database
                # never recorded it under any label -- and its one dependent
                # (audit.0002) goes with it.
                cursor.execute(
                    "DELETE FROM django_migrations WHERE app = 'tenancy' AND name >= %s",
                    [_PRE_RENAME_CUTOFF],
                )
                cursor.execute(
                    "DELETE FROM django_migrations "
                    "WHERE app = 'audit' AND name = '0002_backfill_subject_type_namespace'"
                )
                cursor.execute("SELECT COUNT(*) FROM django_migrations WHERE app = 'tenancy'")
                (tenancy_row_count,) = cursor.fetchone()
            assert tenancy_row_count == 0, (
                "the guard's discriminator -- this simulation must reach it exactly"
            )

            # --- Step 2: prove the bug. `manage.py migrate` calls
            # `loader.check_consistent_history(connection)` before computing
            # any plan (django/core/management/commands/migrate.py). Some
            # already-applied migration outside `tenancy` (e.g. `audit.0001`,
            # which depends on `tenancy.0013`) now has a dependency recorded
            # under the wrong label, so this raises `InconsistentMigrationHistory`
            # -- the real failure a genuinely pre-rename seeded database hits,
            # before Django ever reaches a `CreateModel` step that would
            # otherwise raise `DuplicateTable`.
            executor = MigrationExecutor(connection)
            executor.loader.build_graph()
            with pytest.raises(InconsistentMigrationHistory):
                executor.loader.check_consistent_history(connection)

            # --- Step 3: run the fix. The guard sees zero `tenancy` rows and
            # proceeds. ---
            call_command("rename_organizations_migration_history")

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM django_migrations "
                    "WHERE app = 'organizations' AND name = ANY(%s)",
                    [pre_rename_names],
                )
                (remaining_organizations_rows,) = cursor.fetchone()
                cursor.execute(
                    "SELECT COUNT(*) FROM django_migrations "
                    "WHERE app = 'tenancy' AND name = ANY(%s)",
                    [pre_rename_names],
                )
                (restored_tenancy_rows,) = cursor.fetchone()
            assert remaining_organizations_rows == 0
            assert restored_tenancy_rows == len(pre_rename_names)

            # --- Step 4: history is consistent again for everything the fix
            # touched. `0023` onward are genuinely pending now (their
            # bookkeeping row was removed in Step 1, same as a real pre-1a
            # database), which is not an inconsistency -- nothing depends on
            # them being applied (audit.0002, the one migration that did, was
            # removed alongside `0023`). Confirmed both ways: the consistency
            # check no longer raises, and the migration plan for `tenancy` +
            # `audit` shows exactly the backlog a genuine pre-1a database
            # would have, and nothing else. ---
            executor = MigrationExecutor(connection)
            executor.loader.build_graph()
            executor.loader.check_consistent_history(connection)  # must not raise

            plan_targets = [
                (app_label, name)
                for (app_label, name) in executor.loader.graph.leaf_nodes()
                if app_label in {"tenancy", "audit"}
            ]
            pending = {
                (migration.app_label, migration.name)
                for migration, _backwards in executor.migration_plan(plan_targets)
            }
            expected_pending = {("tenancy", name) for name, _ in post_rename_tenancy_rows} | {
                ("audit", name) for name, _ in audit_dependent_rows
            }
            assert pending == expected_pending
        finally:
            # Restore head state so later tests in this worker's database are
            # unaffected, regardless of where an assertion above failed. Pure
            # bookkeeping -- the real schema was never touched, so restoring
            # these rows (with their original `applied` timestamps) is a
            # complete undo, not merely best-effort.
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE django_migrations SET app = 'tenancy' "
                    "WHERE app = 'organizations' AND name = ANY(%s)",
                    [pre_rename_names],
                )
                for name, applied in post_rename_tenancy_rows:
                    cursor.execute(
                        "INSERT INTO django_migrations (app, name, applied) "
                        "SELECT %s, %s, %s WHERE NOT EXISTS ("
                        "  SELECT 1 FROM django_migrations WHERE app = %s AND name = %s"
                        ")",
                        ["tenancy", name, applied, "tenancy", name],
                    )
                for name, applied in audit_dependent_rows:
                    cursor.execute(
                        "INSERT INTO django_migrations (app, name, applied) "
                        "SELECT %s, %s, %s WHERE NOT EXISTS ("
                        "  SELECT 1 FROM django_migrations WHERE app = %s AND name = %s"
                        ")",
                        ["audit", name, applied, "audit", name],
                    )
                # The package's whole real chain, set aside in Step 0.
                cursor.execute(
                    "UPDATE django_migrations SET app = 'organizations' "
                    "WHERE app = '__pkg_snapshot__'"
                )

    def test_a_second_run_refuses_rather_than_no_ops(self):
        """Running the command twice against the same simulated pre-rename
        state: the first run fixes it for real; the second refuses outright,
        rather than silently no-op-ing the way the pre-review guard did. The
        first run's own success is what moves ``django_migrations`` out of
        the "zero `tenancy` rows" state the guard requires -- there is no
        longer a state in which a second, genuine invocation reaches the
        name-scoped rewrite at all. See the command's module docstring."""
        pre_rename_names = _pre_rename_migration_names()

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT name, applied FROM django_migrations WHERE app = 'tenancy' AND name >= %s",
                [_PRE_RENAME_CUTOFF],
            )
            post_rename_tenancy_rows = cursor.fetchall()
            cursor.execute(
                "SELECT applied FROM django_migrations "
                "WHERE app = 'audit' AND name = '0002_backfill_subject_type_namespace'"
            )
            (audit_dependent_applied,) = cursor.fetchone()
            # The package's whole real chain, set aside for the duration of
            # the simulation -- see the sibling test above for why only the
            # name-colliding row is not enough.
            cursor.execute("SELECT 1 FROM django_migrations WHERE app = 'organizations' LIMIT 1")
            assert cursor.fetchone(), "sanity: the package's own app must be really installed"

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE django_migrations SET app = '__pkg_snapshot__' "
                    "WHERE app = 'organizations'"
                )
                cursor.execute(
                    "UPDATE django_migrations SET app = 'organizations' "
                    "WHERE app = 'tenancy' AND name = ANY(%s)",
                    [pre_rename_names],
                )
                cursor.execute(
                    "DELETE FROM django_migrations WHERE app = 'tenancy' AND name >= %s",
                    [_PRE_RENAME_CUTOFF],
                )
                cursor.execute(
                    "DELETE FROM django_migrations "
                    "WHERE app = 'audit' AND name = '0002_backfill_subject_type_namespace'"
                )

            call_command("rename_organizations_migration_history")

            with pytest.raises(CommandError, match="does not predate Phase 1a"):
                call_command("rename_organizations_migration_history")

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM django_migrations "
                    "WHERE app = 'organizations' AND name = ANY(%s)",
                    [pre_rename_names],
                )
                (remaining,) = cursor.fetchone()
            assert remaining == 0
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE django_migrations SET app = 'tenancy' "
                    "WHERE app = 'organizations' AND name = ANY(%s)",
                    [pre_rename_names],
                )
                for name, applied in post_rename_tenancy_rows:
                    cursor.execute(
                        "INSERT INTO django_migrations (app, name, applied) "
                        "SELECT %s, %s, %s WHERE NOT EXISTS ("
                        "  SELECT 1 FROM django_migrations WHERE app = %s AND name = %s"
                        ")",
                        ["tenancy", name, applied, "tenancy", name],
                    )
                cursor.execute(
                    "INSERT INTO django_migrations (app, name, applied) "
                    "SELECT 'audit', '0002_backfill_subject_type_namespace', %s WHERE NOT EXISTS ("
                    "  SELECT 1 FROM django_migrations "
                    "  WHERE app = 'audit' AND name = '0002_backfill_subject_type_namespace'"
                    ")",
                    [audit_dependent_applied],
                )
                cursor.execute(
                    "UPDATE django_migrations SET app = 'organizations' "
                    "WHERE app = '__pkg_snapshot__'"
                )


@pytest.mark.django_db(transaction=True)
class TestRenameCommandRefusesOnANonZeroTenancyRowCount:
    """The guard's other branch: an ordinary database (this shared test
    database, at head, exactly as every other test in this suite finds it)
    already has many `tenancy`-labelled rows -- including, from Phase 1c
    onward, because the vinta-django-orgs package's own `organizations` app
    has recorded its own migrations too. Either way the command must refuse
    rather than attempt anything.
    """

    def test_refuses_to_run(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM django_migrations WHERE app = 'tenancy'")
            (tenancy_row_count,) = cursor.fetchone()
        assert tenancy_row_count > 0, "sanity: this test's premise is a normally-migrated database"

        with pytest.raises(CommandError, match="does not predate Phase 1a"):
            call_command("rename_organizations_migration_history")

    def test_refuses_before_considering_dry_run(self):
        """``--dry-run`` is not an escape hatch: the guard runs first, so even
        the read-only mode refuses rather than reporting a row count it would
        be unsafe to act on."""
        with pytest.raises(CommandError, match="does not predate Phase 1a"):
            call_command("rename_organizations_migration_history", "--dry-run")


class TestTheNameScopeAloneWouldNotHaveBeenEnough:
    """Why the row-count guard is load-bearing and not redundant.

    No ``django_db`` mark: this test reads only the on-disk migration graph.
    """

    def test_the_name_scope_alone_would_not_have_been_enough(self):
        """The command's other guard scopes its ``UPDATE`` to the migration
        names this app carried before the rename. That scope is computed from
        the on-disk graph and still excludes everything from ``0023_`` up --
        but it **overlaps the package's own migration names**, starting with
        ``0001_initial``. So with the row-count guard removed, the command
        would happily relabel ``('organizations', '0001_initial')`` -- the
        package's own applied-migration row, on any database where Phase 1c
        has installed its app -- onto ``tenancy``, and the next ``migrate``
        would re-run ``organizations.0001_initial`` against tables that
        already exist.
        """
        pre_rename_names = _pre_rename_migration_names()

        # The scope is still what Phase 1b computed: the 22 migrations that
        # predate the rename, and nothing from this phase or later.
        assert len(pre_rename_names) == 22
        assert all(name < _PRE_RENAME_CUTOFF for name in pre_rename_names)

        # ``MigrationLoader(None)`` reads the on-disk graph without a database
        # connection, which is why this whole class needs no ``django_db``.
        package_names = {
            name
            for (app_label, name) in MigrationLoader(
                None, ignore_no_migrations=True
            ).disk_migrations
            if app_label == "organizations"
        }

        # The overlap, concretely.
        assert "0001_initial" in pre_rename_names
        assert "0001_initial" in package_names
        assert set(pre_rename_names) & package_names == {"0001_initial"}
