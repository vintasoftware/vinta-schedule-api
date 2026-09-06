"""Backfill CalendarGroup.duration for pre-existing rows.

``CalendarPermissionService`` fails closed on a null ``duration`` (see
0051's ``CalendarGroup.duration`` help_text and
``CalendarPermissionService._group_duration_pin_satisfied``): a group with
``accepts_public_scheduling=True`` and no duration set refuses every
booking rather than accepting any length. That is the intended failure
direction for groups going forward, but it means every group that existed
before this migration -- public groups above all -- would start refusing
bookings the moment this stack deploys, with no way to set duration first
short of a manual, easy-to-miss pre-deploy audit.

This migration removes that manual step: it sets
``duration = timedelta(minutes=30)`` on EVERY pre-existing ``CalendarGroup``
row with a NULL ``duration`` -- public and private alike, not just the
public groups the fail-closed rule strictly requires. See
``calendar_integration.migrations._0058_backfill_helpers``'s module
docstring ("Why 30 minutes, for every group") for the full reasoning; in
short, this is a deliberate, informed choice, not an oversight: it ALSO
pins every pre-existing PRIVATE group's coded bookings to exactly 30
minutes, where before this migration they were unconstrained by any
group-level duration at all. An organization that books other lengths
through a private group must set that group's ``duration`` explicitly
after this deploys.

``atomic = False``: each batch's ``UPDATE`` (see the helper module) commits
independently rather than sitting inside one all-or-nothing transaction, so
a failure partway through this backfill leaves the batches it already wrote
committed rather than rolled back -- what makes the idempotency guarantee
below a real recovery path rather than a moot point.

Idempotent
----------
Both the row-selection subquery and the batch ``UPDATE`` in the helper
carry the ``duration IS NULL`` guard, so re-running the backfill (either by
re-invoking this migration's ``RunPython`` after a partial failure, or by
calling ``backfill_calendargroup_duration()`` directly, which is importable
precisely for this) only touches rows a prior run never reached. A group
that already has a duration -- set by a human, by ``CalendarGroupService``,
or by a prior partial run of this same backfill -- is never overwritten.

Reverse
-------
``RunPython.noop`` -- deliberately NOT "clear every duration back to
NULL". Two reasons, both fatal to a real reverse:

1. NULLing every group's duration back out would immediately re-break the
   exact fail-closed rule this migration exists to satisfy: every public
   group would start refusing bookings again, and every private group's
   coded bookings would silently unpin from 30 minutes to "any length" --
   a security regression that fails open, the same shape 0051's own
   docstring warns about for its own reverse.
2. It buys nothing anyway. A full reverse past this point continues on to
   0051's reverse, which drops the ``duration`` column outright regardless
   of what value this step leaves behind -- there is no schema state where
   "duration column present, but re-nulled" is ever actually observed by
   anything.
"""

from django.db import migrations

from calendar_integration.migrations._0058_backfill_helpers import (
    backfill_calendargroup_duration,
)


def apply_backfill(apps, schema_editor) -> None:
    """Delegate to the importable, test-covered helper. See module docstring."""
    backfill_calendargroup_duration()


class Migration(migrations.Migration):
    """Backfill CalendarGroup.duration (data migration)."""

    atomic = False

    dependencies = [
        ("calendar_integration", "0057_calendarmanagementtoken_kind_not_null"),
    ]

    operations = [
        migrations.RunPython(
            apply_backfill,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
