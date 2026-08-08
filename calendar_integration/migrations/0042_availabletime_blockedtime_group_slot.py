"""Add group-slot scoping to AvailableTime and BlockedTime (Phase 0 of
CALENDAR_GROUP_SCOPED_AVAILABILITY).

Both ``AvailableTime`` and ``BlockedTime`` are hot tables (recurring-event
expansion runs against them on every availability read), so this migration
follows the lock-aware column-addition pattern end to end:

1. **Add the column, no default, no inline FK.** Plain ``ADD COLUMN ... NULL``
   is metadata-only in Postgres — no table rewrite, brief ``ACCESS EXCLUSIVE``
   lock. Deliberately issued as raw SQL rather than a plain Django ``AddField``
   on a ``ForeignKey``: Django's own ``AddField`` for a FK field defers the
   constraint SQL to a *separate* statement already, but that deferred
   statement omits ``NOT VALID`` — it would still force an immediate,
   lock-holding validation scan of the whole table (see step 2's comment).
2. **Add the FK constraint ``NOT VALID``.** ``ADD CONSTRAINT ... NOT VALID``
   takes a brief ``SHARE ROW EXCLUSIVE`` lock and — critically — skips
   scanning existing rows, so it returns immediately regardless of table size.
3. **``VALIDATE CONSTRAINT`` separately.** Scans the table to confirm existing
   rows satisfy the constraint, but takes only ``SHARE UPDATE EXCLUSIVE``,
   which does not block reads or concurrent writes. Every existing row's new
   column is NULL (this migration writes nothing), and a NULL FK column is
   exempt from FK checking (Postgres ``MATCH SIMPLE``), so the scan has
   nothing to reject — it exists only to flip the constraint from
   "not validated" to "validated" without ever holding a strong lock for the
   scan's duration.
4. **Partial indexes ``CONCURRENTLY``.** ``CREATE INDEX CONCURRENTLY`` avoids
   the ``SHARE`` lock a plain ``CREATE INDEX`` would hold for the build's
   duration, which would block writes on a hot table. The index is a partial
   index (``WHERE group_slot_fk_id IS NOT NULL``) because the column is a base
   row (NULL) for the overwhelming majority of existing and future rows — see
   ``CALENDAR_GROUP_SCOPED_AVAILABILITY`` Guiding Decisions, "Row scoping".

No FK cascade is configured at the database level (``ON DELETE`` is omitted,
matching every other ``OrganizationForeignKey`` in this codebase — see
``common/fields.py``: ``TenantSafeForeignKey`` never passes a DB-level
``on_delete`` clause). Cascade-on-slot-deletion is enforced by Django's
Python-side deletion collector instead, via the model field's
``on_delete=models.CASCADE``. The collector walks relations through
``Model._base_manager`` (see ``django/db/models/deletion.py``), which for both
models — since neither ``AvailableTime``/``BlockedTime`` nor any ancestor sets
``Meta.base_manager_name`` — is Django's own auto-created, always-unfiltered
``Manager()``, never the ``group_slot``-filtered ``objects`` manager added in
this same phase. So deleting a ``CalendarGroupSlot`` still finds and cascades
to every referencing row, scoped or not.

**The FK constraints are ``DEFERRABLE INITIALLY DEFERRED`` — load-bearing, not
cosmetic.** Every FK constraint Django creates natively on Postgres carries
this clause (``django/db/backends/postgresql/operations.py::deferrable_sql``
always returns it), and Django's deletion collector relies on it: for a
*nullable* CASCADE relation, ``Collector.add()`` deliberately skips recording
a delete-order dependency ("nullable relationships ... do not affect the
order in which objects have to be deleted" — ``django/db/models/deletion.py``),
on the assumption that the FK check itself is deferred to ``COMMIT`` and will
therefore still pass even if the referenced row's ``DELETE`` executes before
the referencing rows' ``DELETE`` within the same transaction. Both
``group_slot_fk`` fields are nullable CASCADE relations, so this migration
must reproduce that same deferrable constraint by hand — a plain (non
-deferrable) ``NOT VALID`` constraint here would make ``CalendarGroupSlot``
deletion intermittently raise a foreign-key violation instead of cascading,
because Postgres would enforce the check immediately, before the collector's
(unsequenced, in this case) ``DELETE`` of the referencing rows runs.

``atomic = False`` is required for ``AddIndexConcurrently`` and also gives
each ``RunSQL`` statement here its own transaction, so the NOT VALID / VALIDATE
split actually avoids holding one lock across both steps.

Reverse
-------
Rolling back runs, in order: drop the concurrent indexes, drop the two FK
constraints (dropping a constraint is always instant — no scan), drop the two
columns. No orphaned objects remain — this restores the exact pre-migration
schema.
"""

import django.db.models.deletion
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


AVAILABLETIME_TABLE = "calendar_integration_availabletime"
BLOCKEDTIME_TABLE = "calendar_integration_blockedtime"
GROUPSLOT_TABLE = "calendar_integration_calendargroupslot"

AVAILABLETIME_FK_CONSTRAINT = "availabletime_group_slot_fk"
BLOCKEDTIME_FK_CONSTRAINT = "blockedtime_group_slot_fk"


