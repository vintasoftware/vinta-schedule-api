"""Make CalendarGroup.public_booking_slug non-null + globally unique (step 3/3).

Third migration of the 0052 -> 0053 -> 0054 chain (see 0052's docstring for
why this is three migrations rather than three operations in one file). By
this point every ``CalendarGroup`` row -- pre-existing (backfilled by 0053)
and newly created since 0052 (via the model's Python-level default, declared
here for the first time -- see below) -- has a distinct, non-NULL
``public_booking_slug``.

``SeparateDatabaseAndState`` pairs a state-only ``AlterField`` (the model's
final shape: ``null=False``, ``unique=True``,
``default=generate_public_booking_slug`` -- no separate ``db_index=True``,
since Django never builds one alongside ``unique=True``; the unique
constraint's own index already serves lookups) with database operations
that reach that shape via the standard Postgres zero-downtime
unique-constraint recipe, plus one deploy-safety operation that isn't part
of the model's shape at all (step 4 below). Django has no built-in operation
for a *unique*
index built ``CONCURRENTLY`` -- ``AddIndexConcurrently`` only builds plain,
non-unique ``models.Index`` objects, and a straight ``AlterField(unique=True)``
would instead emit a blocking, non-concurrent
``ALTER TABLE ... ADD CONSTRAINT ... UNIQUE (...)``, which the plan
explicitly asks to avoid:

1. ``CREATE UNIQUE INDEX CONCURRENTLY`` -- builds the index without holding
   a lock that blocks writes for the build's duration (only a brief lock at
   the very start/end). Must run outside any transaction, which
   ``atomic = False`` on this migration provides.
2. ``ALTER TABLE ... ADD CONSTRAINT ... UNIQUE USING INDEX`` -- attaches the
   already-built index as a named constraint. Fast and metadata-only (no
   rebuild, no scan) since the index it attaches to already exists and is
   already valid.
3. ``ALTER TABLE ... ALTER COLUMN ... SET NOT NULL`` -- takes a full-table
   scan under Postgres's classic implementation, but ``CalendarGroup`` is not
   a hot table by this repo's own definition (hot = "receiving non-trivial
   write traffic" -- calendar events, bundle relationships, bookings,
   organization members; an organization creates at most a handful of
   calendar groups, not a per-request volume), so the direct statement is
   the right call per the ``add-migration`` skill's own guidance ("small
   table (< 100k rows) -> either pattern is fine") rather than the
   ``CHECK ... NOT VALID`` / ``VALIDATE CONSTRAINT`` two-phase dance reserved
   for genuinely hot tables.
4. ``ALTER TABLE ... ALTER COLUMN ... SET DEFAULT`` -- a deploy-window safety
   net, not part of the model's final shape. ``manage.py migrate`` runs
   inside Render's build step while the *previous* deploy's pods are still
   serving traffic (see ``render_build.sh``), and a plain Python-level
   ``default=`` on a model field (the one this field already carries, see
   below) is invisible to the database -- ``sqlmigrate`` on this migration
   before this operation was added carried no ``DEFAULT`` clause at all. In
   that window, an old-code pod running an ``INSERT`` that does not know
   about ``public_booking_slug`` yet (any code compiled against pre-0052
   models, or ORM machinery that otherwise omits an unspecified column) hits
   step 3's fresh ``NOT NULL`` as a hard ``NotNullViolation`` -- a 500 with no
   recovery until roll-forward, since a service rollback does not revert an
   already-applied migration. A DB-level ``DEFAULT`` closes that window:
   ``replace(gen_random_uuid()::text, '-', '')`` is 32 lowercase hex
   characters (fits ``max_length=32`` exactly), drawn from core Postgres's
   CSPRNG (``gen_random_uuid()`` has been built into Postgres core, no
   extension required, since v13 -- confirmed present on this database), and
   as globally collision-resistant as the UUIDv4 it comes from. This is
   deliberately a **different format** from the ``secrets.token_urlsafe(16)``
   the Python ``default=generate_public_booking_slug`` produces for
   *application-issued* inserts (below) -- do not "unify" the two into one
   expression. Both are unguessable CSPRNG output; they simply belong to two
   different call paths (raw/old-code inserts that skip the column entirely,
   vs. every insert the current ORM performs) and are expressed in two
   different places (the database's own column default vs. Python) on
   purpose. Implemented as a raw ``RunSQL`` rather than the model field's
   ``db_default=`` kwarg because this migration already routes its DDL
   through hand-written ``database_operations`` under
   ``SeparateDatabaseAndState`` -- the ``state_operations`` below stay exactly
   as they were, so this operation is invisible to ``makemigrations --check``
   and does not appear on the model; it exists purely as a DB-level
   guardrail Django itself never relies on.

The Python ``default=generate_public_booking_slug`` callable is declared on
the field's state starting here, not in 0052 -- see 0052's docstring for why
declaring it that early would have made Django emit a single shared default
value for every pre-existing row at ``ADD COLUMN`` time. Declaring it only in
this state-only ``AlterField`` means it never reaches the database as a SQL
default at all; it only governs what value the ORM assigns a *new*
``CalendarGroup`` instance at ``Model.save()`` time from this migration
onward.

Reverse
-------
Reverses in the opposite order Postgres expects, automatically, because
``SeparateDatabaseAndState.database_backwards`` iterates its
``database_operations`` in reverse: ``DROP DEFAULT`` undoes step 4 (see
above -- purely a DB-level guardrail, nothing in the model or state ever
referenced it, so dropping it is a clean no-op for everything except raw
inserts that would again need to name every non-nullable column explicitly),
then ``DROP NOT NULL`` undoes step 3, then ``DROP CONSTRAINT IF EXISTS``
undoes step 2 -- dropping the constraint also drops the index it owns, so
nothing is orphaned -- then step 1's own reverse is a defensive
``DROP INDEX CONCURRENTLY IF EXISTS`` that is a no-op by the time it runs
(the constraint drop in step 2 already removed the index), guarding against
the two ever being reordered without erroring on a double-drop. The state
reverts to nullable/non-unique/no-default, matching 0052's post-forward
state exactly -- the column's actual data (the slugs 0053 wrote) is
untouched by any of this, since none of these four statements touch row
values, only constraints/nullability/the column default.

Resumability guard (operation 0)
---------------------------------
``atomic = False`` (required for the ``CONCURRENTLY`` build below) means a
failure partway through this migration is NOT rolled back: whatever DDL
already committed stays committed, but Django never records ``0054`` itself
as applied. A failure between step 1's ``CREATE UNIQUE INDEX CONCURRENTLY``
succeeding and step 3's ``SET NOT NULL`` finishing -- or a cancelled
``CREATE INDEX CONCURRENTLY`` that leaves an ``indisvalid = false`` index
behind -- would otherwise wedge a re-run: ``migrate`` would try step 1 again
and die on ``relation "calendargroup_public_booking_slug_uniq" already
exists``, needing a manual ``DROP INDEX`` before the migration could ever
be re-attempted. Operation 0 below is a defensive
``DROP INDEX CONCURRENTLY IF EXISTS`` (reverse: ``noop`` -- there is nothing
for it to undo; it does not create anything) that makes every re-run start
from a clean slate regardless of where a prior attempt died.
``DROP INDEX CONCURRENTLY`` requires running outside a transaction, exactly
like ``CREATE INDEX CONCURRENTLY`` -- ``atomic = False`` on this migration
already provides that.
"""

