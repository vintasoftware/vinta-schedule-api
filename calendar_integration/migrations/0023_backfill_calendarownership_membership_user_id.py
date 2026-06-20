"""Backfill CalendarOwnership.membership_user_id from user_id where an
OrganizationMembership(user_id, organization_id) pair exists.

Strategy
--------
For each CalendarOwnership row whose membership_user_id is still NULL and whose
(user_id, organization_id) has a matching OrganizationMembership, we copy
user_id → membership_user_id.  The SQL form (executed in BATCH_SIZE PK-range
chunks) is:

    UPDATE calendar_integration_calendarownership AS co
    SET    membership_user_id = co.user_id
    WHERE  co.membership_user_id IS NULL
    AND    co.id > %(last_id)s
    AND    co.id <= %(upper_id)s
    AND    EXISTS (
               SELECT 1
               FROM   organizations_organizationmembership m
               WHERE  m.user_id = co.user_id
               AND    m.organization_id = co.organization_id
           )

Re-running this migration is safe (idempotent): already-populated rows are
skipped by the ``membership_user_id IS NULL`` guard.

Orphans
-------
Rows whose (user_id, organization_id) has no matching OrganizationMembership
are left with membership_user_id = NULL.  A CSV report is written to
.vinta-ai-workflows/one-off-runs/ (per .vinta-ai-workflows.yaml) with columns:
    ownership_id, user_id, organization_id, calendar_id

A WARNING is also emitted with the orphan count and the file path.

Reverse
-------
Sets membership_user_id = NULL for all rows.  This is a clean roll-back to the
state before the backfill ran.

Implementation
--------------
The core SQL is extracted to
``calendar_integration/migrations/_0023_backfill_helpers.py`` so tests can
call ``backfill_membership_user_id_sql()`` and ``collect_orphans()`` directly
without running the migration runner.
"""

import csv
import logging
import os
from datetime import UTC, datetime

from django.db import connection, migrations

from calendar_integration.migrations._0023_backfill_helpers import (
    backfill_membership_user_id_sql,
    collect_orphans,
)


logger = logging.getLogger(__name__)

LOG_DIR = ".vinta-ai-workflows/one-off-runs"


def backfill_membership_user_id(apps, schema_editor):
    """Populate membership_user_id for CalendarOwnership rows with a matching membership."""
    backfill_membership_user_id_sql()

    orphans = collect_orphans()
    if not orphans:
        return

    # Emit CSV report.
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    csv_filename = f"calendarownership_orphans_{ts}.csv"
    csv_path = os.path.join(LOG_DIR, csv_filename)

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["ownership_id", "user_id", "organization_id", "calendar_id"])
            writer.writerows(orphans)
        logger.warning(
            "CalendarOwnership backfill: %d orphan row(s) whose (user_id, organization_id) "
            "has no OrganizationMembership — left with membership_user_id=NULL.  "
            "CSV report written to %s",
            len(orphans),
            csv_path,
        )
    except OSError:
        # File write failed (e.g. read-only filesystem in CI).  Fall back to
        # logging each orphan row individually.
        logger.warning(
            "CalendarOwnership backfill: %d orphan row(s) — could not write CSV to %s.  "
            "Logging individually below.",
            len(orphans),
            csv_path,
        )
        for ownership_id, user_id, organization_id, calendar_id in orphans:
            logger.warning(
                "  orphan CalendarOwnership id=%s user_id=%s "
                "organization_id=%s calendar_id=%s",
                ownership_id,
                user_id,
                organization_id,
                calendar_id,
            )


def reverse_backfill_membership_user_id(apps, schema_editor):
    """Roll back: clear membership_user_id on all CalendarOwnership rows."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE calendar_integration_calendarownership SET membership_user_id = NULL"
        )


class Migration(migrations.Migration):
    """Backfill CalendarOwnership.membership_user_id (Phase 1 data migration)."""

    atomic = False

    dependencies = [
        ("calendar_integration", "0022_calendarownership_membership_and_more"),
        ("organizations", "0011_organizationbranding"),
    ]

    operations = [
        migrations.RunPython(
            backfill_membership_user_id,
            reverse_code=reverse_backfill_membership_user_id,
        ),
    ]
