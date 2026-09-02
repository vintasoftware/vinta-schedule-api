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

    Iterates PK ranges in ``BATCH_SIZE`` chunks (never ``.all()``), assigning
    each NULL row a freshly generated, collision-checked slug with a single
    parameterized ``UPDATE`` per row -- one random value per row cannot be
    expressed as a single batch ``UPDATE ... WHERE id IN (...)`` the way
    ``_0034_backfill_helpers.py``'s shared-value backfill can, since every row
    needs its own distinct value.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT COALESCE(MAX(id), 0) FROM calendar_integration_calendargroup")
        (max_id,) = cursor.fetchone()

    if max_id == 0:
        return

    existing_slugs = _load_existing_slugs()

    last_id = 0
    while last_id < max_id:
        upper_id = last_id + BATCH_SIZE
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM calendar_integration_calendargroup "
                "WHERE public_booking_slug IS NULL AND id > %s AND id <= %s "
                "ORDER BY id",
                [last_id, upper_id],
            )
            batch_ids = [row[0] for row in cursor.fetchall()]

        for group_id in batch_ids:
            slug = _generate_unused_slug(existing_slugs)
            existing_slugs.add(slug)
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE calendar_integration_calendargroup SET public_booking_slug = %s "
                    "WHERE id = %s AND public_booking_slug IS NULL",
                    [slug, group_id],
                )

        last_id = upper_id


def reverse_backfill_public_booking_slugs() -> None:
    """Roll back: clear ``public_booking_slug`` on every row.

    Safe only while the column is still nullable at the DB level -- 0054
    (which makes the column ``NOT NULL``) reverses before this migration's
    reverse ever runs, per normal migration-graph ordering, so by the time
    this runs during a reverse the column is nullable again.
    """
    with connection.cursor() as cursor:
        cursor.execute("UPDATE calendar_integration_calendargroup SET public_booking_slug = NULL")
