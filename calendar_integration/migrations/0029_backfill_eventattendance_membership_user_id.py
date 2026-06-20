"""Backfill EventAttendance.membership_user_id from user_id where an
OrganizationMembership(user_id, organization_id) pair exists.

Strategy
--------
For each EventAttendance row whose membership_user_id is still NULL, whose
user_id IS NOT NULL, and whose (user_id, organization_id) has a matching
OrganizationMembership, we copy user_id → membership_user_id.  The SQL form
(executed in BATCH_SIZE PK-range chunks) is:

    UPDATE calendar_integration_eventattendance AS ea
    SET    membership_user_id = ea.user_id
    WHERE  ea.membership_user_id IS NULL
    AND    ea.user_id IS NOT NULL
    AND    ea.id > %(last_id)s
    AND    ea.id <= %(upper_id)s
    AND    EXISTS (
               SELECT 1
               FROM   organizations_organizationmembership m
               WHERE  m.user_id = ea.user_id
               AND    m.organization_id = ea.organization_id
           )

Re-running this migration is safe (idempotent): already-populated rows are
skipped by the ``membership_user_id IS NULL`` guard.

Null-user rows
--------------
``EventAttendance`` rows where ``user_id IS NULL`` represent synced/external
attendees without a local user account.  These are NOT orphans — they
legitimately have no membership — and are left with ``membership_user_id = NULL``
without any report or warning.

Orphans
-------
Rows whose user_id IS NOT NULL but whose (user_id, organization_id) has no
matching OrganizationMembership are left with membership_user_id = NULL.  A CSV
report is written to .vinta-ai-workflows/one-off-runs/ with columns:
    attendance_id, user_id, organization_id, event_id

A WARNING is also emitted with the orphan count and the file path.

Reverse
-------
Sets membership_user_id = NULL for all rows.  This is a clean roll-back to the
state before the backfill ran.

Implementation
--------------
The core SQL is extracted to
``calendar_integration/migrations/_0029_backfill_helpers.py`` so tests can
call ``backfill_membership_user_id_sql()`` and ``collect_orphans()`` directly
without running the migration runner.
"""

import csv
import logging
import os
from datetime import UTC, datetime

from django.db import connection, migrations

from calendar_integration.migrations._0029_backfill_helpers import (
    backfill_membership_user_id_sql,
    collect_orphans,
)


logger = logging.getLogger(__name__)

LOG_DIR = ".vinta-ai-workflows/one-off-runs"


def backfill_membership_user_id(apps, schema_editor):
    """Populate membership_user_id for EventAttendance rows with a matching membership."""
    backfill_membership_user_id_sql()

    orphans = collect_orphans()
    if not orphans:
        return

    # Emit CSV report.
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    csv_filename = f"eventattendance_orphans_{ts}.csv"
    csv_path = os.path.join(LOG_DIR, csv_filename)

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["attendance_id", "user_id", "organization_id", "event_id"])
            writer.writerows(orphans)
        logger.warning(
            "EventAttendance backfill: %d orphan row(s) whose (user_id, organization_id) "
            "has no OrganizationMembership — left with membership_user_id=NULL.  "
            "CSV report written to %s",
            len(orphans),
            csv_path,
        )
    except OSError:
        # File write failed (e.g. read-only filesystem in CI).  Fall back to
        # logging each orphan row individually.
        logger.warning(
            "EventAttendance backfill: %d orphan row(s) — could not write CSV to %s.  "
            "Logging individually below.",
            len(orphans),
            csv_path,
        )
        for attendance_id, user_id, organization_id, event_id in orphans:
            logger.warning(
                "  orphan EventAttendance id=%s user_id=%s organization_id=%s event_id=%s",
                attendance_id,
                user_id,
                organization_id,
                event_id,
            )


def reverse_backfill_membership_user_id(apps, schema_editor):
    """Roll back: clear membership_user_id on all EventAttendance rows."""
    with connection.cursor() as cursor:
        cursor.execute("UPDATE calendar_integration_eventattendance SET membership_user_id = NULL")


class Migration(migrations.Migration):
    """Backfill EventAttendance.membership_user_id (Phase 3 data migration)."""

    atomic = False

    dependencies = [
        ("calendar_integration", "0028_eventattendance_membership_and_more"),
        ("organizations", "0011_organizationbranding"),
    ]

    operations = [
        migrations.RunPython(
            backfill_membership_user_id,
            reverse_code=reverse_backfill_membership_user_id,
        ),
    ]
