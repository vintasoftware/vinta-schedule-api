"""Make CalendarManagementToken.kind NOT NULL, with a DB-level default (step 3/3).

Third migration of the 0055 -> 0056 -> 0057 chain (see 0055's docstring for
why this is three migrations rather than one). By this point every
``CalendarManagementToken`` row -- pre-existing (backfilled by 0056
according to the old heuristic) and newly created since 0055 (via the
model's Python-level ``default=MANAGEMENT_TOKEN``, declared here for the
first time -- see below) -- has a non-NULL ``kind``.

No index
--------
``kind`` is deliberately left unindexed. Both call sites that read through
``CalendarManagementTokenQuerySet.booking_codes`` (``revoke_token`` and
``BookingCodeViewSet.destroy``, via ``CalendarManagementTokenManager.
booking_codes_for_organization``) resolve a single row by primary key
(``.get(id=token_id)``) -- the ``organization``/``kind`` predicates apply
against an already-uniquely-identified row, not as a scan-narrowing
predicate, so no index on ``kind`` (alone or composite with
``organization``) would be used by the planner for either query. The column
is also low-cardinality (two values), which makes a plain B-tree index a
poor candidate even if a scanning query existed. If a future read scans
*all* booking codes for an organization (e.g. a support/admin listing),
revisit this alongside that read's actual query shape rather than guessing
now.

Lock / downtime audit
----------------------
Unlike Phase 3b's ``0054`` (which needed ``atomic = False`` for a
``CONCURRENTLY`` unique index build), this migration adds no index, so it
runs as an ordinary, fully atomic migration -- a single transaction, rolled
back whole on any failure, no resumability hazard to guard against.
Within that transaction:

1. ``ADD CONSTRAINT ... CHECK (kind IS NOT NULL) NOT VALID`` -- a brief
   ``SHARE ROW EXCLUSIVE``-class lock; does **not** scan the table (existing
   rows are not checked yet).
2. A straggler-catching ``UPDATE ... SET kind = 'management_token' WHERE
   kind IS NULL`` -- belt-and-suspenders alongside 0056's drain loop. 0056
   should have already left zero NULL rows, but this transaction re-checks
   immediately before validating, catching anything that fell through
   (a bug in 0056, or -- since ``atomic = True`` here means every statement
   in this migration holds its locks until COMMIT -- nothing can race this
   specific transaction from outside it once ADD CONSTRAINT above has run,
   so this is a correctness safety net rather than a concurrency one).
   Classifies any straggler as ``MANAGEMENT_TOKEN``, matching the field's
   own fail-closed default (see the model's ``help_text``): an
   unclassifiable row becomes un-revokable, never wrongly revokable.
3. ``VALIDATE CONSTRAINT`` -- scans the table to confirm every row satisfies
   the CHECK, but takes only a ``SHARE UPDATE EXCLUSIVE`` lock, which does
   not block concurrent reads or writes on this table (outside this
   transaction's own lock scope).
4. ``ALTER COLUMN kind SET NOT NULL`` -- Postgres (since v12) recognizes the
   just-validated CHECK constraint from step 1 and promotes it to the
   column's ``NOT NULL`` attribute **without re-scanning the table** --
   fast and metadata-only. This is the same optimization AGENTS.md's
   lock-aware reference describes as unnecessary "in Postgres < 16"; this
   migration takes the belt-and-suspenders CHECK/VALIDATE route anyway
   because, unlike Phase 3b's ``CalendarGroup`` (a handful of rows per
   organization, explicitly reasoned as "not hot"), this table is written
   on every booking-code mint today and is about to take substantially more
   write volume once Phase 8 mints two codes per booking automatically --
   the same caution 0051 already applied to this exact table for its FK
   constraint (``NOT VALID`` + ``VALIDATE CONSTRAINT``), extended here for
   consistency.
5. ``DROP CONSTRAINT`` -- the CHECK from step 1 is now redundant (its job is
   done by the column's own ``NOT NULL`` attribute as of step 4); dropping
   it leaves nothing orphaned and matches the model's state, which declares
   no such constraint.
6. ``ALTER COLUMN kind SET DEFAULT 'management_token'`` -- a deploy-window
   safety net, not part of the model's final shape by itself (the model's
   ``db_default=`` kwarg is what requests it; see below). ``manage.py
   migrate`` runs inside Render's build step while the *previous* deploy's
   pods are still serving traffic, and those pods' compiled code predates
   this column entirely -- any ``INSERT`` they issue omits ``kind``
   altogether. Once step 4 above lands, such an ``INSERT`` would hit a
   ``NotNullViolation`` without this default in place.

The Python ``default=CalendarManagementTokenKind.MANAGEMENT_TOKEN`` and the
``db_default=CalendarManagementTokenKind.MANAGEMENT_TOKEN`` are both
declared on the field's state starting here, not in 0055 -- see 0055's
docstring for why declaring either that early would have made every
pre-existing row (correctly ``BOOKING_CODE`` in some cases) default to
``MANAGEMENT_TOKEN`` before 0056's heuristic backfill ever ran.

Reverse
-------
``SeparateDatabaseAndState.database_backwards`` runs the six
``database_operations`` above in reverse order, each executing its own
``reverse_sql``:

1. Step 6's reverse (``DROP DEFAULT``) runs first, undoing the DB-level
   default.
2. Step 5's reverse is ``noop`` -- the CHECK constraint it dropped going
   forward is already gone; there is nothing to restore (restoring it would
   only be dropped again by step 1's reverse below, and reversing INTO a
   state 0056 never had is not the goal).
3. Step 4's reverse (``DROP NOT NULL``) undoes the column's ``NOT NULL``
   attribute.
4. Steps 3, 2, and 1's reverses are all ``noop``: the CHECK constraint they
   built and validated no longer exists by this point (step 5's forward
   already dropped it), so there is nothing left to un-validate, un-catch,
   or un-add.

The end state is nullable, no default, no CHECK constraint -- exactly
0056's post-forward state. The column's actual data (the classifications
0056 wrote) is untouched throughout, since none of these six statements
write to row values except the defensive straggler ``UPDATE`` in step 2,
which never runs during a reverse.
"""

