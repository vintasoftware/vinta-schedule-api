"""Fix migration identity on a database seeded before the tenancy rename.

**Phase 1b of the vinta-django-orgs migration** (see ai-plans/2026-08-12-
VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md; carried forward from the
Phase 1a review, see ai-plans/TRACKING_VINTA_DJANGO_ORGS_MIGRATION.md).

Phase 1a renamed this app's code from ``organizations`` to ``tenancy`` and
pinned every table to its pre-rename ``organizations_*`` name -- no table
moved. Django's migration graph, however, is keyed by *app label*, not table
name: a database that was migrated before this rename landed has
``django_migrations`` rows keyed ``('organizations', '0001_initial')``
through ``('organizations', '0022_organization_week_start')``. After the
rename, the on-disk migration graph resolves those same 22 files as
``tenancy.0001_initial`` through ``tenancy.0022_organization_week_start`` --
a *different* migration identity from the loader's point of view, even
though every model and every table is unchanged. Running ``migrate`` against
such a database calls ``loader.check_consistent_history(connection)`` before
computing any plan, and that raises ``InconsistentMigrationHistory``: some
already-applied migration in another app (``audit``, ``calendar_integration``,
``payments``, ``public_api``, ``webhooks``, ...) depends on one of these 22
migrations, and that dependency now appears unapplied under its new
``tenancy`` identity.

This is the fix, and it must run **before** ``migrate`` — not as a Django
migration itself. A migration cannot fix its own app's identity: the loader
decides what is "pending" by reading ``django_migrations`` *before* it runs
anything, so a `RunPython` step inside the ``tenancy`` graph would never even
get a chance to execute (the loader would already be stuck on the
inconsistency first).

Idempotent: only rows whose ``name`` matches one of the 22 pre-rename
``tenancy`` migrations (``0001_initial`` … ``0022_organization_week_start``,
computed from the on-disk migration graph, not hardcoded) are rewritten from
``organizations`` to ``tenancy``. Re-running after the rename (no matching
rows left) is a no-op.

**Guarded against Phase 1c.** Phase 1c installs
``organizations.apps.OrganizationsConfig`` -- the vinta-django-orgs package's
own Django app -- which records its own migrations in ``django_migrations``
under the label ``organizations`` (a legitimate use of that label, distinct
from this app's pre-rename history). Once that app is installed, a blanket
rewrite would corrupt its applied history by relabelling the package's own
rows onto ``tenancy``, causing the next ``migrate`` to re-run
``organizations.0001_initial`` against tables that already exist. This
command therefore refuses to run at all (``CommandError``) once
``organizations`` is an installed app -- this command's job is already done
by then; there is nothing left in ``django_migrations`` under the old label
that legitimately belongs to *this* app's pre-Phase-1c history.

Usage, once, before ``migrate``, against any database created before this
branch::

    python manage.py rename_organizations_migration_history
"""

from __future__ import annotations

from typing import Any

from django.apps import apps as django_apps
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection
from django.db.migrations.loader import MigrationLoader


#: The app label this app's migrations were recorded under before Phase 1a's
#: rename, and the label the vinta-django-orgs package's own app claims from
#: Phase 1c onward -- see the module docstring's "Guarded against Phase 1c"
#: section for why those two facts make an unconditional rewrite unsafe.
_PRE_RENAME_APP_LABEL = "organizations"

#: The app label this app's migrations are recorded under from Phase 1a onward.
_RENAMED_APP_LABEL = "tenancy"

#: Migrations numbered at or above this are Phase 1b and later -- new since
#: the rename, so they were never recorded under the old label by any
#: pre-branch database. Excluding them from the rewrite scope means this
#: command can never touch a row it shouldn't, even by coincidence of name.
_PRE_RENAME_CUTOFF = "0023_"


def _pre_rename_migration_names() -> list[str]:
    """The on-disk ``tenancy`` migration names that predate the rename.

    Computed from the actual migration graph (not hardcoded) so this stays
    correct if the pre-rename range is ever revisited.
    """
    loader = MigrationLoader(None, ignore_no_migrations=True)
    return sorted(
        name
        for (app_label, name) in loader.disk_migrations
        if app_label == _RENAMED_APP_LABEL and name < _PRE_RENAME_CUTOFF
    )


class Command(BaseCommand):
    """Rewrite ``django_migrations.app`` from ``organizations`` to ``tenancy``."""

    help = (  # noqa: A003
        "Rewrites django_migrations rows keyed under the pre-rename 'organizations' "
        "app label onto 'tenancy', so `migrate` recognizes them as already applied "
        "instead of re-attempting them and hitting InconsistentMigrationHistory. "
        "Idempotent -- safe to run against a database that has already been fixed "
        "or that was never seeded pre-rename. Refuses to run (CommandError) once "
        "the vinta-django-orgs package's own 'organizations' app is installed "
        "(Phase 1c onward) -- see the module docstring."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many rows would be rewritten without changing anything.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if django_apps.is_installed(_PRE_RENAME_APP_LABEL):
            raise CommandError(
                "The 'organizations' app is installed (Phase 1c or later). From that "
                "point on, 'organizations'-labelled rows in django_migrations belong "
                "to the vinta-django-orgs package's own app, not to this app's "
                "pre-rename history -- rewriting them onto 'tenancy' would corrupt "
                "the package's applied-migration state and cause `migrate` to "
                "re-attempt migrations against tables that already exist. This "
                "command is only for databases seeded before Phase 1a's rename, on "
                "a checkout before Phase 1c installed the package's app."
            )

        dry_run: bool = options["dry_run"]
        pre_rename_names = _pre_rename_migration_names()

        with connection.cursor() as cursor:
            # `name = ANY(%s)` (Postgres array binding) rather than a
            # dynamically-sized `IN (%s, %s, ...)` clause -- the query text is
            # fully static, and `pre_rename_names` is bound as a single
            # parameter, so there is no string-based SQL construction here.
            cursor.execute(
                "SELECT COUNT(*) FROM django_migrations WHERE app = %s AND name = ANY(%s)",
                [_PRE_RENAME_APP_LABEL, pre_rename_names],
            )
            (pending_count,) = cursor.fetchone()

            if pending_count == 0:
                self.stdout.write(
                    self.style.SUCCESS(
                        "No pre-rename 'organizations'-labelled rows found in "
                        "django_migrations -- nothing to do (already fixed, or this "
                        "database was only ever migrated post-rename)."
                    )
                )
                return

            if dry_run:
                self.stdout.write(
                    f"Would rewrite {pending_count} django_migrations row(s) from "
                    "'organizations' to 'tenancy'."
                )
                return

            cursor.execute(
                "UPDATE django_migrations SET app = %s WHERE app = %s AND name = ANY(%s)",
                [_RENAMED_APP_LABEL, _PRE_RENAME_APP_LABEL, pre_rename_names],
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Rewrote {pending_count} django_migrations row(s) from 'organizations' "
                "to 'tenancy'. `migrate` can now be run safely."
            )
        )
