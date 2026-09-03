"""Importable backfill helpers for ``CalendarManagementToken.kind``.

Extracted from migration 0056 so tests can call
``backfill_calendar_management_token_kind()`` directly without going through
the migration runner -- same pattern as
``calendar_integration/migrations/_0053_backfill_helpers.py``.

Classification rule (matches the heuristic being replaced, exactly)
---------------------------------------------------------------------
A pre-existing row is classified ``BOOKING_CODE`` when
``minted_by_membership_user_id IS NOT NULL OR minted_by_system_user_id IS
NOT NULL``, and ``MANAGEMENT_TOKEN`` otherwise -- the same predicate
``CalendarManagementTokenQuerySet.booking_codes`` used to filter on before
Phase 7 replaced it with this column. This backfill exists precisely to
freeze that predicate's *result* onto every pre-existing row before the
predicate itself is deleted, so historical classification does not change
the moment the heuristic goes away.

Idempotency
-----------
Both the row-selection query and the ``UPDATE`` carry the ``kind IS NULL``
guard, so re-running the backfill only touches rows a prior run never
reached. It does not reclassify or overwrite any row a prior run already
filled in -- including a row a human or another process set explicitly in
the interim.

Cross-organization raw SQL
---------------------------
This backfill must see every ``CalendarManagementToken`` row across every
organization, matching the documented exception in
``_0034_backfill_helpers.py`` for the same reason (a one-off data-migration
backfill, not a request-scoped read). Table name is a literal in every
statement below (not interpolated from a variable) so ruff's ``S608``
SQL-injection heuristic does not flag it -- there is no untrusted input in
any of these statements.

Drain loop, not a snapshotted ``MAX(id)`` bound
-------------------------------------------------
``manage.py migrate`` runs inside Render's build step while the *previous*
deploy's pods are still serving traffic, so old code can ``INSERT`` new
``CalendarManagementToken`` rows for the whole duration of this backfill
(with ``kind`` NULL, since old code predates this column). A bound computed
once up front (``SELECT MAX(id)`` before the loop starts) would never see a
row inserted after that snapshot -- its id exceeds the bound, the loop's
upper-bound condition never reaches it, and it would be left with ``kind IS
NULL`` for 0057's ``SET NOT NULL`` to fail on. This is exactly the bug
Phase 3b's reviewer caught in an earlier draft of that chain's backfill
(see ``_0053_backfill_helpers.py``'s own "Drain loop" section). Instead,
``backfill_calendar_management_token_kind`` repeatedly re-queries
``WHERE kind IS NULL ORDER BY id LIMIT <batch>`` until a query returns
nothing -- a true drain, not a fixed range -- so a row inserted mid-backfill
is picked up by a later iteration rather than missed.

Batch ``UPDATE``, not a per-row loop
--------------------------------------
Unlike ``_0053_backfill_helpers.py`` (which must assign a distinct random
value per row and therefore can only ``UPDATE`` one row at a time), every
row in a batch here is classified by the same ``CASE`` expression evaluated
against its own two columns -- there is nothing row-specific to compute in
Python. Each batch is filled with a single parameterized ``UPDATE ...
WHERE id IN (SELECT id FROM ... WHERE kind IS NULL ORDER BY id LIMIT
%s)`` rather than one statement per row.

No reverse helper
------------------
This module intentionally has no ``reverse_backfill_calendar_management_
token_kind`` counterpart. 0056's migration reverse is ``RunPython.noop`` --
see that migration's docstring for why NULLing the column back out is
avoided and buys nothing anyway.
"""

from django.db import connection


BATCH_SIZE = 500

# Table name is a literal in the statement below (not interpolated from a
# variable) so ruff's S608 SQL-injection heuristic does not flag it -- there
# is no untrusted input in this statement, only the ``%s``-parameterized
# batch size. Matches the convention in ``_0053_backfill_helpers.py``.
_UPDATE_BATCH = """
    UPDATE calendar_integration_calendarmanagementtoken
    SET kind = CASE
        WHEN minted_by_membership_user_id IS NOT NULL
             OR minted_by_system_user_id IS NOT NULL
        THEN 'booking_code'
        ELSE 'management_token'
    END
    WHERE kind IS NULL
      AND id IN (
        SELECT id FROM calendar_integration_calendarmanagementtoken
        WHERE kind IS NULL
        ORDER BY id
        LIMIT %s
      )
"""


def backfill_calendar_management_token_kind() -> None:
    """Fill ``kind`` for every ``CalendarManagementToken`` row that lacks one.

    Drains ``BATCH_SIZE``-sized batches of NULL-``kind`` rows (never
    ``.all()``, never a snapshotted id bound -- see the module docstring's
    "Drain loop" section for why), classifying each batch with a single
    ``UPDATE`` that mirrors the pre-Phase-7 ``booking_codes()`` heuristic
    exactly. Terminates when a drain query updates no rows, at which point
    every row that was ``NULL`` at any point during the loop -- including
    one inserted concurrently, by old code, mid-backfill -- has been
    filled.
    """
    while True:
        with connection.cursor() as cursor:
            cursor.execute(_UPDATE_BATCH, [BATCH_SIZE])
            updated = cursor.rowcount

        if not updated:
            break
