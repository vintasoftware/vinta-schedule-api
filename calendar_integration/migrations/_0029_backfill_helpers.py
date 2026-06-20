"""Importable backfill helpers for EventAttendance.membership_user_id.

These functions are extracted from migration 0029 so that tests can call them
directly without going through the Django migration runner.  The migration
itself delegates to these functions.

Both functions are safe to call in any order and idempotent.

Null-user rows
--------------
``EventAttendance.user_id`` is currently ``NOT NULL`` at the DB level, so no
null-user rows can exist today; the ``AND ea.user_id IS NOT NULL`` guard in the
backfill and orphan queries is defensive / forward-compatible (the sync service
references ``user=None`` in code, a latent inconsistency this phase does not
change). Were such rows to exist, they would legitimately have no membership and
would NOT be reported as orphans.

Orphans
-------
Only rows where ``user_id IS NOT NULL`` AND no matching ``OrganizationMembership``
exists for ``(user_id, organization_id)`` are considered orphans.  These remain
with ``membership_user_id = NULL`` and are reported via the caller.
"""

from django.db import connection


BATCH_SIZE = 500


def backfill_membership_user_id_sql() -> None:
    """Batch-UPDATE membership_user_id = user_id where a matching membership exists.

    Iterates EventAttendance PKs in BATCH_SIZE chunks.  Rows that already have
    membership_user_id set (non-NULL) are skipped by the WHERE guard, making the
    function idempotent.  Rows where user_id IS NULL are also skipped — they
    legitimately have no membership.

    Intentional cross-organization raw SQL: this is a one-off data-migration
    backfill that must touch all rows across all organizations; do not replace
    with the org-scoped ORM manager.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT COALESCE(MAX(id), 0) FROM calendar_integration_eventattendance")
        (max_id,) = cursor.fetchone()

    if max_id == 0:
        return

    last_id = 0
    while last_id < max_id:
        upper_id = last_id + BATCH_SIZE
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE calendar_integration_eventattendance AS ea
                SET    membership_user_id = ea.user_id
                WHERE  ea.membership_user_id IS NULL
                AND    ea.user_id IS NOT NULL
                AND    ea.id > %s
                AND    ea.id <= %s
                AND    EXISTS (
                           SELECT 1
                           FROM   organizations_organizationmembership m
                           WHERE  m.user_id = ea.user_id
                           AND    m.organization_id = ea.organization_id
                       )
                """,
                [last_id, upper_id],
            )
        last_id = upper_id


def collect_orphans() -> list[tuple]:
    """Return rows whose (user_id, organization_id) has no OrganizationMembership.

    Excludes rows where user_id IS NULL — those are legitimate null-user
    attendances (synced attendees), not orphans.

    Returns a list of (attendance_id, user_id, organization_id, event_id) tuples.
    Only rows that still have membership_user_id IS NULL AND user_id IS NOT NULL
    are considered; this matches rows that the backfill could not populate because
    no membership exists.

    Intentional cross-organization raw SQL: this is a one-off data-migration
    backfill that must inspect all rows across all organizations; do not replace
    with the org-scoped ORM manager.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT ea.id, ea.user_id, ea.organization_id, ea.event_fk_id
            FROM   calendar_integration_eventattendance ea
            WHERE  ea.membership_user_id IS NULL
            AND    ea.user_id IS NOT NULL
            AND    NOT EXISTS (
                       SELECT 1
                       FROM   organizations_organizationmembership m
                       WHERE  m.user_id = ea.user_id
                       AND    m.organization_id = ea.organization_id
                   )
            ORDER BY ea.id
            """
        )
        return cursor.fetchall()