import calendar_integration.models
from django.db import migrations, models


TABLE = "calendar_integration_calendargroup"
CONSTRAINT_NAME = "calendargroup_public_booking_slug_uniq"

DROP_STALE_INDEX_IF_ANY = f"DROP INDEX CONCURRENTLY IF EXISTS {CONSTRAINT_NAME};"

CREATE_UNIQUE_INDEX_CONCURRENTLY = (
    f"CREATE UNIQUE INDEX CONCURRENTLY {CONSTRAINT_NAME} ON {TABLE} (public_booking_slug);"
)
DROP_INDEX_CONCURRENTLY_IF_EXISTS = f"DROP INDEX CONCURRENTLY IF EXISTS {CONSTRAINT_NAME};"

ADD_CONSTRAINT_USING_INDEX = (
    f"ALTER TABLE {TABLE} ADD CONSTRAINT {CONSTRAINT_NAME} UNIQUE USING INDEX {CONSTRAINT_NAME};"
)
DROP_CONSTRAINT_IF_EXISTS = f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {CONSTRAINT_NAME};"

SET_NOT_NULL = f"ALTER TABLE {TABLE} ALTER COLUMN public_booking_slug SET NOT NULL;"
DROP_NOT_NULL = f"ALTER TABLE {TABLE} ALTER COLUMN public_booking_slug DROP NOT NULL;"

# Deploy-window safety net only -- see the module docstring's step 4. Not the
# same value space as the Python ``default=generate_public_booking_slug``
# below; do not unify the two.
SET_DB_DEFAULT = (
    f"ALTER TABLE {TABLE} ALTER COLUMN public_booking_slug "
    "SET DEFAULT replace(gen_random_uuid()::text, '-', '');"
)
DROP_DB_DEFAULT = f"ALTER TABLE {TABLE} ALTER COLUMN public_booking_slug DROP DEFAULT;"

HELP_TEXT = (
    "Opaque, unguessable identifier used to address this group on the "
    "unauthenticated codeless booking route, instead of the integer primary "
    "key. Uniqueness is GLOBAL (not scoped to organization) because that "
    "route carries no organization in its path -- the slug alone must "
    "identify exactly one group system-wide. Authorizes nothing by itself: "
    "accepts_public_scheduling still gates codeless booking, and a group "
    "later flipped to public already has its identifier."
)


class Migration(migrations.Migration):
    """Make CalendarGroup.public_booking_slug NOT NULL + globally UNIQUE (3/3)."""

    atomic = False

    dependencies = [
        ("calendar_integration", "0053_backfill_calendargroup_public_booking_slug"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="calendargroup",
                    name="public_booking_slug",
                    field=models.CharField(
                        max_length=32,
                        unique=True,
                        default=calendar_integration.models.generate_public_booking_slug,
                        help_text=HELP_TEXT,
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=DROP_STALE_INDEX_IF_ANY,
                    reverse_sql=migrations.RunSQL.noop,
                ),
                migrations.RunSQL(
                    sql=CREATE_UNIQUE_INDEX_CONCURRENTLY,
                    reverse_sql=DROP_INDEX_CONCURRENTLY_IF_EXISTS,
                ),
                migrations.RunSQL(
                    sql=ADD_CONSTRAINT_USING_INDEX,
                    reverse_sql=DROP_CONSTRAINT_IF_EXISTS,
                ),
                migrations.RunSQL(
                    sql=SET_NOT_NULL,
                    reverse_sql=DROP_NOT_NULL,
                ),
                migrations.RunSQL(
                    sql=SET_DB_DEFAULT,
                    reverse_sql=DROP_DB_DEFAULT,
                ),
            ],
        ),
    ]
