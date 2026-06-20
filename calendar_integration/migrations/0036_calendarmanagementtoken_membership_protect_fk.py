"""Phase 6 cutover (DB-integrity half): raw-SQL composite PROTECT FK.

The ``CalendarManagementToken.membership`` relation is a Django ``ForeignObject``
and therefore carries **no** DB-level foreign-key constraint. PROTECT delete
semantics are enforced here by a raw-SQL composite FK:

    ALTER TABLE calendar_integration_calendarmanagementtoken
      ADD CONSTRAINT calmgmttoken_membership_protect_fk
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
Mirrors the CalendarOwnership (0026) and EventAttendance (0032) FKs. The
constraint is ``ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED`` so the
referential check fires at **COMMIT**, not at statement time. PROTECT semantics
still hold: deleting a membership while a live ``CalendarManagementToken`` still
references it raises at commit. Because deleting a ``User`` CASCADEs to their
``OrganizationMembership`` rows, this FK also blocks ``User`` deletion while a
live member token references the membership — an intended behaviour change.

Why DEFERRABLE (org-cascade reason)
-----------------------------------
A non-deferrable ``RESTRICT`` here would break ``Organization`` deletion: Django's
Python cascade collector removes the ``OrganizationMembership`` and the
``CalendarManagementToken`` in an order it chooses and is **blind** to the
``ForeignObject`` dependency (it carries no DB constraint Django can see). If it
deletes the membership first, a non-deferrable RESTRICT would abort the entire
org-deletion transaction. With a DEFERRABLE INITIALLY DEFERRED constraint the
check is postponed to COMMIT, so a same-transaction cascade that removes **both**
the membership and the referencing token succeeds, while a membership-only delete
(token still live) still fails at commit — exactly the PROTECT guarantee we want.
The ``membership`` ForeignObject is itself wired ``on_delete=DO_NOTHING`` (in
``common/fields.py``) so Django's collector never raises ``ProtectedError`` eagerly.

Rows with ``membership_user_id IS NULL`` (external-attendee / null-membership
tokens) are not constrained: a composite FK with a NULL column is not enforced by
Postgres (MATCH SIMPLE).

Lock / downtime audit
---------------------
``atomic = False`` so the two ALTER TABLE statements run in separate
transactions:

1. ``ADD CONSTRAINT ... NOT VALID`` takes a brief ``SHARE ROW EXCLUSIVE`` lock
   and does **not** scan the table — existing rows are not validated, so the
   statement returns quickly.
2. ``VALIDATE CONSTRAINT`` scans the table to validate existing rows but takes
   only a ``SHARE UPDATE EXCLUSIVE`` lock, which does not block reads or writes.

Reverse
-------
``DROP CONSTRAINT IF EXISTS calmgmttoken_membership_protect_fk`` — restores the
schema to the post-0035 state (no DB FK on the ForeignObject relation).
"""

from django.db import migrations


CONSTRAINT_NAME = "calmgmttoken_membership_protect_fk"

ADD_CONSTRAINT_NOT_VALID = f"""
ALTER TABLE calendar_integration_calendarmanagementtoken
  ADD CONSTRAINT {CONSTRAINT_NAME}
  FOREIGN KEY (membership_user_id, organization_id)
  REFERENCES organizations_organizationmembership (user_id, organization_id)
  ON DELETE NO ACTION
  DEFERRABLE INITIALLY DEFERRED
  NOT VALID;
"""

VALIDATE_CONSTRAINT = f"""
ALTER TABLE calendar_integration_calendarmanagementtoken
  VALIDATE CONSTRAINT {CONSTRAINT_NAME};
"""

DROP_CONSTRAINT = f"""
ALTER TABLE calendar_integration_calendarmanagementtoken
  DROP CONSTRAINT IF EXISTS {CONSTRAINT_NAME};
"""


class Migration(migrations.Migration):
    """Add the raw-SQL composite PROTECT FK on CalendarManagementToken.membership."""

    atomic = False

    dependencies = [
        ("calendar_integration", "0035_remove_calendarmanagementtoken_user"),
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
