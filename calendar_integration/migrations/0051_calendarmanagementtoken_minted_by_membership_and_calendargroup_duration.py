"""Add ``CalendarManagementToken.minted_by_membership`` and ``CalendarGroup.duration``.

Two independent additions landing in the same migration because both are
small, mutually-unrelated schema changes with no shared dependency ordering
concern (see the "Mint attribution" / "Duration pinning -- storage" Guiding
Decisions):

- ``CalendarGroup.duration`` -- a plain nullable ``DurationField`` on
  ``calendar_integration_calendargroup``. No index, no constraint, no
  default; nothing filters on it (every read that needs it already has the
  group row in hand). A nullable column with no default is a metadata-only
  ``ALTER TABLE`` in Postgres -- no table rewrite, no scan.

  This lives on ``CalendarGroup``, NOT on ``CalendarManagementToken`` --
  history correction from an earlier draft of this migration, which added it
  to the token instead. That was wrong in one decisive way: a **codeless**
  public-group booking (``CalendarGroup.accepts_public_scheduling=True``)
  presents no code, so it inherits no per-code pin -- the one booking path
  reachable with no credential was also the one path with no length
  constraint. Duration is a property of the thing being booked (the group),
  not of the invitation to book it (the code). Single-calendar codes carry no
  duration pin at all in this design -- there is no ``Calendar.duration``,
  and pinning per-calendar duration was deliberately dropped rather than
  relocated (there is no codeless single-calendar booking path, so there was
  never an unconstrained hole to close there).
- ``minted_by_membership`` -- an ``OrganizationMembershipForeignKey``
  (``common/fields.py``) on ``CalendarManagementToken``, which contributes a
  concrete ``minted_by_membership_user_id`` ``BigIntegerField`` plus a
  ``ForeignObject`` descriptor joining ``(organization_id,
  minted_by_membership_user_id)`` -> ``OrganizationMembership(organization_id,
  user_id)``. Per that field's own documented contract, the adopting
  migration must add:

  1. A composite index on ``(organization, minted_by_membership_user_id)``
     -- ``calmgmttoken_org_minter_idx``, matching the existing
     ``calmgmttoken_org_member_idx`` convention for the sibling ``membership``
     field (added in 0033). Added with ``AddIndexConcurrently`` here (unlike
     0033's plain ``AddIndex``) per this plan's Risk & Rollout note that the
     index should not take a blocking ``SHARE`` lock if
     ``calendar_integration_calendarmanagementtoken`` is large in production
     -- cheap insurance given ``atomic = False`` is already required below for
     the FK's ``NOT VALID`` / ``VALIDATE`` split.
  2. A raw-SQL composite FK constraint -- the ``ForeignObject`` carries none
     at the DB level; ORM traversal conveniences only.

On-delete semantics: SET NULL, not PROTECT
-------------------------------------------
The existing membership FKs in this app (0026 ``CalendarOwnership``, 0032
``EventAttendance``, 0036 ``CalendarManagementToken.membership``) all
implement PROTECT as ``ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED``.
PROTECT is wrong for this column: it would block deleting any user who has
*ever* minted a booking code, a far more routine event than deleting a user
who owns a calendar or attends an event. This column instead mirrors the
sibling ``minted_by_system_user`` FK's semantics (``on_delete=SET_NULL``) --
deleting the minting user leaves the token row live with attribution cleared,
never blocked.

A bare composite ``ON DELETE SET NULL`` would null **both** referenced
columns, including the NOT NULL tenant key ``organization_id`` --
unacceptable, since that column is this table's own tenancy guarantee.
Postgres 15+ supports a column-list form on ``SET NULL`` that nulls only the
named column:

    ALTER TABLE calendar_integration_calendarmanagementtoken
      ADD CONSTRAINT calmgmttoken_minter_membership_fk
      FOREIGN KEY (minted_by_membership_user_id, organization_id)
      REFERENCES organizations_organizationmembership (user_id, organization_id)
      ON DELETE SET NULL (minted_by_membership_user_id)
      DEFERRABLE INITIALLY DEFERRED
      NOT VALID;

The project runs Postgres 18 locally (``postgres:alpine``,
``/var/lib/postgresql/18/``) and is expected to on Render; this syntax is a
**hard parse error** on Postgres < 15, not a silent degradation -- confirm the
target database's actual major version before this migration ever ships to a
new environment.

Why still DEFERRABLE INITIALLY DEFERRED
----------------------------------------
Same reason as every other membership FK in this app (0026's docstring has
the full explanation): Django's Python cascade collector is blind to
``ForeignObject`` dependencies, so it may delete an ``OrganizationMembership``
row before (or interleaved with) deleting the ``CalendarManagementToken`` rows
that reference it via ``minted_by_membership`` -- in particular during
whole-``Organization`` deletion. SET NULL semantics do not remove this
hazard: the check still needs the reference to resolve validly against
whatever the membership row's state is *at COMMIT*, once the same
transaction's other cascades have settled, not at the moment the collector
happens to touch it. A non-deferrable constraint risks aborting organization
deletion depending on collector ordering; deferring the check to COMMIT does
not.

Rows with ``minted_by_membership_user_id IS NULL`` (every code minted by a
``SystemUser``, or by an internal flow that never sets this column) are
unconstrained: a composite FK with a NULL column is not enforced by Postgres
(MATCH SIMPLE, the only mode Postgres composite FKs support).

Lock / downtime audit
----------------------
``atomic = False`` so the ``NOT VALID`` / ``VALIDATE CONSTRAINT`` statements
run in separate transactions, and so the composite index can use
``CONCURRENTLY``:

1. ``AddField(calendargroup, "duration")`` is a nullable column with no
   default on ``calendar_integration_calendargroup`` -- a metadata-only
   ``ALTER TABLE``, no table rewrite, no scan. Independent of every
   operation below (different table).
2. ``AddField(calendarmanagementtoken, "minted_by_membership_user_id")`` is
   likewise a nullable ``BigIntegerField`` with no default -- metadata-only.
   (The companion ``AddField`` for ``minted_by_membership`` itself is a pure
   ``ForeignObject`` state addition with no DB column of its own.)
3. ``AddIndexConcurrently`` builds the composite index without holding a
   blocking ``SHARE`` lock for the duration of the build.
4. ``ADD CONSTRAINT ... NOT VALID`` takes a brief ``SHARE ROW EXCLUSIVE`` lock
   and does **not** scan the table -- existing rows are not validated, so the
   statement returns quickly.
5. ``VALIDATE CONSTRAINT`` scans the table to validate existing rows but
   takes only a ``SHARE UPDATE EXCLUSIVE`` lock, which does not block reads
   or writes.

Reverse
-------
Drops the constraint, then the index, then all three ``CalendarManagementToken``
fields, then ``CalendarGroup.duration`` -- restoring the schema to the
pre-migration state. Reverting this migration in an environment where any
group has since had its ``duration`` set **silently unpins every group that
had one** (a 30-minute group becomes an any-length group for its
codeless/code-gated booking path) -- a security regression that fails open.
Revoke or otherwise stop relying on group duration pinning before ever
reverting this migration in such an environment, and revert any later phase
that writes to either column first, so nothing is writing to them while they
disappear.
"""

