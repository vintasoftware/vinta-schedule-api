"""Backfill CalendarGroup.public_booking_slug for pre-existing rows (step 2/3).

Second migration of the 0052 -> 0053 -> 0054 chain (see 0052's docstring for
why this is three separate migrations rather than three operations bundled
in one). Fills every ``public_booking_slug IS NULL`` row -- every
``CalendarGroup`` that existed before 0052 added the column -- with a
freshly generated, distinct, collision-checked slug, via
``calendar_integration.migrations._0053_backfill_helpers``.

``atomic = False``: each row's ``UPDATE`` (see the helper module) commits
independently rather than sitting inside one all-or-nothing transaction, so
a failure partway through this backfill leaves the rows it already wrote
committed rather than rolled back. That is what makes the idempotency
guarantee below a real recovery path rather than a moot point -- a wrapping
transaction would undo every prior batch on any failure, leaving nothing for
a rerun to skip.

Idempotent
----------
Both the row-selection query and the per-row ``UPDATE`` in the helper carry
the ``public_booking_slug IS NULL`` guard, so re-running the backfill (either
by re-invoking this migration's ``RunPython`` after a partial failure via a
fake/forced re-run, or -- the normal case -- by calling
``backfill_public_booking_slugs()`` directly, which is importable precisely
for this) only touches rows a prior run never reached. It does not
regenerate or overwrite any slug a prior run already wrote.

Collision-checked, not merely trusted
--------------------------------------
``secrets.token_urlsafe(16)`` gives ~128 bits of entropy, but 0054 right
after this migration turns ``public_booking_slug`` into a hard, global
(cross-organization) unique constraint, so this backfill does not merely
trust the odds: every existing non-NULL slug is loaded into an in-memory set
up front and grown as rows are filled, and every candidate is checked
against that set (regenerating on a hit) before being written. See the
helper module's own docstring for the full detail.

Reverse
-------
``RunPython.noop`` -- deliberately NOT "clear every slug back to NULL".
``public_booking_slug`` is handed out in public, unauthenticated booking
links; NULLing every row here would permanently invalidate every link
already distributed to a patient, and a subsequent forward re-apply of this
migration mints *brand-new*, unrelated slugs rather than restoring the old
ones -- the data loss would be unrecoverable. It also buys nothing: a full
reverse past this point continues on to ``0052``'s ``RemoveField``, which
drops the ``public_booking_slug`` column outright regardless of what value
this step leaves behind. See
``calendar_integration/migrations/_0053_backfill_helpers.py``'s "No reverse
helper" section for the fuller version of this reasoning.
"""

from django.db import migrations

from calendar_integration.migrations._0053_backfill_helpers import (
    backfill_public_booking_slugs,
)


def apply_backfill(apps, schema_editor) -> None:
    """Delegate to the importable, test-covered helper. See module docstring."""
    backfill_public_booking_slugs()


class Migration(migrations.Migration):
    """Backfill CalendarGroup.public_booking_slug (data migration, 2/3)."""

    atomic = False

    dependencies = [
        ("calendar_integration", "0052_calendargroup_public_booking_slug"),
    ]

    operations = [
        migrations.RunPython(
            apply_backfill,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
