"""Importable backfill helpers for ``CalendarGroup.duration``.

Extracted from migration 0058 so tests can call
``backfill_calendargroup_duration()`` directly without going through the
migration runner -- same pattern as
``calendar_integration/migrations/_0056_backfill_helpers.py``.

Why 30 minutes, for every group -- public AND private
--------------------------------------------------------
``CalendarPermissionService`` fails closed (see 0051's
``CalendarGroup.duration`` help_text and
``CalendarPermissionService._group_duration_pin_satisfied``): a group with
``accepts_public_scheduling=True`` and a null ``duration`` refuses every
booking rather than accepting any length. That is deliberate for groups
created going forward -- ``CalendarGroupService.create_group`` /
``update_group`` require ``duration`` be set whenever
``accepts_public_scheduling=True`` -- but it means every *pre-existing*
public group would break on deploy, with no in-app way to fix it before
traffic starts hitting the fail-closed check.

This backfill closes that gap by giving EVERY pre-existing group with a
NULL ``duration`` -- public and private alike -- ``timedelta(minutes=30)``,
not just the public ones the fail-closed rule strictly requires. This is a
deliberate, explicit choice (see 0058's own migration docstring for the
full tradeoff), not an oversight: it also pins every pre-existing PRIVATE
group's coded bookings to exactly 30 minutes, where before this migration
they were unconstrained by any group-level duration at all. An organization
that books other lengths through a private group must set that group's
``duration`` explicitly after this deploys -- this backfill cannot know
what length a given private group's bookings should be, and 30 minutes is
a default, not a discovered fact.

Idempotency
-----------
Only rows with ``duration IS NULL`` are ever touched (the ``UPDATE``'s
outer ``WHERE`` clause and the draining subquery both carry the guard), so
re-running this function after a partial failure completes the remaining
rows without rewriting any row a prior run -- or a human, or another
process -- already filled in.

Cross-organization raw SQL
---------------------------
Same documented exception as ``_0034_backfill_helpers.py``: a one-off
data-migration backfill needs to see every ``CalendarGroup`` row across
every organization at once, not a single tenant's slice, so it uses the raw
cursor directly rather than the org-scoped ORM manager. Table name is a
literal in the statement below (not interpolated from a variable) so
ruff's ``S608`` SQL-injection heuristic does not flag it -- there is no
untrusted input in this statement, only the ``%s``-parameterized duration
value and batch size.

Drain loop, not a snapshotted ``MAX(id)`` bound
-------------------------------------------------
``manage.py migrate`` runs inside Render's build step while the *previous*
deploy's pods are still serving traffic, so old code -- which predates
``duration`` entirely -- can ``INSERT`` new ``CalendarGroup`` rows with
``duration`` NULL for the whole duration of this backfill. A bound
computed once up front (``SELECT MAX(id)`` before the loop starts) would
never see a row inserted after that snapshot -- its id exceeds the bound,
the loop's upper-bound condition never reaches it, and the row would be
left with ``duration IS NULL``, silently reintroducing the exact
fail-closed break this migration exists to close, for a row inserted
mid-deploy. This is exactly the bug a previous reviewer caught in an
earlier draft of this app's backfill chain (see
``_0053_backfill_helpers.py``'s and ``_0056_backfill_helpers.py``'s own
"Drain loop" sections). Instead, ``backfill_calendargroup_duration``
repeatedly re-queries ``WHERE duration IS NULL ORDER BY id LIMIT <batch>``
until a query returns nothing -- a true drain, not a fixed range -- so a
row inserted mid-drain (by old code, or by anything else) is picked up by
a later iteration rather than missed.

Batch ``UPDATE``, not a per-row loop
--------------------------------------
Every row gets the exact same value (``timedelta(minutes=30)``), so unlike
``_0053_backfill_helpers.py`` (which must assign a distinct random value
per row and can therefore only ``UPDATE`` one row at a time), each batch
here is filled with a single parameterized ``UPDATE ... WHERE id IN
(SELECT id FROM ... WHERE duration IS NULL ORDER BY id LIMIT %s)`` -- same
shape as ``_0056_backfill_helpers.py``'s batch ``UPDATE``.

No reverse helper
------------------
This module intentionally has no ``reverse_backfill_calendargroup_duration``
counterpart. 0058's migration reverse is ``RunPython.noop`` -- see that
migration's docstring for why NULLing every duration back out would
re-break every public group's fail-closed check (and silently unpin every
private group's coded bookings back to "any length"), and buys nothing
anyway: a full reverse past this point continues on to 0051's reverse,
which drops the ``duration`` column outright regardless of what this step
leaves in it.
"""

from datetime import timedelta
from typing import Any

from django.db import connection


BATCH_SIZE = 500

#: The value every NULL ``duration`` row is filled with. See the module
#: docstring's "Why 30 minutes, for every group" section for the reasoning.
BACKFILL_DURATION = timedelta(minutes=30)

# Table name is a literal in the statement below (not interpolated from a
# variable) so ruff's S608 SQL-injection heuristic does not flag it -- there
# is no untrusted input in this statement, only the ``%s``-parameterized
# duration value and batch size. Matches the convention in
# ``_0056_backfill_helpers.py``.
_UPDATE_BATCH = """
    UPDATE calendar_integration_calendargroup
    SET duration = %s
    WHERE duration IS NULL
      AND id IN (
        SELECT id FROM calendar_integration_calendargroup
        WHERE duration IS NULL
        ORDER BY id
        LIMIT %s
      )
"""


def backfill_calendargroup_duration() -> None:
    """Fill ``duration`` for every ``CalendarGroup`` row that lacks one.

    Sets ``duration = timedelta(minutes=30)`` on EVERY row with
    ``duration IS NULL`` -- public and private groups alike; see the module
    docstring's "Why 30 minutes, for every group" section for why this is
    deliberate, not an oversight. Drains ``BATCH_SIZE``-sized batches
    (never ``.all()``, never a snapshotted id bound -- see the module
    docstring's "Drain loop" section for why) with a single parameterized
    ``UPDATE`` per batch. Terminates when a drain query updates no rows, at
    which point every row that was ``NULL`` at any point during the loop --
    including one inserted concurrently, by old code, mid-backfill -- has
    been filled.
    """
    while True:
        with connection.cursor() as cursor:
            params: list[Any] = [BACKFILL_DURATION, BATCH_SIZE]
            cursor.execute(_UPDATE_BATCH, params)
            updated = cursor.rowcount

        if not updated:
            break