import django.db.models.deletion
from django.conf import settings
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


CONSTRAINT_NAME = "calmgmttoken_minter_membership_fk"

ADD_CONSTRAINT_NOT_VALID = f"""
ALTER TABLE calendar_integration_calendarmanagementtoken
  ADD CONSTRAINT {CONSTRAINT_NAME}
  FOREIGN KEY (minted_by_membership_user_id, organization_id)
  REFERENCES organizations_organizationmembership (user_id, organization_id)
  ON DELETE SET NULL (minted_by_membership_user_id)
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
    """Add CalendarGroup.duration + CalendarManagementToken.minted_by_membership."""

    atomic = False

    dependencies = [
        ("calendar_integration", "0050_unlist_non_default_imported_calendars"),
        # Explicit (not swappable_dependency(settings.ORGANIZATION_MEMBERSHIP_MODEL))
        # to match the 0033 precedent, and because the raw-SQL composite FK below
        # references ``uniq_membership_user_organization`` -- added by 0006 and
        # untouched since -- which this dependency guarantees transitively rather
        # than by accident of the migration graph's chain order.
        ("organizations", "0011_organizationbranding"),
        migrations.swappable_dependency(settings.ORGANIZATION_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="calendargroup",
            name="duration",
            field=models.DurationField(
                blank=True,
                help_text=(
                    "When set, an event booked or rescheduled through this group must span "
                    "exactly this duration. Enforced by CalendarPermissionService. Duration "
                    "pinning lives here rather than on CalendarManagementToken because a "
                    "codeless public-group booking (accepts_public_scheduling=True) presents "
                    "no code, so it inherits no per-code pin -- the group being booked is the "
                    "only place a length constraint can live for that path. A group that "
                    "accepts public scheduling MUST have this set (enforced by "
                    "CalendarGroupService.create_group / update_group, not a DB constraint -- "
                    "pre-existing public groups with no duration are grandfathered at rest and "
                    "refused at booking time instead, fail-closed, by "
                    "CalendarPermissionService). Null is otherwise unpinned, matching every "
                    "restricted group and every group created before this field existed."
                ),
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="calendarmanagementtoken",
            name="minted_by_membership",
            field=models.ForeignObject(
                editable=False,
                from_fields=["minted_by_membership_user_id", "organization_id"],
                null=True,
                on_delete=django.db.models.deletion.DO_NOTHING,
                related_name="minted_booking_codes",
                to=settings.ORGANIZATION_MEMBERSHIP_MODEL,
                to_fields=["user_id", "organization_id"],
            ),
        ),
        migrations.AddField(
            model_name="calendarmanagementtoken",
            name="minted_by_membership_user_id",
            field=models.BigIntegerField(
                blank=True,
                help_text=(
                    "The organization member who minted this booking code through the "
                    "authenticated REST surface, if any. Null for codes minted by a "
                    "SystemUser (see minted_by_system_user) or by internal flows."
                ),
                null=True,
            ),
        ),
        AddIndexConcurrently(
            model_name="calendarmanagementtoken",
            index=models.Index(
                fields=["organization", "minted_by_membership_user_id"],
                name="calmgmttoken_org_minter_idx",
            ),
        ),
        migrations.RunSQL(
            sql=ADD_CONSTRAINT_NOT_VALID,
            reverse_sql=DROP_CONSTRAINT,
        ),
        migrations.RunSQL(
            sql=VALIDATE_CONSTRAINT,
            # The constraint is dropped wholesale by the first RunSQL's reverse;
            # there is nothing to "un-validate" here.
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
