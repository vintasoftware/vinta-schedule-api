"""Backfill CalendarManagementToken.kind for pre-existing rows (step 2/3).

Second migration of the 0055 -> 0056 -> 0057 chain (see 0055's docstring for
why this is three separate migrations rather than three operations bundled
in one). Fills every ``kind IS NULL`` row -- every ``CalendarManagementToken``
that existed before 0055 added the column -- using the same heuristic
``CalendarManagementTokenQuerySet.booking_codes`` used to filter on before
this phase replaced it: ``minted_by_membership_user_id IS NOT NULL OR
minted_by_system_user_id IS NOT NULL`` -> ``BOOKING_CODE``, else
``MANAGEMENT_TOKEN``. See
``calendar_integration.migrations._0056_backfill_helpers`` for the batched,
idempotent, drain-loop implementation.

``atomic = False``: each batch's ``UPDATE`` (see the helper module) commits
independently rather than sitting inside one all-or-nothing transaction, so
a failure partway through this backfill leaves the batches it already wrote
committed rather than rolled back -- what makes the idempotency guarantee
below a real recovery path rather than a moot point.

Idempotent
----------
Both the row-selection query and the batch ``UPDATE`` in the helper carry
the ``kind IS NULL`` guard, so re-running the backfill (either by
re-invoking this migration's ``RunPython`` after a partial failure, or by
calling ``backfill_calendar_management_token_kind()`` directly, which is
importable precisely for this) only touches rows a prior run never reached.

Reverse
-------
``RunPython.noop`` -- deliberately NOT "clear every kind back to NULL".
``kind`` decides what ``revoke_token`` may touch; NULLing it back out here
would make revoke's own lookup (``booking_codes_for_organization``, which
filters ``kind=BOOKING_CODE``) stop matching every pre-existing booking
code, effectively un-revoking all of them for the duration between this
reverse and 0055's ``RemoveField`` reverse. It also buys nothing: a full
reverse past this point continues on to 0055's ``RemoveField``, which drops
the ``kind`` column outright regardless of what value this step leaves
behind.
"""

from django.db import migrations

from calendar_integration.migrations._0056_backfill_helpers import (
    backfill_calendar_management_token_kind,
)


def apply_backfill(apps, schema_editor) -> None:
    """Delegate to the importable, test-covered helper. See module docstring."""
    backfill_calendar_management_token_kind()


class Migration(migrations.Migration):
    """Backfill CalendarManagementToken.kind (data migration, 2/3)."""

    atomic = False

    dependencies = [
        ("calendar_integration", "0055_calendarmanagementtoken_kind"),
    ]

    operations = [
        migrations.RunPython(
            apply_backfill,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
