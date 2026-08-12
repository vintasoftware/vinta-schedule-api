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
such a database re-attempts all 22 migrations from scratch and fails with
``DuplicateTable`` on the very first ``CreateModel`` (``organizations_
organization`` already exists).

This is the fix, and it must run **before** ``migrate`` — not as a Django
migration itself. A migration cannot fix its own app's identity: the loader
decides what is "pending" by reading ``django_migrations`` *before* it runs
anything, so a `RunPython` step inside the ``tenancy`` graph would never even
get a chance to execute (the loader would already be trying, and failing, to
re-run ``0001_initial`` first).

Idempotent: only ``organizations``-labelled rows are touched. Re-running
after the rename (no matching rows left) is a no-op. Since this app is the
only one that has ever used the ``organizations`` label (the
vinta-django-orgs package's own app is not installed until Phase 1c, and
would use the same label if it were — see the plan's "App-label collision"
Guiding Decision), a blanket rewrite is safe: nothing else in
``django_migrations`` can legitimately carry that label.

Usage, once, before ``migrate``, against any database created before this
branch::

    python manage.py rename_organizations_migration_history
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import connection


class Command(BaseCommand):
    """Rewrite ``django_migrations.app`` from ``organizations`` to ``tenancy``."""

    help = (  # noqa: A003
        "Rewrites django_migrations rows keyed under the pre-rename 'organizations' "
        "app label onto 'tenancy', so `migrate` recognizes them as already applied "
        "instead of re-attempting them and hitting DuplicateTable. Idempotent -- "
        "safe to run against a database that has already been fixed or that was "
        "never seeded pre-rename."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many rows would be rewritten without changing anything.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        dry_run: bool = options["dry_run"]

        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM django_migrations WHERE app = 'organizations'")
            (pending_count,) = cursor.fetchone()

            if pending_count == 0:
                self.stdout.write(
                    self.style.SUCCESS(
                        "No 'organizations'-labelled rows found in django_migrations -- "
                        "nothing to do (already fixed, or this database was only ever "
                        "migrated post-rename)."
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
                "UPDATE django_migrations SET app = 'tenancy' WHERE app = 'organizations'"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Rewrote {pending_count} django_migrations row(s) from 'organizations' "
                "to 'tenancy'. `migrate` can now be run safely."
            )
        )
