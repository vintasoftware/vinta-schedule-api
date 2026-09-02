"""Make CalendarGroup.public_booking_slug non-null + globally unique (step 3/3).

Third migration of the 0052 -> 0053 -> 0054 chain (see 0052's docstring for
why this is three migrations rather than three operations in one file). By
this point every ``CalendarGroup`` row -- pre-existing (backfilled by 0053)
and newly created since 0052 (via the model's Python-level default, declared
here for the first time -- see below) -- has a distinct, non-NULL
``public_booking_slug``.

``SeparateDatabaseAndState`` pairs a state-only ``AlterField`` (the model's
final shape: ``null=False``, ``unique=True``, ``db_index=True``,
``default=generate_public_booking_slug``) with three raw-SQL database
operations that reach that shape via the standard Postgres zero-downtime
unique-constraint recipe. Django has no built-in operation for a *unique*
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
``database_operations`` in reverse: ``DROP NOT NULL`` undoes step 3, then
``DROP CONSTRAINT IF EXISTS`` undoes step 2 -- dropping the constraint also
drops the index it owns, so nothing is orphaned -- then step 1's own reverse
is a defensive ``DROP INDEX CONCURRENTLY IF EXISTS`` that is a no-op by the
time it runs (the constraint drop in step 2 already removed the index),
guarding against the two ever being reordered without erroring on a
double-drop. The state reverts to nullable/non-unique/no-default, matching
0052's post-forward state exactly -- the column's actual data (the slugs
0053 wrote) is untouched by any of this, since none of these three
statements touch row values, only constraints/nullability.
"""

import calendar_integration.models
from django.db import migrations, models


TABLE = "calendar_integration_calendargroup"
CONSTRAINT_NAME = "calendargroup_public_booking_slug_uniq"

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
                        db_index=True,
                        default=calendar_integration.models.generate_public_booking_slug,
                        help_text=HELP_TEXT,
                    ),
                ),
            ],
            database_operations=[
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
            ],
        ),
    ]
