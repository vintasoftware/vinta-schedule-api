"""Acceptance test for the seeded-database migration path.

Phase 1b of the vinta-django-orgs migration (see ai-plans/2026-08-12-VINTA_
DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md) chose option (a) from its
Changes list: a management command
(``tenancy.management.commands.rename_organizations_migration_history``)
that rewrites ``django_migrations.app`` from ``organizations`` to ``tenancy``
before ``migrate`` runs, rather than requiring every pre-existing database to
be dropped and rebuilt from scratch. See that command's module docstring for
why this cannot be a Django migration.

This builds the pre-rename state by directly rewriting ``django_migrations``
rows in the (already fully-migrated) test database: the 22 migrations that
existed under this app before Phase 1a's rename are relabelled back onto
``organizations``, exactly matching what a database created before this
branch actually has on disk. That is sufficient to reproduce the bug --
nothing about the table contents needs to change, since Phase 1a pinned every
table to its pre-rename name and renamed no data.

Like ``payments/tests/test_billing_period_summary_model.py::
TestBillingPeriodSummaryMigration``, this drives the migration executor
directly against the shared per-worker test database and restores it in a
``finally`` block so later tests in this worker are unaffected regardless of
assertion outcome -- hence ``@pytest.mark.django_db(transaction=True)``
(direct DDL/DML against ``django_migrations`` must actually commit to be
visible to a fresh ``MigrationExecutor``, so the transactional-rollback
default fixture is the wrong tool here).

Also covers the command's two Phase 1c guards (see
``rename_organizations_migration_history``'s module docstring): the
migration-name scope (only the 22 pre-rename ``tenancy`` migration names are
ever rewritten, never an unrelated ``organizations``-labelled row) and the
``CommandError`` refusal once the vinta-django-orgs package's own
``organizations`` app is installed.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.exceptions import InconsistentMigrationHistory
from django.db.migrations.executor import MigrationExecutor

import pytest
from model_bakery import baker

from audit.factories import AuditFactory
from tenancy.models import Organization


# The migration immediately after which this phase's own new migrations
# start landing (0023 onward). Everything before it is a real pre-rename
# migration: a database seeded before this branch could not possibly have a
# django_migrations row for 0023, so it is excluded from the simulated
# pre-rename state.
_PRE_RENAME_CUTOFF = "0023_"

# Apps whose migration plan this test asserts is empty. Deliberately scoped
# rather than every leaf node in the project graph: `payments`' own migration
# test (`payments/tests/test_billing_period_summary_model.py::
# TestBillingPeriodSummaryMigration`) drives a real `MigrationExecutor`
# against the *same* shared per-worker test database and, under `pytest -n
# auto`, can interleave with this test on the same worker -- an unrelated,
# pre-existing, already-documented flake in that file's own scope (see
# ai-plans/TRACKING_VINTA_DJANGO_ORGS_MIGRATION.md and the implementer
# handoff note), not something this phase's diff touches or should chase.
# Asserting over the whole project graph would make this test collateral
# damage from that pollution; scoping to the two apps this phase's migrations
# actually touch keeps the assertion meaningful without depending on every
# other app's test-isolation discipline.
#
# Consequence: the empty-plan assertions in this file only prove the global
# `InconsistentMigrationHistory` guarantee for `tenancy` and `audit`. The
# project-wide guarantee -- that no *other* app's applied migrations were
# left inconsistent by the relabel -- comes only from
# `executor.loader.check_consistent_history(connection)` (called against the
# full graph, not a scoped one) in `test_migrate_runs_clean_from_a_simulated_
# pre_rename_state` below.
_SCOPED_APPS = frozenset({"tenancy", "audit"})

# The four models this app owns as of Phase 1b -- mirrors
# `tenancy.migrations.0023_move_content_types_to_tenancy._TENANCY_MODEL_NAMES`.
_TENANCY_MODEL_NAMES = (
    "organization",
    "organizationmembership",
    "organizationinvitation",
    "organizationbranding",
)


def _pre_rename_tenancy_migration_names() -> list[str]:
    executor = MigrationExecutor(connection)
    names = sorted(
        name
        for (app_label, name) in executor.loader.graph.nodes
        if app_label == "tenancy" and name < _PRE_RENAME_CUTOFF
    )
    return names


def _scoped_leaf_targets(executor: MigrationExecutor) -> list[tuple[str, str]]:
    return [
        (app_label, name)
        for (app_label, name) in executor.loader.graph.leaf_nodes()
        if app_label in _SCOPED_APPS
    ]


@pytest.mark.django_db(transaction=True)
class TestSeededDatabaseMigrationPath:
    def test_migrate_runs_clean_from_zero(self):
        """The test database itself is the "from zero" proof: pytest-django
        builds it by running every migration, from an empty database, before
        any test executes. If that had failed, no test in this suite would
        run at all. This test just makes the claim explicit and checks the
        plan is empty (nothing pending) for the two apps this phase's
        migrations touch, at the point this test runs."""
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        plan = executor.migration_plan(_scoped_leaf_targets(executor))

        assert plan == []

    def test_migrate_runs_clean_from_a_simulated_pre_rename_state(self):
        pre_rename_names = _pre_rename_tenancy_migration_names()
        # Sanity: the plan's Phase 1a Changes list describes exactly 22
        # migrations existing under the pre-rename app. A count far below
        # that would mean this test built its fixture wrong, not that the
        # command under test is broken.
        assert len(pre_rename_names) >= 22

        placeholders = ", ".join(["%s"] * len(pre_rename_names))

        try:
            # --- Step 1: simulate the pre-rename seeded state. ---
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE django_migrations SET app = 'organizations' "
                    f"WHERE app = 'tenancy' AND name IN ({placeholders})",
                    pre_rename_names,
                )
                cursor.execute("SELECT COUNT(*) FROM django_migrations WHERE app = 'organizations'")
                (organizations_row_count,) = cursor.fetchone()
            assert organizations_row_count == len(pre_rename_names)

            # --- Step 2: prove the bug. `manage.py migrate` calls
            # `loader.check_consistent_history(connection)` before computing
            # any plan (django/core/management/commands/migrate.py). Every
            # migration still recorded as applied whose graph dependency is
            # one of the just-relabelled rows (e.g. `tenancy.0023` depends on
            # `tenancy.0022`, now recorded under `organizations` instead) now
            # has an "applied before its dependency" gap, so this raises
            # `InconsistentMigrationHistory` -- the real failure a genuinely
            # pre-rename seeded database hits, before Django ever reaches the
            # `CreateModel` step that would otherwise raise `DuplicateTable`.
            executor = MigrationExecutor(connection)
            executor.loader.build_graph()
            with pytest.raises(InconsistentMigrationHistory):
                executor.loader.check_consistent_history(connection)

            # --- Step 3: run the fix. ---
            call_command("rename_organizations_migration_history")

            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM django_migrations WHERE app = 'organizations'")
                (remaining_organizations_rows,) = cursor.fetchone()
            assert remaining_organizations_rows == 0

            # --- Step 4: `migrate` is now a clean no-op: the history is
            # consistent again and nothing is pending for the two apps this
            # phase touches ... ---
            executor = MigrationExecutor(connection)
            executor.loader.build_graph()
            executor.loader.check_consistent_history(connection)  # must not raise
            plan = executor.migration_plan(_scoped_leaf_targets(executor))
            assert plan == []

            # ... and actually invoking `migrate` (the real entry point, which
            # also runs Django's own `check_consistent_history` guard) does
            # not raise and attempts no DDL. Scoped to the two apps this test
            # cares about -- see `_SCOPED_APPS` -- rather than a global
            # `migrate`, so an unrelated app's own test-isolation issue can
            # never turn into a failure here.
            for app_label in _SCOPED_APPS:
                call_command("migrate", app_label, verbosity=0)
        finally:
            # Restore head state so later tests in this worker's database are
            # unaffected, regardless of where an assertion above failed.
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE django_migrations SET app = 'tenancy' WHERE app = 'organizations'"
                )

    def test_migrate_runs_this_phases_own_migrations_against_pre_rename_data(self):
        """Extends the simulated pre-rename state above so `tenancy.0023` and
        `audit.0002` are genuinely *pending*, not already applied -- the
        earlier test only relabels `0001`..`0022`, leaving `0023` and
        `audit.0002` recorded as applied and the content types already at
        `tenancy`, so `migrate` has nothing left to do for either migration.
        This additionally deletes their `django_migrations` rows, relabels
        the four content types back to `organizations` (deleting the
        `tenancy` ones this test database auto-created), and seeds one
        pre-rename-shaped audit row, then proves the fix command + a real
        `migrate` actually run both migrations and produce the same
        post-rename state.
        """
        pre_rename_names = _pre_rename_tenancy_migration_names()
        placeholders = ", ".join(["%s"] * len(pre_rename_names))

        org = baker.make(Organization)
        stale_audit = AuditFactory().create(
            organization=org, subject_type="organizations.Organization"
        )

        original_content_type_ids = {
            model_name: ContentType.objects.get(app_label="tenancy", model=model_name).pk
            for model_name in _TENANCY_MODEL_NAMES
        }

        try:
            # --- Step 1: relabel the 22 pre-rename tenancy migrations, same
            # as the base simulated state. ---
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE django_migrations SET app = 'organizations' "
                    f"WHERE app = 'tenancy' AND name IN ({placeholders})",
                    pre_rename_names,
                )
                # --- Step 2: this phase's own migrations were never applied
                # against a genuinely pre-rename database -- delete their
                # applied rows so `migrate` actually has to run them.
                cursor.execute(
                    "DELETE FROM django_migrations WHERE "
                    "(app = 'tenancy' AND name = '0023_move_content_types_to_tenancy') "
                    "OR (app = 'audit' AND name = '0002_backfill_subject_type_namespace')"
                )

            # --- Step 3: relabel the content types back to 'organizations',
            # deleting the head-state 'tenancy' rows -- the shape a database
            # seeded before Phase 1a's rename actually has. ---
            for content_type_id in original_content_type_ids.values():
                ContentType.objects.filter(pk=content_type_id).update(app_label="organizations")

            # --- Step 4: run the fix, then the real migrate for the two
            # apps this phase touches. ---
            call_command("rename_organizations_migration_history")
            for app_label in _SCOPED_APPS:
                call_command("migrate", app_label, verbosity=0)

            # --- Assert: `tenancy.0023` actually ran and moved the content
            # types back to 'tenancy', at their original ids. ---
            for content_type_id in original_content_type_ids.values():
                content_type = ContentType.objects.get(pk=content_type_id)
                assert content_type.app_label == "tenancy"

            # --- Assert: `audit.0002` actually ran and rewrote the
            # pre-rename `subject_type`. ---
            stale_audit.refresh_from_db()
            assert stale_audit.subject_type == "tenancy.Organization"
        finally:
            # Restore head state so later tests in this worker's database are
            # unaffected, regardless of where an assertion above failed. Both
            # calls are idempotent no-ops if the try block already reached
            # them successfully.
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE django_migrations SET app = 'tenancy' WHERE app = 'organizations'"
                )
            for app_label in _SCOPED_APPS:
                call_command("migrate", app_label, verbosity=0)
            for content_type_id in original_content_type_ids.values():
                ContentType.objects.filter(pk=content_type_id).update(app_label="tenancy")

    def test_command_is_idempotent(self):
        call_command("rename_organizations_migration_history")
        call_command("rename_organizations_migration_history")

        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM django_migrations WHERE app = 'organizations'")
            (organizations_row_count,) = cursor.fetchone()
        assert organizations_row_count == 0

    def test_dry_run_does_not_change_anything(self):
        pre_rename_names = _pre_rename_tenancy_migration_names()[:1]

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE django_migrations SET app = 'organizations' "
                    "WHERE app = 'tenancy' AND name = %s",
                    pre_rename_names,
                )

            call_command("rename_organizations_migration_history", "--dry-run")

            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM django_migrations WHERE app = 'organizations'")
                (organizations_row_count,) = cursor.fetchone()
            assert organizations_row_count == 1
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE django_migrations SET app = 'tenancy' WHERE app = 'organizations'"
                )


@pytest.mark.django_db(transaction=True)
class TestRenameCommandGuardsAgainstPackageOwnedRows:
    """BLOCKER 1 (Phase 1b review): Phase 1c installs the vinta-django-orgs
    package's own ``organizations.apps.OrganizationsConfig``, which records
    its migrations in ``django_migrations`` under the same ``organizations``
    label this command used to rewrite unconditionally. Both guards the
    command now has are exercised here: the name-scoped ``UPDATE`` (only the
    22 pre-rename ``tenancy`` migration names are ever touched) and the
    ``CommandError`` refusal once the package's app is installed.
    """

    def test_out_of_scope_organizations_row_is_left_untouched(self):
        """A ``django_migrations`` row labelled ``organizations`` whose name
        is not one of this app's 22 pre-rename migrations -- the shape a
        package-owned row would have -- must survive the rewrite even though
        it carries the same app label as the rows this command does move.
        """
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO django_migrations (app, name, applied) "
                "VALUES ('organizations', '9999_package_owned_migration', now())"
            )

        try:
            call_command("rename_organizations_migration_history")

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT app FROM django_migrations WHERE name = '9999_package_owned_migration'"
                )
                (app_label,) = cursor.fetchone()
            assert app_label == "organizations"
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM django_migrations WHERE name = '9999_package_owned_migration'"
                )
                cursor.execute(
                    "UPDATE django_migrations SET app = 'tenancy' WHERE app = 'organizations'"
                )

    def test_refuses_to_run_when_the_organizations_app_is_installed(self):
        """Once Phase 1c installs the package's own ``organizations`` app,
        this command must refuse outright rather than attempt any rewrite --
        by that point, this command's job is already done, and any
        ``organizations``-labelled row left in ``django_migrations`` belongs
        to the package, not to this app's pre-rename history.
        """
        with patch(
            "tenancy.management.commands.rename_organizations_migration_history"
            ".django_apps.is_installed",
            return_value=True,
        ):
            with pytest.raises(CommandError):
                call_command("rename_organizations_migration_history")
