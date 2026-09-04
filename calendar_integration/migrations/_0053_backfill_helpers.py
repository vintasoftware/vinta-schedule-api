"""Importable backfill helpers for ``CalendarGroup.public_booking_slug``.

Extracted from migration 0053 so tests can call
``backfill_public_booking_slugs()`` directly without going through the
migration runner -- same pattern as
``calendar_integration/migrations/_0034_backfill_helpers.py``.

Idempotency
-----------
Only rows with ``public_booking_slug IS NULL`` are ever touched (both the
``SELECT`` that finds candidate rows and the ``UPDATE`` that writes them carry
the ``IS NULL`` guard), so re-running this function after a partial failure
completes the remaining rows without rewriting any row a prior run already
filled in.

Collision checking
-------------------
``secrets.token_urlsafe(16)`` gives ~128 bits of entropy, but uniqueness here
is a hard, global (cross-organization) DB constraint the migration adds right
after this backfill runs, so the generator does not merely trust the odds:
every existing non-NULL slug in the table is loaded into an in-memory set up
front, and every newly generated candidate is checked against that set
(growing it as rows are filled) before being written. A collision regenerates
the candidate rather than being written.

Cross-organization raw SQL
---------------------------
``public_booking_slug`` uniqueness is GLOBAL, not organization-scoped (the
codeless booking route carries no organization in its path), so this backfill
must see every ``CalendarGroup`` row across every organization at once to
collision-check correctly. It uses the raw cursor directly rather than the
org-scoped ORM manager, matching the documented exception in
``_0034_backfill_helpers.py`` for the same reason. Table name is a literal in
every statement below (not interpolated from a variable) so ruff's ``S608``
SQL-injection heuristic does not flag it -- there is no untrusted input in
any of these statements, only ``%s``-parameterized values.

Drain loop, not a snapshotted ``MAX(id)`` bound
-------------------------------------------------
``manage.py migrate`` runs inside Render's build step while the *previous*
deploy's pods are still serving traffic, so old code can ``INSERT`` new
``CalendarGroup`` rows for the whole duration of this backfill. A bound
computed once up front (``SELECT MAX(id)`` before the loop starts) would
never see a row inserted after that snapshot -- its id exceeds the bound, the
loop's ``id <= upper_id`` condition never reaches it, and it would be left
with ``public_booking_slug IS NULL`` for 0054's ``SET NOT NULL`` to fail on.
Instead, ``backfill_public_booking_slugs`` repeatedly re-queries
``WHERE public_booking_slug IS NULL ORDER BY id LIMIT <batch>`` until a query
returns nothing -- a true drain, not a fixed range -- so a row inserted mid-
backfill (by old code, or by anything else) is picked up by a later
iteration rather than missed. The batching and the collision check are
unchanged; only the loop's stopping condition is.

No reverse helper
------------------
This module intentionally has no ``reverse_backfill_public_booking_slugs``
counterpart. 0053's migration reverse is ``RunPython.noop`` -- see that
migration's docstring -- because NULLing every slug back out would
permanently invalidate every public booking link already handed to a
patient (a fresh ``0053`` re-apply mints *new*, unrelated slugs, it cannot
restore the old ones), and buys nothing anyway: a full reverse continues on
to ``0052``'s ``RemoveField``, which drops the column regardless of what
this step leaves in it.
"""

import secrets

from django.db import connection


BATCH_SIZE = 500


def _load_existing_slugs() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT public_booking_slug FROM calendar_integration_calendargroup "
            "WHERE public_booking_slug IS NOT NULL"
        )
        return {row[0] for row in cursor.fetchall()}


def _generate_unused_slug(existing: set[str]) -> str:
    slug = secrets.token_urlsafe(16)
    while slug in existing:
        slug = secrets.token_urlsafe(16)
    return slug


def backfill_public_booking_slugs() -> None:
    """Fill ``public_booking_slug`` for every ``CalendarGroup`` row that lacks one.

    Drains ``BATCH_SIZE``-sized batches of NULL-slug rows (never ``.all()``,
    never a snapshotted id bound -- see the module docstring's "Drain loop"
    section for why), assigning each row a freshly generated,
    collision-checked slug with a single parameterized ``UPDATE`` per row --
    one random value per row cannot be expressed as a single batch
    ``UPDATE ... WHERE id IN (...)`` the way ``_0034_backfill_helpers.py``'s
    shared-value backfill can, since every row needs its own distinct value.
    Terminates when a drain query returns no rows, at which point every row
    that was ``NULL`` at any point during the loop -- including one inserted
    concurrently, by old code, mid-backfill -- has been filled.
    """
    existing_slugs = _load_existing_slugs()

    while True:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM calendar_integration_calendargroup "
                "WHERE public_booking_slug IS NULL ORDER BY id LIMIT %s",
                [BATCH_SIZE],
            )
            batch_ids = [row[0] for row in cursor.fetchall()]

        if not batch_ids:
            break

        for group_id in batch_ids:
            slug = _generate_unused_slug(existing_slugs)
            existing_slugs.add(slug)
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE calendar_integration_calendargroup SET public_booking_slug = %s "
                    "WHERE id = %s AND public_booking_slug IS NULL",
                    [slug, group_id],
                )
