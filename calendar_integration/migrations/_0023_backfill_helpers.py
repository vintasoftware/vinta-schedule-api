"""Importable backfill helpers for CalendarOwnership.membership_user_id.

These functions are extracted from migration 0023 so that tests can call them
directly without going through the Django migration runner.  The migration
itself delegates to these functions.

Both functions are safe to call in any order and idempotent.
"""

from django.db import connection


BATCH_SIZE = 500


def backfill_membership_user_id_sql() -> None:
    """Batch-UPDATE membership_user_id = user_id where a matching membership exists.

    Iterates CalendarOwnership PKs in BATCH_SIZE chunks.  Rows that already have
    membership_user_id set (non-NULL) are skipped by the WHERE guard, making the
    function idempotent.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COALESCE(MAX(id), 0) FROM calendar_integration_calendarownership"
        )
        (max_id,) = cursor.fetchone()

    if max_id == 0:
        return

    last_id = 0
    while last_id < max_id:
        upper_id = last_id + BATCH_SIZE
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE calendar_integration_calendarownership AS co
                SET    membership_user_id = co.user_id
                WHERE  co.membership_user_id IS NULL
                AND    co.id > %s
                AND    co.id <= %s
                AND    EXISTS (
                           SELECT 1
                           FROM   organizations_organizationmembership m
                           WHERE  m.user_id = co.user_id
                           AND    m.organization_id = co.organization_id
                       )
                """,
                [last_id, upper_id],
            )
        last_id = upper_id


def collect_orphans() -> list[tuple]:
    """Return rows whose (user_id, organization_id) has no OrganizationMembership.

    Returns a list of (ownership_id, user_id, organization_id, calendar_id) tuples.
    Only rows that still have membership_user_id IS NULL are considered; this
    matches rows that the backfill could not populate because no membership exists.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT co.id, co.user_id, co.organization_id, co.calendar_fk_id
            FROM   calendar_integration_calendarownership co
            WHERE  co.membership_user_id IS NULL
            AND    NOT EXISTS (
                       SELECT 1
                       FROM   organizations_organizationmembership m
                       WHERE  m.user_id = co.user_id
                       AND    m.organization_id = co.organization_id
                   )
            ORDER BY co.id
            """
        )
        return cursor.fetchall()
