"""Phase 7b (FINAL): swap ``OrganizationMembership`` to a composite primary key.

Goal
----
Make ``OrganizationMembership``'s primary key the ``(user_id, organization_id)``
pair and drop the implicit ``id`` ``BigAutoField``. A membership's identity is the
(user, org) pair (explicit requester decision).

Chosen sequencing — Option A (keep ``uniq_membership_user_organization``)
------------------------------------------------------------------------
Three raw-SQL composite PROTECT FKs reference
``organizations_organizationmembership (user_id, organization_id)`` and bind to the
``uniq_membership_user_organization`` UNIQUE constraint (added in Phases 2b/4b/6):

  * ``calownership_membership_protect_fk``  (calendar_integration_calendarownership)
  * ``evattendance_membership_protect_fk``  (calendar_integration_eventattendance)
  * ``calmgmttoken_membership_protect_fk``  (calendar_integration_calendarmanagementtoken)

Postgres binds a foreign key to a *specific* unique/PK constraint. If we dropped
``uniq_membership_user_organization`` those three FKs would lose their referenced
target and Postgres would refuse the drop while they depend on it. Option A keeps
that unique constraint untouched, so:

  * the 3 FKs keep depending on it — **no drop/re-add of the calendar FKs needed**;
  * we additionally ``ADD PRIMARY KEY (user_id, organization_id)``, which creates a
    second unique index on the same columns. It is redundant with the unique
    constraint but legal, and Postgres allows exactly one PRIMARY KEY per table
    (the old ``id`` PK is dropped first).

Django's own ``check`` is **clean** with the redundant unique kept (it does not flag
the unique-vs-PK overlap), so Option A keeps both ``makemigrations --check`` clean
and all three calendar FKs valid — the safest path. The alternative (Option B:
drop the 3 FKs, drop the unique, swap the PK, re-add the 3 FKs against the PK
columns) was rejected because it touches three tenant calendar tables in this
organizations migration for no behavioural gain.

Why this is NOT a plain ``RemoveField(id) + AddField(pk)`` auto-migration
-------------------------------------------------------------------------
``models.CompositePrimaryKey`` is a *virtual* ORM-level field: Django's autodetector
emits ``DROP COLUMN id`` for the removed ``id`` and treats ``AddField(pk)`` as a
DB no-op. That leaves the table with **no real PRIMARY KEY** — it merely relies on
the pre-existing unique constraint for ORM identity. The acceptance contract for
this phase requires an actual composite ``PRIMARY KEY (user_id, organization_id)``
visible in ``psql \\d``. We therefore use ``SeparateDatabaseAndState``: the *state* half
runs Django's expected ``RemoveField`` + ``AddField`` (so ``makemigrations --check``
stays clean), while the *database* half runs hand-written raw SQL that drops the
``id`` PK + column and explicitly adds the composite PRIMARY KEY.

Forward DB SQL
--------------
    ALTER TABLE organizations_organizationmembership
      DROP CONSTRAINT organizations_organizationmembership_pkey;   -- old id PK
    ALTER TABLE organizations_organizationmembership
      DROP COLUMN id;                                              -- + its identity seq
    ALTER TABLE organizations_organizationmembership
      ADD PRIMARY KEY (user_id, organization_id);                 -- new composite PK

``user_id`` and ``organization_id`` are already ``NOT NULL`` (the two FKs are
non-nullable), so ``ADD PRIMARY KEY`` does not have to alter nullability and the
existing data already satisfies uniqueness (guaranteed by
``uniq_membership_user_organization``). The new PK index is built from scratch.

Lock / downtime audit (PRODUCTION COST)
---------------------------------------
This is the riskiest lock in the plan: a PK swap on a referenced, multi-tenant
table. Every statement here takes ``ACCESS EXCLUSIVE`` on
``organizations_organizationmembership``:

  1. ``DROP CONSTRAINT ..._pkey`` — instant (drops the id PK index).
  2. ``DROP COLUMN id`` — instant metadata change (no rewrite; column is just
     marked dropped), but still ``ACCESS EXCLUSIVE``.
  3. ``ADD PRIMARY KEY (user_id, organization_id)`` — builds a fresh unique index
     under ``ACCESS EXCLUSIVE`` (Postgres has no ``ADD PRIMARY KEY ... CONCURRENTLY``;
     a concurrent variant would require ``CREATE UNIQUE INDEX CONCURRENTLY`` +
     ``ALTER TABLE ... ADD PRIMARY KEY USING INDEX`` across two transactions). The
     index build scans the whole table and blocks reads + writes on this table for
     its duration.

``organizations_organizationmembership`` is a low-cardinality table (one row per
user-per-org), so the index build is fast and the lock window is short in practice.
The migration is kept ``atomic = True`` (default) so the whole swap is one
transaction — there is never a window where the table has no primary key visible to
other transactions. **Schedule this migration in a low-traffic window** regardless,
because the ``ACCESS EXCLUSIVE`` lock briefly blocks all access to memberships
(which gate every tenant-scoped request). It does NOT lock the three referencing
calendar tables.

Reverse
-------
Restores the previous schema exactly: drop the composite PK, re-add ``id`` as a
``bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY`` (the original Django
``BigAutoField`` shape). ``uniq_membership_user_organization`` is untouched
throughout, so the 3 calendar FKs remain valid across both directions. The state
half reverses to ``RemoveField(pk)`` + ``AddField(id)``.
"""

