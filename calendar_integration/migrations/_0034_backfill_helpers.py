"""Importable backfill helpers for CalendarManagementToken.membership_user_id.

These functions are extracted from migration 0034 so that tests can call them
directly without going through the Django migration runner.  The migration
itself delegates to these functions.

Both functions are safe to call in any order and idempotent.

Null-user rows
--------------
``CalendarManagementToken.user_id`` is genuinely nullable: a token may belong
to an ``external_attendee`` instead of a registered user.  Such rows
legitimately have no membership and are NOT reported as orphans.  The
``AND t.user_id IS NOT NULL`` guard in all queries below ensures they are
skipped unconditionally.

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

    Iterates CalendarManagementToken PKs in BATCH_SIZE chunks.  Rows that
    already have membership_user_id set (non-NULL) are skipped by the WHERE
    guard, making the function idempotent.  Rows where user_id IS NULL
    (external-attendee tokens) are also skipped — they legitimately have no
    membership and are NOT orphans.

    Intentional cross-organization raw SQL: this is a one-off data-migration
    backfill that must touch all rows across all organizations; do not replace
    with the org-scoped ORM manager.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COALESCE(MAX(id), 0) FROM calendar_integration_calendarmanagementtoken"
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
                UPDATE calendar_integration_calendarmanagementtoken AS t
                SET    membership_user_id = t.user_id
                WHERE  t.membership_user_id IS NULL
                AND    t.user_id IS NOT NULL
                AND    t.id > %s
                AND    t.id <= %s
                AND    EXISTS (
                           SELECT 1
                           FROM   organizations_organizationmembership m
                           WHERE  m.user_id = t.user_id
                           AND    m.organization_id = t.organization_id
                       )
                """,
                [last_id, upper_id],
            )
        last_id = upper_id


def collect_orphans() -> list[tuple]:
    """Return rows whose (user_id, organization_id) has no OrganizationMembership.

    Excludes rows where user_id IS NULL — those are legitimate external-attendee
    tokens (not orphans).

    Returns a list of (token_id, user_id, organization_id) tuples.
    Only rows that still have membership_user_id IS NULL AND user_id IS NOT NULL
    are considered; this matches rows the backfill could not populate because
    no membership exists.

    Intentional cross-organization raw SQL: this is a one-off data-migration
    backfill that must inspect all rows across all organizations; do not replace
    with the org-scoped ORM manager.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT t.id, t.user_id, t.organization_id
            FROM   calendar_integration_calendarmanagementtoken t
            WHERE  t.membership_user_id IS NULL
            AND    t.user_id IS NOT NULL
            AND    NOT EXISTS (
                       SELECT 1
                       FROM   organizations_organizationmembership m
                       WHERE  m.user_id = t.user_id
                       AND    m.organization_id = t.organization_id
                   )
            ORDER BY t.id
            """
        )
        return cursor.fetchall()
