"""Phase 2b cutover (DB-integrity half): raw-SQL composite PROTECT FK.

The ``CalendarOwnership.membership`` relation is a Django ``ForeignObject`` and
therefore carries **no** DB-level foreign-key constraint. PROTECT delete
semantics are enforced here by a raw-SQL composite FK:

    ALTER TABLE calendar_integration_calendarownership
      ADD CONSTRAINT calownership_membership_protect_fk
      FOREIGN KEY (membership_user_id, organization_id)
      REFERENCES organizations_organizationmembership (user_id, organization_id)
      ON DELETE NO ACTION
      DEFERRABLE INITIALLY DEFERRED
      NOT VALID;

The referenced ``(user_id, organization_id)`` columns are exactly the columns of
the ``uniq_membership_user_organization`` unique constraint on
``OrganizationMembership`` (fields ``["user", "organization"]``), which is what
makes them a valid FK target. The column order ``(membership_user_id,
organization_id)`` positionally maps to ``(user_id, organization_id)``.

Deferred PROTECT semantics
--------------------------
This is the first raw-SQL membership PROTECT FK in the project; it is **not** a
pre-existing convention from ``common/fields.py``. The constraint is
``ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED`` so that the referential
check fires at **COMMIT**, not at statement time. PROTECT semantics still hold:
deleting a membership while a live ``CalendarOwnership`` still references it
raises at commit (the ownership's dangling reference is detected then). Because
deleting a ``User`` CASCADEs to their ``OrganizationMembership`` rows, this FK
also blocks ``User`` deletion while a live ``CalendarOwnership`` references the
membership — an intended behaviour change.

Why DEFERRABLE (org-cascade reason)
-----------------------------------
Every other FK on ``calendar_integration_calendarownership`` is
``DEFERRABLE INITIALLY DEFERRED`` / ``NO ACTION``. Using a non-deferrable
``RESTRICT`` here would break ``Organization`` deletion: Django's Python cascade
collector removes the ``OrganizationMembership`` and the ``CalendarOwnership`` in
an order it chooses and is **blind** to the ``ForeignObject`` dependency (it
carries no DB constraint Django can see). If it deletes the membership first, a
non-deferrable RESTRICT would abort the entire org-deletion transaction. With a
DEFERRABLE INITIALLY DEFERRED constraint the check is postponed to COMMIT, so a
same-transaction cascade that removes **both** the membership and the referencing
ownership succeeds, while a membership-only delete (ownership still live) still
fails at commit — exactly the PROTECT guarantee we want.

Rows with ``membership_user_id IS NULL`` (orphans) are not constrained: a
composite FK with a NULL column is not enforced by Postgres (MATCH SIMPLE).

Lock / downtime audit
---------------------
``atomic = False`` so the two ALTER TABLE statements run in separate
transactions:

1. ``ADD CONSTRAINT ... NOT VALID`` takes a brief ``SHARE ROW EXCLUSIVE`` lock
   and does **not** scan the table — existing rows are not validated, so the
   statement returns quickly.
2. ``VALIDATE CONSTRAINT`` scans the table to validate existing rows but takes
   only a ``SHARE UPDATE EXCLUSIVE`` lock, which does not block reads or writes.

Splitting NOT VALID from VALIDATE (and keeping them in separate transactions)
avoids a single long-held strong lock. CalendarOwnership is low-volume, so this
is conservative rather than strictly necessary.

Reverse
-------
``DROP CONSTRAINT calownership_membership_protect_fk`` — restores the schema to
the post-0025 state (no DB FK on the ForeignObject relation). No orphaned objects
remain.
"""

from django.db import migrations


CONSTRAINT_NAME = "calownership_membership_protect_fk"

ADD_CONSTRAINT_NOT_VALID = f"""
ALTER TABLE calendar_integration_calendarownership
  ADD CONSTRAINT {CONSTRAINT_NAME}
  FOREIGN KEY (membership_user_id, organization_id)
  REFERENCES organizations_organizationmembership (user_id, organization_id)
  ON DELETE NO ACTION
  DEFERRABLE INITIALLY DEFERRED
  NOT VALID;
"""

VALIDATE_CONSTRAINT = f"""
ALTER TABLE calendar_integration_calendarownership
  VALIDATE CONSTRAINT {CONSTRAINT_NAME};
"""

DROP_CONSTRAINT = f"""
ALTER TABLE calendar_integration_calendarownership
  DROP CONSTRAINT IF EXISTS {CONSTRAINT_NAME};
"""


class Migration(migrations.Migration):
    """Add the raw-SQL composite PROTECT FK on CalendarOwnership.membership."""

    atomic = False

    dependencies = [
        ("calendar_integration", "0025_remove_calendarownership_user_and_more"),
        ("organizations", "0011_organizationbranding"),
    ]

    operations = [
        migrations.RunSQL(
            sql=ADD_CONSTRAINT_NOT_VALID,
            reverse_sql=DROP_CONSTRAINT,
        ),
        migrations.RunSQL(
            sql=VALIDATE_CONSTRAINT,
            # The constraint is dropped wholesale by the first operation's reverse;
            # there is nothing to "un-validate" here.
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
