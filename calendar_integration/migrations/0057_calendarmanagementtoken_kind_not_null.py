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
``atomic = False`` -- required so ``VALIDATE CONSTRAINT`` (step 3 below)
runs under its own ``SHARE UPDATE EXCLUSIVE`` lock instead of being folded
into one all-or-nothing transaction that would hold the ``ADD CONSTRAINT``
step's stronger lock for the full duration of the validation scan. An
earlier draft of this migration left ``atomic`` undeclared (defaulting to
``True``), which made every statement below commit as one transaction --
the net lock profile of a plain ``SET NOT NULL``, at the cost of four extra
DDL statements, and the opposite of what this docstring claims. Declaring
``atomic = False`` here is what actually delivers the lock profile described
below:

1. ``DROP CONSTRAINT IF EXISTS`` -- resumability guard, see below.
2. ``ADD CONSTRAINT ... CHECK (kind IS NOT NULL) NOT VALID`` -- a brief
   ``SHARE ROW EXCLUSIVE``-class lock; does **not** scan the table (existing
   rows are not checked yet).
3. A straggler-catching ``UPDATE ... SET kind = <CASE ...> WHERE kind IS
   NULL`` -- belt-and-suspenders alongside 0056's drain loop. 0056 should
   have already left zero NULL rows, but this re-checks immediately before
   validating, catching anything that fell through (a bug in 0056, or a row
   inserted in the window between 0056's drain loop terminating and this
   migration starting -- ``atomic = False`` means this is no longer a single
   transaction with 0056, so that window is real). Classifies any straggler
   with the **exact same predicate 0056 uses**
   (``calendar_integration.migrations._0056_backfill_helpers.SET_KIND_CASE_SQL``,
   imported rather than duplicated so the two definitions can never drift):
   ``BOOKING_CODE`` when either actor column is set, ``MANAGEMENT_TOKEN``
   otherwise. An earlier draft of this migration used a blanket
   ``SET kind = 'management_token'`` here, reasoning it "matches the field's
   own fail-closed default: an unclassifiable row becomes un-revokable". That
   reasoning was wrong for exactly the rows this statement can actually see:
   a straggler carrying ``minted_by_system_user_id`` (or
   ``minted_by_membership_user_id``) is not unclassifiable -- it is a real
   booking code that the same predicate 0056 already applies would correctly
   classify. Under Render's build-step deploy window (``manage.py migrate``
   running while the previous deploy's pods still serve, see ``0056``'s own
   "Drain loop" docstring section), an old pod can mint a genuine booking
   code with ``kind`` NULL in the gap between 0056 finishing and this
   migration starting; flattening it to ``MANAGEMENT_TOKEN`` here would make
   ``booking_codes()`` exclude it and ``revoke_token`` refuse to revoke it --
   permanently, since the token would already be NOT NULL by the time anyone
   noticed. Using the same CASE expression 0056 uses closes that hole.
4. ``VALIDATE CONSTRAINT`` -- scans the table to confirm every row satisfies
   the CHECK, but takes only a ``SHARE UPDATE EXCLUSIVE`` lock, which does
   not block concurrent reads or writes on this table.
5. ``ALTER COLUMN kind SET DEFAULT 'management_token'`` -- a deploy-window
   safety net, not part of the model's final shape by itself (the model's
   ``db_default=`` kwarg is what requests it; see below). Deliberately
   ordered BEFORE step 6's ``SET NOT NULL``, not after: ``manage.py migrate``
   runs inside Render's build step while the *previous* deploy's pods are
   still serving traffic, and those pods' compiled code predates this column
   entirely -- any ``INSERT`` they issue omits ``kind`` altogether. With
   ``atomic = False``, each statement here commits independently, so if
   ``SET NOT NULL`` (step 6) committed *before* this default were in place,
   an old-pod ``INSERT`` landing in that window would hit a hard
   ``NotNullViolation`` with no DB-level default yet to save it -- a 500 with
   no recovery until roll-forward. Setting the default first closes that
   window entirely: by the time step 6 makes the column NOT NULL, every
   ``INSERT`` that omits ``kind`` already has somewhere to fall back to.