class Migration(migrations.Migration):
    """Lock-aware: nullable column, NOT VALID FK + separate VALIDATE, concurrent indexes."""

    atomic = False

    dependencies = [
        ("calendar_integration", "0041_alter_calendarorganizationresourcesimport_status"),
        ("organizations", "0017_alter_organizationmembership_is_billing_owner"),
    ]

    operations = [
        # --- Step 1: add the raw columns (metadata-only, no inline FK) -----
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="availabletime",
                    name="group_slot_fk",
                    field=models.ForeignKey(
                        blank=True,
                        help_text=(
                            "If set, this available time applies only when the calendar is "
                            "evaluated inside this group slot, narrowing (never widening) base "
                            "availability there. Null (the default) means a base row that "
                            "applies everywhere the calendar is evaluated."
                        ),
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="group_scoped_available_times_fk_rel",
                        to="calendar_integration.calendargroupslot",
                    ),
                ),
                migrations.AddField(
                    model_name="availabletime",
                    name="group_slot",
                    field=models.ForeignObject(
                        editable=False,
                        from_fields=["group_slot_fk", "organization_id"],
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="group_scoped_available_times",
                        to="calendar_integration.calendargroupslot",
                        to_fields=["id", "organization_id"],
                    ),
                ),
                migrations.AddField(
                    model_name="blockedtime",
                    name="group_slot_fk",
                    field=models.ForeignKey(
                        blank=True,
                        help_text=(
                            "If set, this blocked time applies only when the calendar is "
                            "evaluated inside this group slot, and nowhere else. Null (the "
                            "default) means a base row that blocks time everywhere the "
                            "calendar is evaluated."
                        ),
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="group_scoped_blocked_times_fk_rel",
                        to="calendar_integration.calendargroupslot",
                    ),
                ),
                migrations.AddField(
                    model_name="blockedtime",
                    name="group_slot",
                    field=models.ForeignObject(
                        editable=False,
                        from_fields=["group_slot_fk", "organization_id"],
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="group_scoped_blocked_times",
                        to="calendar_integration.calendargroupslot",
                        to_fields=["id", "organization_id"],
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=f"ALTER TABLE {AVAILABLETIME_TABLE} ADD COLUMN group_slot_fk_id bigint NULL;",
                    reverse_sql=f"ALTER TABLE {AVAILABLETIME_TABLE} DROP COLUMN group_slot_fk_id;",
                ),
                migrations.RunSQL(
                    sql=f"ALTER TABLE {BLOCKEDTIME_TABLE} ADD COLUMN group_slot_fk_id bigint NULL;",
                    reverse_sql=f"ALTER TABLE {BLOCKEDTIME_TABLE} DROP COLUMN group_slot_fk_id;",
                ),
            ],
        ),
        # --- Step 2: add the FK constraints as NOT VALID --------------------
        # DEFERRABLE INITIALLY DEFERRED matches every FK constraint Django
        # creates natively on Postgres (see the module docstring) and is
        # required for the ORM's deletion collector to cascade correctly.
        migrations.RunSQL(
            sql=(
                f"ALTER TABLE {AVAILABLETIME_TABLE} "
                f"ADD CONSTRAINT {AVAILABLETIME_FK_CONSTRAINT} "
                f"FOREIGN KEY (group_slot_fk_id) REFERENCES {GROUPSLOT_TABLE} (id) "
                f"DEFERRABLE INITIALLY DEFERRED "
                f"NOT VALID;"
            ),
            reverse_sql=(
                f"ALTER TABLE {AVAILABLETIME_TABLE} "
                f"DROP CONSTRAINT IF EXISTS {AVAILABLETIME_FK_CONSTRAINT};"
            ),
        ),
        migrations.RunSQL(
            sql=(
                f"ALTER TABLE {BLOCKEDTIME_TABLE} "
                f"ADD CONSTRAINT {BLOCKEDTIME_FK_CONSTRAINT} "
                f"FOREIGN KEY (group_slot_fk_id) REFERENCES {GROUPSLOT_TABLE} (id) "
                f"DEFERRABLE INITIALLY DEFERRED "
                f"NOT VALID;"
            ),
            reverse_sql=(
                f"ALTER TABLE {BLOCKEDTIME_TABLE} "
                f"DROP CONSTRAINT IF EXISTS {BLOCKEDTIME_FK_CONSTRAINT};"
            ),
        ),
        # --- Step 3: validate the constraints in a separate, weak-lock step -
        migrations.RunSQL(
            sql=(
                f"ALTER TABLE {AVAILABLETIME_TABLE} "
                f"VALIDATE CONSTRAINT {AVAILABLETIME_FK_CONSTRAINT};"
            ),
            # The constraint is dropped wholesale by the ADD CONSTRAINT step's
            # reverse; there is nothing to "un-validate" here.
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql=(
                f"ALTER TABLE {BLOCKEDTIME_TABLE} "
                f"VALIDATE CONSTRAINT {BLOCKEDTIME_FK_CONSTRAINT};"
            ),
            reverse_sql=migrations.RunSQL.noop,
        ),
        # --- Step 4: partial indexes, built concurrently ---------------------
        AddIndexConcurrently(
            model_name="availabletime",
            index=models.Index(
                fields=["organization", "group_slot_fk"],
                condition=models.Q(("group_slot_fk__isnull", False)),
                name="availabletime_group_slot_idx",
            ),
        ),
        AddIndexConcurrently(
            model_name="blockedtime",
            index=models.Index(
                fields=["organization", "group_slot_fk"],
                condition=models.Q(("group_slot_fk__isnull", False)),
                name="blockedtime_group_slot_idx",
            ),
        ),
    ]
