"""Importable backfill helpers for migration 0050.

Extracted from the migration so tests can call them directly without going
through the Django migration runner, mirroring
``_0034_backfill_helpers.py``.

What this backfills
-------------------
Imports used to bring every calendar an account could see in live: ``ACTIVE``
and syncing. They now bring in only the account's default calendar (Google's
``primary``, Outlook's ``isDefaultCalendar``) that way, and land every other
calendar ``UNLISTED`` with sync off until someone activates it. These helpers
apply the same shape to calendars imported before that change.

Scope of the forward pass -- ``PERSONAL``, externally-provided, currently
``ACTIVE`` calendars that no ``CalendarOwnership`` row marks as a default:

- ``RESOURCE`` / ``VIRTUAL`` / ``BUNDLE`` calendars are untouched: rooms and
  bundles are managed elsewhere and were never part of the per-account import.
- Already-``UNLISTED`` calendars are untouched. Unlisted-but-syncing is a state a
  user chose deliberately (hidden from booking, still synced for conflict
  detection); retroactively cutting that sync would break the thing they kept it
  for.
- ``INACTIVE`` (soft-deleted) calendars are untouched.

Both directions are idempotent and safe to re-run.

Reverse
-------
The forward pass stores each row's prior ``(visibility, sync_enabled)`` under
the ``unlisted_by_migration_0050`` key in ``Calendar.meta``, so the reverse pass
restores exactly what each row held rather than guessing a uniform "active and
syncing" -- a calendar that was ACTIVE with sync already switched off comes back
that way.
"""

from django.db import connection


BATCH_SIZE = 500

# Key under Calendar.meta holding the pre-backfill {visibility, sync_enabled}
# snapshot. Present only on rows this migration changed; removed on reverse.
BACKFILL_META_KEY = "unlisted_by_migration_0050"

CALENDAR_TABLE = "calendar_integration_calendar"
OWNERSHIP_TABLE = "calendar_integration_calendarownership"


def _max_calendar_id() -> int:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT COALESCE(MAX(id), 0) FROM {CALENDAR_TABLE}")  # noqa: S608
        (max_id,) = cursor.fetchone()
    return max_id


def _run_batched(sql: str) -> int:
    """Execute ``sql`` over PK ranges of the calendar table, returning rows changed.

    ``sql`` takes ``%(last_id)s`` / ``%(upper_id)s`` bounds. Batching keeps each
    statement's row locks short rather than holding the whole table for one
    UPDATE.
    """
    max_id = _max_calendar_id()
    if max_id == 0:
        return 0

    changed = 0
    last_id = 0
    while last_id < max_id:
        upper_id = last_id + BATCH_SIZE
        with connection.cursor() as cursor:
            cursor.execute(sql, {"last_id": last_id, "upper_id": upper_id})
            changed += cursor.rowcount or 0
        last_id = upper_id

    return changed


def unlist_non_default_calendars() -> int:
    """Unlist + stop syncing imported calendars that are nobody's default.

    Intentional cross-organization raw SQL: a data-migration backfill has to
    touch every row in every organization, so the org-scoped ORM manager is
    deliberately not used here.

    Returns the number of calendars changed.
    """
    sql = f"""
        UPDATE {CALENDAR_TABLE} AS c
        SET    visibility = 'unlisted',
               sync_enabled = FALSE,
               meta = jsonb_set(
                   COALESCE(c.meta, '{{}}'::jsonb),
                   '{{{BACKFILL_META_KEY}}}',
                   jsonb_build_object(
                       'visibility', c.visibility,
                       'sync_enabled', c.sync_enabled
                   ),
                   TRUE
               )
        WHERE  c.id > %(last_id)s
        AND    c.id <= %(upper_id)s
        AND    c.calendar_type = 'personal'
        AND    c.provider <> 'internal'
        AND    c.visibility = 'active'
        AND    NOT jsonb_exists(COALESCE(c.meta, '{{}}'::jsonb), '{BACKFILL_META_KEY}')
        AND    NOT EXISTS (
                   SELECT 1
                   FROM   {OWNERSHIP_TABLE} AS o
                   WHERE  o.calendar_fk_id = c.id
                   AND    o.organization_id = c.organization_id
                   AND    o.is_default
               )
    """  # noqa: S608
    return _run_batched(sql)


def restore_unlisted_calendars() -> int:
    """Restore the visibility / sync_enabled each backfilled row held before.

    Reads the snapshot ``unlist_non_default_calendars`` wrote under
    ``Calendar.meta`` and drops the key, so a second reverse pass is a no-op.

    Returns the number of calendars restored.
    """
    sql = f"""
        UPDATE {CALENDAR_TABLE} AS c
        SET    visibility = c.meta -> '{BACKFILL_META_KEY}' ->> 'visibility',
               sync_enabled = (c.meta -> '{BACKFILL_META_KEY}' ->> 'sync_enabled')::boolean,
               meta = c.meta - '{BACKFILL_META_KEY}'
        WHERE  c.id > %(last_id)s
        AND    c.id <= %(upper_id)s
        AND    jsonb_exists(COALESCE(c.meta, '{{}}'::jsonb), '{BACKFILL_META_KEY}')
    """  # noqa: S608
    return _run_batched(sql)