from django.db import migrations

import common.fields


DROP_ID_PK_AND_ADD_COMPOSITE_PK = """
ALTER TABLE organizations_organizationmembership
  DROP CONSTRAINT organizations_organizationmembership_pkey;
ALTER TABLE organizations_organizationmembership
  DROP COLUMN id;
ALTER TABLE organizations_organizationmembership
  ADD CONSTRAINT organizations_organizationmembership_pkey
  PRIMARY KEY (user_id, organization_id);
"""

REVERSE_RESTORE_ID_PK = """
ALTER TABLE organizations_organizationmembership
  DROP CONSTRAINT organizations_organizationmembership_pkey;
ALTER TABLE organizations_organizationmembership
  ADD COLUMN id bigint GENERATED BY DEFAULT AS IDENTITY;
ALTER TABLE organizations_organizationmembership
  ADD CONSTRAINT organizations_organizationmembership_pkey PRIMARY KEY (id);
"""


class Migration(migrations.Migration):
    """Composite primary key (user_id, organization_id) on OrganizationMembership."""

    dependencies = [
        ("organizations", "0012_organizationinvitation_membership_user_id_and_more"),
        # The composite PK swap touches the constraint the three calendar PROTECT FKs
        # depend on. We keep the unique constraint (Option A) so the FKs are not
        # rebound, but depend on the latest calendar_integration migration so this
        # runs after all three FKs exist — making the dependency explicit and the
        # "all 3 FKs still valid post-swap" guarantee deterministic.
        ("calendar_integration", "0036_calendarmanagementtoken_membership_protect_fk"),
        # CRITICAL ordering: ``public_api.SystemUser.scoped_to_membership`` was a real
        # FK to ``OrganizationMembership.id`` whose DB constraint depends on the *id PK
        # index* (``organizations_organizationmembership_pkey``). Phase 7a's public_api
        # 0008 drops that legacy FK column. If this migration ran first, ``DROP
        # CONSTRAINT ..._pkey`` would fail: "other objects depend on it". Depend on
        # 0008 so the legacy FK is gone before we drop the id PK. (Organizations 0012
        # likewise dropped ``OrganizationInvitation.membership_id`` — its dependency is
        # already covered by the 0012 dependency above.)
        ("public_api", "0008_remove_systemuser_scoped_to_membership_fk_and_more"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(
                    model_name="organizationmembership",
                    name="id",
                ),
                migrations.AddField(
                    model_name="organizationmembership",
                    name="pk",
                    field=common.fields.SafeCompositePrimaryKey(
                        "user",
                        "organization",
                        blank=True,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=DROP_ID_PK_AND_ADD_COMPOSITE_PK,
                    reverse_sql=REVERSE_RESTORE_ID_PK,
                ),
            ],
        ),
    ]