from django.db import migrations, models


TABLE = "calendar_integration_calendarmanagementtoken"
CHECK_CONSTRAINT_NAME = "calmgmttoken_kind_not_null_chk"

ADD_CHECK_NOT_VALID = (
    f"ALTER TABLE {TABLE} ADD CONSTRAINT {CHECK_CONSTRAINT_NAME} "
    "CHECK (kind IS NOT NULL) NOT VALID;"
)
DROP_CHECK_IF_EXISTS = f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {CHECK_CONSTRAINT_NAME};"

# Straggler safety net -- see step 2 in the module docstring. Classifies as
# MANAGEMENT_TOKEN, matching the field's own fail-closed default. Table name
# is a literal here (not the ``TABLE`` variable) so ruff's S608
# SQL-injection heuristic does not flag it -- there is no untrusted input.
CATCH_STRAGGLERS = (
    "UPDATE calendar_integration_calendarmanagementtoken "
    "SET kind = 'management_token' WHERE kind IS NULL;"
)

VALIDATE_CHECK = f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {CHECK_CONSTRAINT_NAME};"

SET_NOT_NULL = f"ALTER TABLE {TABLE} ALTER COLUMN kind SET NOT NULL;"
DROP_NOT_NULL = f"ALTER TABLE {TABLE} ALTER COLUMN kind DROP NOT NULL;"

# Deploy-window safety net only -- see step 6 in the module docstring.
SET_DB_DEFAULT = f"ALTER TABLE {TABLE} ALTER COLUMN kind SET DEFAULT 'management_token';"
DROP_DB_DEFAULT = f"ALTER TABLE {TABLE} ALTER COLUMN kind DROP DEFAULT;"

HELP_TEXT = (
    "Explicit discriminator: BOOKING_CODE tokens are single-use booking "
    "codes, selected by CalendarManagementTokenQuerySet.booking_codes and "
    "therefore revokable via CalendarPermissionService.revoke_token / "
    "DELETE /booking-codes/<id>/. Everything else (owner, attendee, "
    "external-attendee tokens) is MANAGEMENT_TOKEN and never revokable "
    "through those surfaces. Defaults to MANAGEMENT_TOKEN deliberately: a "
    "mint path that forgets to set this produces an un-revokable token "
    "rather than a wrongly-revokable one -- it fails closed. Set "
    "explicitly by every create_*_token method on "
    "CalendarPermissionService; the default exists only as a safety net, "
    "never leaned on by a known call site."
)


class Migration(migrations.Migration):
    """Make CalendarManagementToken.kind NOT NULL with a DB default (3/3)."""

    dependencies = [
        ("calendar_integration", "0056_backfill_calendarmanagementtoken_kind"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="calendarmanagementtoken",
                    name="kind",
                    field=models.CharField(
                        max_length=20,
                        choices=[
                            ("booking_code", "Booking Code"),
                            ("management_token", "Management Token"),
                        ],
                        default="management_token",
                        db_default="management_token",
                        help_text=HELP_TEXT,
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=ADD_CHECK_NOT_VALID,
                    reverse_sql=migrations.RunSQL.noop,
                ),
                migrations.RunSQL(
                    sql=CATCH_STRAGGLERS,
                    reverse_sql=migrations.RunSQL.noop,
                ),
                migrations.RunSQL(
                    sql=VALIDATE_CHECK,
                    reverse_sql=migrations.RunSQL.noop,
                ),
                migrations.RunSQL(
                    sql=SET_NOT_NULL,
                    reverse_sql=DROP_NOT_NULL,
                ),
                migrations.RunSQL(
                    sql=DROP_CHECK_IF_EXISTS,
                    reverse_sql=migrations.RunSQL.noop,
                ),
                migrations.RunSQL(
                    sql=SET_DB_DEFAULT,
                    reverse_sql=DROP_DB_DEFAULT,
                ),
            ],
        ),
    ]
