import django.db.models.deletion
from django.db import migrations, models


def backfill_scoped_to_membership_user_id(apps, schema_editor):
    """Copy the scoped membership's user_id from the legacy ``scoped_to_membership_fk_id``.

    The legacy ``scoped_to_membership_fk`` FK stored a reference to
    ``OrganizationMembership.id``. The new ``(organization_id, scoped_to_membership_user_id)``
    join needs the membership's ``user_id`` denormalized onto the SystemUser row.
    NULL stays NULL (organization-wide token).
    """
    SystemUser = apps.get_model("public_api", "SystemUser")
    OrganizationMembership = apps.get_model("organizations", "OrganizationMembership")
    membership_user_by_id = dict(OrganizationMembership.objects.values_list("id", "user_id"))
    for system_user_id, membership_id in SystemUser.objects.filter(
        scoped_to_membership_fk_id__isnull=False
    ).values_list("id", "scoped_to_membership_fk_id"):
        user_id = membership_user_by_id.get(membership_id)
        if user_id is not None:
            SystemUser.objects.filter(id=system_user_id).update(
                scoped_to_membership_user_id=user_id
            )


def noop_reverse(apps, schema_editor):
    """No-op reverse: the legacy FK column is re-added + backfilled by RunSQL."""


# Drop the legacy scoped_to_membership_fk_id column. Reverse re-adds it as a NULLABLE
# column and best-effort backfills by resolving the membership from the retained
# (organization_id, scoped_to_membership_user_id) pair. Re-added nullable to match the
# original (the FK was null=True); a NULL value means organization-wide token.
DROP_SCOPED_FK_COLUMN = """
ALTER TABLE public_api_systemuser
  DROP COLUMN IF EXISTS scoped_to_membership_fk_id;
"""

READD_SCOPED_FK_COLUMN_NULLABLE_AND_BACKFILL = """
ALTER TABLE public_api_systemuser
  ADD COLUMN IF NOT EXISTS scoped_to_membership_fk_id bigint NULL;
UPDATE public_api_systemuser AS su
  SET scoped_to_membership_fk_id = om.id
  FROM organizations_organizationmembership AS om
  WHERE su.scoped_to_membership_user_id IS NOT NULL
    AND om.organization_id = su.organization_id
    AND om.user_id = su.scoped_to_membership_user_id;
"""


class Migration(migrations.Migration):
    """Phase 7a: convert SystemUser.scoped_to_membership off the real FK.

    Replaces the ``scoped_to_membership_fk`` FK (real FK to ``OrganizationMembership.id``)
    with the ``(organization_id, scoped_to_membership_user_id)`` ForeignObject pattern so
    the per-owner-token scoping relation survives Phase 7b's composite-PK swap. NULL still
    means an organization-wide token.
    """

    dependencies = [
        ("organizations", "0012_organizationinvitation_membership_user_id_and_more"),
        ("public_api", "0007_systemuser_scoped_to_membership"),
    ]

    operations = [
        # 1. Add the denormalized scoped_to_membership_user_id column (nullable).
        migrations.AddField(
            model_name="systemuser",
            name="scoped_to_membership_user_id",
            field=models.BigIntegerField(
                blank=True,
                help_text=(
                    "When set, this token may only read/write data belonging to calendars "
                    "owned by this organization membership's user. NULL = organization-wide "
                    "token (legacy default)."
                ),
                null=True,
            ),
        ),
        # 2. Backfill it from the legacy FK's user_id before the FK is dropped.
        migrations.RunPython(backfill_scoped_to_membership_user_id, noop_reverse),
        # 3. Swap the ``scoped_to_membership`` field: state replaces the legacy
        #    ForeignObject (joining on scoped_to_membership_fk) with the (org, user)
        #    ForeignObject and drops the concrete ``scoped_to_membership_fk`` FK from
        #    state; DB drops the legacy ``scoped_to_membership_fk_id`` column (reverse
        #    re-adds it nullable + best-effort backfill).
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(
                    model_name="systemuser",
                    name="scoped_to_membership_fk",
                ),
                migrations.AlterField(
                    model_name="systemuser",
                    name="scoped_to_membership",
                    field=models.ForeignObject(
                        editable=False,
                        from_fields=("scoped_to_membership_user_id", "organization_id"),
                        null=True,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="scoped_system_users",
                        to="organizations.organizationmembership",
                        to_fields=("user_id", "organization_id"),
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=DROP_SCOPED_FK_COLUMN,
                    reverse_sql=READD_SCOPED_FK_COLUMN_NULLABLE_AND_BACKFILL,
                ),
            ],
        ),
    ]
