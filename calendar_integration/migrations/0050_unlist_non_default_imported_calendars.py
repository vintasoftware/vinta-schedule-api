"""Unlist and stop syncing imported calendars that are nobody's default.

Imports used to bring in every calendar an account could see as ``ACTIVE`` and
syncing. They now activate only the account's default calendar (Google's
``primary``, Outlook's ``isDefaultCalendar``) and land the rest ``UNLISTED``
with ``sync_enabled=False`` until the owner or an org admin activates them
through ``PATCH /calendar/{id}/``. This applies that shape to calendars that
were imported before the change, so existing organizations stop syncing the
holidays / birthdays / subscribed-team calendars nobody schedules against.

Scope, idempotency, and the meta snapshot the reverse pass restores from are
documented in ``calendar_integration/migrations/_0050_backfill_helpers.py``.

Data loss: none. No row is deleted and no event is touched -- a calendar this
migration unlists keeps every event already synced onto it, and re-activating it
through the API resumes syncing (and requests a fresh sync).
"""

import logging

from django.db import migrations

from calendar_integration.migrations._0050_backfill_helpers import (
    restore_unlisted_calendars,
    unlist_non_default_calendars,
)


logger = logging.getLogger(__name__)


def unlist_non_default(apps, schema_editor):
    """Unlist + disable sync on personal calendars that no ownership marks default."""
    changed = unlist_non_default_calendars()
    logger.info("Unlisted %d non-default imported calendar(s) and disabled their sync.", changed)


def restore_non_default(apps, schema_editor):
    """Roll back: restore each backfilled calendar's prior visibility / sync_enabled."""
    restored = restore_unlisted_calendars()
    logger.info("Restored %d calendar(s) unlisted by the 0050 backfill.", restored)


class Migration(migrations.Migration):
    """Unlist non-default imported calendars (data migration)."""

    atomic = False

    dependencies = [
        ("calendar_integration", "0049_naive_tz_unaware_datetime_fields"),
    ]

    operations = [
        migrations.RunPython(
            unlist_non_default,
            reverse_code=restore_non_default,
        ),
    ]
