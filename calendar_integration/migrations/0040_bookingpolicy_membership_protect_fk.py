"""Phase 1 (DB-integrity): raw-SQL composite PROTECT FK for BookingPolicy.membership.

The ``BookingPolicy.membership`` relation is a Django ``ForeignObject`` and
therefore carries **no** DB-level foreign-key constraint. PROTECT delete
semantics are enforced here by a raw-SQL composite FK:

    ALTER TABLE calendar_integration_bookingpolicy
      ADD CONSTRAINT bookingpolicy_membership_protect_fk
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

This mirrors the CalendarOwnership (0026), EventAttendance (0032),
CalendarManagementToken (0036), and ExternalEventChangeRequest.resolved_by (0038)
membership PROTECT FKs exactly.

Deferred PROTECT semantics
--------------------------
The constraint is ``ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED`` so the
referential check fires at **COMMIT**, not at statement time. PROTECT semantics
still hold: deleting a membership while a live ``BookingPolicy`` still references
it as its target raises at commit. Because deleting a ``User`` CASCADEs to their
``OrganizationMembership`` rows, this FK also blocks ``User`` deletion while a
live membership-scoped policy references the membership — an intended behaviour
change.

Why DEFERRABLE (org-cascade reason)
-----------------------------------
A non-deferrable ``RESTRICT`` would break ``Organization`` deletion: Django's
Python cascade collector removes the ``OrganizationMembership`` and the
``BookingPolicy`` in an order it chooses and is **blind** to the ``ForeignObject``
dependency (it carries no DB constraint Django can see). With a DEFERRABLE
INITIALLY DEFERRED constraint the check is postponed to COMMIT, so a
same-transaction cascade that removes **both** the membership and the referencing
policy succeeds, while a membership-only delete (policy still live) still fails at
commit.

Rows with ``membership_user_id IS NULL`` (calendar / calendar-group /
organization-default policies) are not constrained: a composite FK with a NULL
column is not enforced by Postgres (MATCH SIMPLE).

Lock / downtime audit
---------------------
``atomic = False`` so the two ALTER TABLE statements run in separate
transactions:

1. ``ADD CONSTRAINT ... NOT VALID`` takes a brief ``SHARE ROW EXCLUSIVE`` lock
   and does **not** scan the table — existing rows are not validated, so the
   statement returns quickly.
2. ``VALIDATE CONSTRAINT`` scans the table to validate existing rows but takes
   only a ``SHARE UPDATE EXCLUSIVE`` lock, which does not block reads or writes.

The table is new and low-volume at this point; splitting is conservative but
consistent with the established pattern.

Reverse path
------------
``DROP CONSTRAINT bookingpolicy_membership_protect_fk`` — restores the schema to
the post-0039 state (no DB FK on the ForeignObject relation). No orphaned objects
remain.
"""

from django.db import migrations


CONSTRAINT_NAME = "bookingpolicy_membership_protect_fk"

ADD_CONSTRAINT_NOT_VALID = f"""
ALTER TABLE calendar_integration_bookingpolicy
  ADD CONSTRAINT {CONSTRAINT_NAME}
  FOREIGN KEY (membership_user_id, organization_id)
  REFERENCES organizations_organizationmembership (user_id, organization_id)
  ON DELETE NO ACTION
  DEFERRABLE INITIALLY DEFERRED
  NOT VALID;
"""

VALIDATE_CONSTRAINT = f"""
ALTER TABLE calendar_integration_bookingpolicy
  VALIDATE CONSTRAINT {CONSTRAINT_NAME};
"""

DROP_CONSTRAINT = f"""
ALTER TABLE calendar_integration_bookingpolicy
  DROP CONSTRAINT IF EXISTS {CONSTRAINT_NAME};
"""


class Migration(migrations.Migration):
    """Add the raw-SQL composite PROTECT FK on BookingPolicy.membership."""

    atomic = False

    dependencies = [
        ("calendar_integration", "0039_bookingpolicy"),
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
