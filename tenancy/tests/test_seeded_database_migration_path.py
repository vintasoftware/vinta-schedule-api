"""The seeded-database migration path, and the point at which it closes.

Phase 1b of the vinta-django-orgs migration (see ai-plans/2026-08-12-VINTA_
DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md) shipped a management command
(``tenancy.management.commands.rename_organizations_migration_history``) that
rewrites ``django_migrations.app`` from ``organizations`` to ``tenancy`` before
``migrate`` runs, so a database created before Phase 1a's app rename does not
have to be dropped and rebuilt. See that command's module docstring for why it
cannot be a Django migration.

**Phase 1c closes that window, and this module is what that looks like in
tests.** Phase 1c installs ``organizations.apps.OrganizationsConfig`` -- the
vinta-django-orgs package's own Django app -- which records *its* migrations
under the same ``organizations`` label. From that moment the command refuses to
run at all, and its refusal is not belt-and-braces: the package's own
``organizations.0001_initial`` shares a name with this app's pre-rename
``0001_initial``, so the command's name-scoped ``UPDATE`` -- its other guard --
cannot tell the two apart and would relabel the package's row. The hard
``CommandError`` is the guard that actually holds. ``test_the_name_scope_alone_
would_not_have_been_enough`` pins exactly that, so a future change that softens
the refusal into a warning has a red test waiting for it.

Phase 1b's happy-path acceptance tests (build the pre-rename state, prove
``InconsistentMigrationHistory``, run the fix, prove ``migrate`` is clean
afterwards) are **deliberately gone** rather than kept alive by mocking the
guard off. Mocking it off would exercise a path that is now actively unsafe in
this codebase -- it would rewrite the package's applied-migration row against
the shared test database -- and a test that has to disable a safety check to
reach its subject is testing something the code no longer does. Their coverage
lives on the Phase 1b branch, where the command is reachable. See
``ai-plans/TRACKING_VINTA_DJANGO_ORGS_MIGRATION.md``.

``@pytest.mark.django_db(transaction=True)`` on the migration-plan test for the
same reason as before: a fresh ``MigrationExecutor`` reads committed state.
"""

from __future__ import annotations

from django.apps import apps as django_apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader

import pytest

from tenancy.management.commands.rename_organizations_migration_history import (
    _PRE_RENAME_CUTOFF,
    _pre_rename_migration_names,
)


# Apps whose migration plan this test asserts is empty. Deliberately scoped
# rather than every leaf node in the project graph: `payments`' own migration
# test (`payments/tests/test_billing_period_summary_model.py::
# TestBillingPeriodSummaryMigration`) drives a real `MigrationExecutor`
# against the *same* shared per-worker test database and, under `pytest -n
# auto`, can interleave with this test on the same worker -- an unrelated,
# pre-existing, already-documented flake in that file's own scope (see
# ai-plans/TRACKING_VINTA_DJANGO_ORGS_MIGRATION.md), not something this phase's
# diff touches or should chase. `organizations` is in the set from Phase 1c
# onward: the package's own five migrations have to apply cleanly too, and this
# is the assertion that says so.
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
        plan is empty (nothing pending) for the three apps this phase's
        migrations touch, at the point this test runs.

        Phase 1c makes this a real assertion rather than a formality: it adds
        four ``tenancy`` migrations (including a primary-key swap driven by raw
        SQL under ``SeparateDatabaseAndState``, which the autodetector cannot
        check for itself) and the package's own five.
        """
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        plan = executor.migration_plan(_scoped_leaf_targets(executor))

        assert plan == []


class TestRenameCommandIsClosedFromPhase1cOnward:
    """The command's two guards, now that the package's app is installed.

    No ``django_db`` mark: neither test reaches the database. That is the
    point -- the refusal happens before any query.
    """

    def test_the_organizations_app_really_is_installed(self):
        """Pins the premise the two tests below rest on. Without this, a future
        settings change that dropped the package's app would silently turn both
        of them into vacuous passes."""
        assert django_apps.is_installed("organizations")

    def test_refuses_to_run(self):
        """The command refuses outright rather than attempting any rewrite."""
        with pytest.raises(CommandError, match="Phase 1c or later"):
            call_command("rename_organizations_migration_history")

    def test_refuses_before_considering_dry_run(self):
        """``--dry-run`` is not an escape hatch: the guard runs first, so even
        the read-only mode refuses rather than reporting a row count it would
        be unsafe to act on."""
        with pytest.raises(CommandError, match="Phase 1c or later"):
            call_command("rename_organizations_migration_history", "--dry-run")

    def test_the_name_scope_alone_would_not_have_been_enough(self):
        """Why the ``CommandError`` above is load-bearing and not redundant.

        The command's other guard scopes its ``UPDATE`` to the migration names
        this app carried before the rename. That scope is computed from the
        on-disk graph and still excludes everything from ``0023_`` up -- but it
        **overlaps the package's own migration names**, starting with
        ``0001_initial``. So with the refusal removed, the command would relabel
        ``('organizations', '0001_initial')`` -- the package's applied-migration
        row -- onto ``tenancy``, and the next ``migrate`` would re-run
        ``organizations.0001_initial`` against tables that already exist.
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
