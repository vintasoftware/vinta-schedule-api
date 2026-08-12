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

Only rows whose ``name`` matches one of the 22 pre-rename ``tenancy``
migrations (``0001_initial`` … ``0022_organization_week_start``, computed from
the on-disk migration graph, not hardcoded) are ever rewritten, from
``organizations`` to ``tenancy``. Running the fix against an already-fixed
database used to be a graceful no-op (no matching ``organizations``-labelled
rows left); as of the guard below, a second run never reaches that check at
all, because the first run's own success moves ``django_migrations`` out of
the state the guard requires -- see its "Non-zero" branch.

**Guard, corrected in the Phase 1c review.** Phase 1c installs
``organizations.apps.OrganizationsConfig`` -- the vinta-django-orgs package's
own Django app -- which is installed for the lifetime of every checkout from
that phase onward. The command's original guard (``CommandError`` whenever
``apps.is_installed("organizations")``) tested a **code** fact that became
permanently true the moment Phase 1c merged, which made this command refuse
unconditionally on `main` -- including against a genuinely pre-Phase-1a
database, the one case it exists to fix, and including on the `main`-tracking
phase branch this repository's `README.md` used to tell operators to run it
from (a remedy that evaporates the moment that branch is merged and deleted).

The guard now tests a **data** fact instead: ``SELECT COUNT(*) FROM
django_migrations WHERE app = 'tenancy'``.

* **Zero** -- no migration has ever been recorded under the ``tenancy``
  label, so this database predates Phase 1a (the label did not exist before
  it) and every ``organizations``-labelled row up to the pre-rename cutoff is
  ours. Proceed with the name-scoped rewrite below.
* **Non-zero** -- some ``tenancy``-labelled migration has been applied, which
  can only happen *after* Phase 1a landed. This database is not in the state
  this command exists to fix -- it was migrated fresh post-rename (there is
  nothing to fix), or it has already been fixed by a prior run of this very
  command (same conclusion), or Phase 1c has also run (in which case the
  ``organizations``-labelled rows belong to the package, not to this app's
  pre-rename history, and rewriting them would corrupt the package's applied
  state). Refuse outright (``CommandError``), in every one of those cases,
  rather than trying to tell them apart -- refusing is safe in all of them,
  and none of them can be told apart from ``django_migrations`` alone.

This is sound because ``migrate`` raises ``InconsistentMigrationHistory``
before applying anything, so a genuinely pre-rename database can never hold
a ``tenancy``-labelled row (nothing under that label could have been applied
to it), and a database that has run `migrate` even once since Phase 1a always
holds at least one. The name-scoped ``UPDATE`` (only the 22 pre-rename
``tenancy`` migration names, computed from the on-disk graph) stays as the
second guard -- belt and braces, not a replacement.

Usage, once, before ``migrate``, against any database created before Phase 1a
landed::

    python manage.py rename_organizations_migration_history
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection
from django.db.migrations.loader import MigrationLoader


#: The app label this app's migrations were recorded under before Phase 1a's
#: rename, and the label the vinta-django-orgs package's own app claims from
#: Phase 1c onward -- see the module docstring's "Guard" section for why that
#: makes a data-fact guard (not a code-fact one) the sound choice.
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
        "Refuses outright (CommandError, no mutation) unless django_migrations has "
        "zero rows labelled 'tenancy' -- the data fact that distinguishes a "
        "genuinely pre-Phase-1a, never-yet-migrated database from every other "
        "state, including a database migrated fresh under the current codebase and "
        "one where Phase 1c has installed the vinta-django-orgs package's own app. "
        "Safe (but not a silent no-op) to run against either of the latter two: "
        "the refusal is loud, not a corruption. See the module docstring."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many rows would be rewritten without changing anything.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM django_migrations WHERE app = %s",
                [_RENAMED_APP_LABEL],
            )
            (tenancy_row_count,) = cursor.fetchone()

        if tenancy_row_count > 0:
            raise CommandError(
                f"django_migrations already has {tenancy_row_count} row(s) labelled "
                f"'{_RENAMED_APP_LABEL}', so this database does not predate Phase 1a's "
                "rename -- either it was migrated fresh after the rename (nothing to "
                "fix), or the vinta-django-orgs package's own app has since recorded "
                "its migrations under 'organizations' (Phase 1c onward), in which case "
                "those rows belong to the package and rewriting them would corrupt its "
                "applied-migration state. This command is only for a database whose "
                "django_migrations has never recorded anything under 'tenancy' -- see "
                "the module docstring."
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
                        "django_migrations -- nothing to do (this database was never "
                        "migrated at all, under either label)."
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