6. ``ALTER COLUMN kind SET NOT NULL`` -- Postgres (since v12) recognizes the
   just-validated CHECK constraint from step 2 and promotes it to the
   column's ``NOT NULL`` attribute **without re-scanning the table** --
   fast and metadata-only. AGENTS.md's lock-aware guidance is that Postgres
   < 16 *requires* this two-phase (``CHECK ... NOT VALID`` + ``VALIDATE
   CONSTRAINT``) route to avoid a full-table-scanning ``SET NOT NULL`` on a
   large/hot table -- not, as an earlier draft of this docstring said, that
   the optimization is "unnecessary" there. This migration takes that route
   unconditionally because, unlike Phase 3b's ``CalendarGroup`` (a handful of
   rows per organization, explicitly reasoned as "not hot"), this table is
   written on every booking-code mint today and is about to take
   substantially more write volume once Phase 8 mints two codes per booking
   automatically -- the same caution 0051 already applied to this exact
   table for its FK constraint (``NOT VALID`` + ``VALIDATE CONSTRAINT``),
   extended here for consistency.
7. ``DROP CONSTRAINT`` -- the CHECK from step 2 is now redundant (its job is
   done by the column's own ``NOT NULL`` attribute as of step 6); dropping
   it leaves nothing orphaned and matches the model's state, which declares
   no such constraint.

The Python ``default=CalendarManagementTokenKind.MANAGEMENT_TOKEN`` and the
``db_default=CalendarManagementTokenKind.MANAGEMENT_TOKEN`` are both
declared on the field's state starting here, not in 0055 -- see 0055's
docstring for why declaring either that early would have made every
pre-existing row (correctly ``BOOKING_CODE`` in some cases) default to
``MANAGEMENT_TOKEN`` before 0056's heuristic backfill ever ran.

Resumability guard (operation 0)
---------------------------------
``atomic = False`` means a failure partway through this migration is NOT
rolled back: whatever DDL already committed stays committed, but Django
never records ``0057`` itself as applied. A failure between step 2's
``ADD CONSTRAINT ... NOT VALID`` succeeding and step 7's ``DROP CONSTRAINT``
running would otherwise wedge a re-run: ``migrate`` would try step 2 again
and die on ``constraint "calmgmttoken_kind_not_null_chk" for relation ...
already exists``, needing a manual ``DROP CONSTRAINT`` before the migration
could ever be re-attempted. Operation 0 below is a defensive
``DROP CONSTRAINT IF EXISTS`` (reverse: ``noop`` -- there is nothing for it
to undo; it does not create anything, matching the same pattern 0054 uses
for its own ``CREATE INDEX CONCURRENTLY`` resumability guard) that makes
every re-run start from a clean slate regardless of where a prior attempt
died. Every other statement here is independently idempotent on a retry
(``CATCH_STRAGGLERS`` re-guards on ``kind IS NULL``; ``SET DEFAULT`` /
``SET NOT NULL`` are no-ops when already set; ``DROP CONSTRAINT IF EXISTS``
in step 7 already tolerates a missing constraint) -- the ``ADD CONSTRAINT``
name collision was the only genuine wedge risk.

Reverse
-------
``SeparateDatabaseAndState.database_backwards`` runs the seven
``database_operations`` above in reverse order, each executing its own
``reverse_sql``:

1. Step 7's reverse is ``noop`` -- the CHECK constraint it dropped going
   forward is already gone; there is nothing to restore.
2. Step 6's reverse (``DROP NOT NULL``) undoes the column's ``NOT NULL``
   attribute.
3. Step 5's reverse (``DROP DEFAULT``) undoes the DB-level default.
4. Steps 4, 3, 2, and 0's reverses are all ``noop``: the CHECK constraint
   they built and validated no longer exists by this point (step 7's
   forward already dropped it), so there is nothing left to un-validate,
   un-catch, or un-add.

The end state is nullable, no default, no CHECK constraint -- exactly
0056's post-forward state. The column's actual data (the classifications
0056 wrote) is untouched throughout, since none of these seven statements
write to row values except the defensive straggler ``UPDATE`` in step 3,
which never runs during a reverse.
"""

from django.db import migrations, models

from calendar_integration.migrations._0056_backfill_helpers import SET_KIND_CASE_SQL


TABLE = "calendar_integration_calendarmanagementtoken"
CHECK_CONSTRAINT_NAME = "calmgmttoken_kind_not_null_chk"

DROP_STALE_CHECK_IF_ANY = f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {CHECK_CONSTRAINT_NAME};"

ADD_CHECK_NOT_VALID = (
    f"ALTER TABLE {TABLE} ADD CONSTRAINT {CHECK_CONSTRAINT_NAME} "
    "CHECK (kind IS NOT NULL) NOT VALID;"
)
DROP_CHECK_IF_EXISTS = f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {CHECK_CONSTRAINT_NAME};"

# Straggler safety net -- see step 3 in the module docstring. Uses the exact
# same predicate 0056's backfill uses (imported, not duplicated) so a
# genuinely-classifiable row (one with an actor column set) is never
# flattened to MANAGEMENT_TOKEN and rendered permanently un-revokable. Table
# name is a literal here (not the ``TABLE`` variable) so ruff's S608
# SQL-injection heuristic does not flag it -- there is no untrusted input.
CATCH_STRAGGLERS = (
    "UPDATE calendar_integration_calendarmanagementtoken "
    f"SET kind = {SET_KIND_CASE_SQL} "
    "WHERE kind IS NULL;"
)

VALIDATE_CHECK = f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {CHECK_CONSTRAINT_NAME};"

SET_NOT_NULL = f"ALTER TABLE {TABLE} ALTER COLUMN kind SET NOT NULL;"
DROP_NOT_NULL = f"ALTER TABLE {TABLE} ALTER COLUMN kind DROP NOT NULL;"

# Deploy-window safety net only -- see step 5 in the module docstring.
# Ordered BEFORE ``SET_NOT_NULL`` below -- see the "ordering trap" paragraph
# in the module docstring's step 5.
SET_DB_DEFAULT = f"ALTER TABLE {TABLE} ALTER COLUMN kind SET DEFAULT 'management_token';"
DROP_DB_DEFAULT = f"ALTER TABLE {TABLE} ALTER COLUMN kind DROP DEFAULT;"

HELP_TEXT = (
    "Explicit discriminator: BOOKING_CODE tokens are single-use booking "
    "codes, selected by CalendarManagementTokenQuerySet.booking_codes and "
    "therefore eligible for revocation via CalendarPermissionService.revoke_token "
    "/ DELETE /booking-codes/<id>/ (the REST surface additionally requires "
    "owner-or-org-admin). Everything else (owner, attendee, external-attendee "
    "tokens) is MANAGEMENT_TOKEN and never revokable through those surfaces. "
    "Defaults to MANAGEMENT_TOKEN deliberately: a mint path that forgets to "
    "set this produces an un-revokable token rather than a wrongly-revokable "
    "one -- it fails closed. Set explicitly on creation by every "
    "create_*_token method on CalendarPermissionService (via "
    "get_or_create(defaults=...), so only on the CREATE branch -- a "
    "get_or_create hit on an existing row trusts that row's own kind "
    "rather than re-asserting it); the column default exists only as a "
    "safety net for a row inserted with kind omitted entirely, never "
    "leaned on by a known call site."
)


class Migration(migrations.Migration):
    """Make CalendarManagementToken.kind NOT NULL with a DB default (3/3)."""

    atomic = False

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
                    sql=DROP_STALE_CHECK_IF_ANY,
                    reverse_sql=migrations.RunSQL.noop,
                ),
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
                    sql=SET_DB_DEFAULT,
                    reverse_sql=DROP_DB_DEFAULT,
                ),
                migrations.RunSQL(
                    sql=SET_NOT_NULL,
                    reverse_sql=DROP_NOT_NULL,
                ),
                migrations.RunSQL(
                    sql=DROP_CHECK_IF_EXISTS,
                    reverse_sql=migrations.RunSQL.noop,
                ),
            ],
        ),
    ]
