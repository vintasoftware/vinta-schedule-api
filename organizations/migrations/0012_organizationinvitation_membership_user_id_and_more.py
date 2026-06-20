import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_membership_user_id(apps, schema_editor):
    """Copy the accepted membership's user_id from the legacy ``membership_id`` FK.

    The legacy ``membership`` OneToOneField stored ``membership_id`` referencing
    ``OrganizationMembership.id``. The new ``(organization_id, membership_user_id)``
    join needs the membership's ``user_id`` denormalized onto the invitation row.
    """
    OrganizationInvitation = apps.get_model("organizations", "OrganizationInvitation")
    OrganizationMembership = apps.get_model("organizations", "OrganizationMembership")
    membership_user_by_id = dict(OrganizationMembership.objects.values_list("id", "user_id"))
    for invitation_id, membership_id in OrganizationInvitation.objects.filter(
        membership_id__isnull=False
    ).values_list("id", "membership_id"):
        user_id = membership_user_by_id.get(membership_id)
        if user_id is not None:
            OrganizationInvitation.objects.filter(id=invitation_id).update(
                membership_user_id=user_id
            )


def noop_reverse(apps, schema_editor):
    """No-op reverse: the legacy membership_id column is re-added + backfilled by RunSQL."""


# Drop the legacy OneToOne column. Reverse re-adds ``membership_id`` as a NULLABLE
# column and best-effort backfills it by resolving the membership from the retained
# (organization_id, membership_user_id) pair. Re-added nullable (not NOT NULL) because
# the original OneToOne was nullable anyway; Django's auto-reverse of RemoveField on a
# OneToOne FK would attempt a constraint that does not match the composite-PK target.
DROP_MEMBERSHIP_FK_COLUMN = """
ALTER TABLE organizations_organizationinvitation
  DROP COLUMN IF EXISTS membership_id;
"""

READD_MEMBERSHIP_FK_COLUMN_NULLABLE_AND_BACKFILL = """
ALTER TABLE organizations_organizationinvitation
  ADD COLUMN IF NOT EXISTS membership_id bigint NULL;
UPDATE organizations_organizationinvitation AS oi
  SET membership_id = om.id
  FROM organizations_organizationmembership AS om
  WHERE oi.membership_user_id IS NOT NULL
    AND om.organization_id = oi.organization_id
    AND om.user_id = oi.membership_user_id;
"""


class Migration(migrations.Migration):
    """Phase 7a: convert OrganizationInvitation.membership off the real OneToOne.

    Replaces the ``membership`` OneToOneField (real FK to ``OrganizationMembership.id``)
    with the ``(organization_id, membership_user_id)`` ForeignObject pattern so the
    relation survives Phase 7b's composite-PK swap. OneToOne semantics are preserved by
    a partial unique constraint on ``(organization, membership_user_id)``.
    """

    dependencies = [
        ("organizations", "0011_organizationbranding"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # 1. Add the denormalized membership_user_id column (nullable).
        migrations.AddField(
            model_name="organizationinvitation",
            name="membership_user_id",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        # 2. Backfill it from the legacy membership FK's user_id before the FK is dropped.
        migrations.RunPython(backfill_membership_user_id, noop_reverse),
        # 3. Swap the ``membership`` field: state replaces the OneToOneField with the
        #    (org, user) ForeignObject; DB drops the legacy ``membership_id`` FK column
        #    (reverse re-adds it nullable + best-effort backfill).
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="organizationinvitation",
                    name="membership",
                    field=models.ForeignObject(
                        editable=False,
                        from_fields=("membership_user_id", "organization_id"),
                        null=True,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="invitation",
                        to="organizations.organizationmembership",
                        to_fields=("user_id", "organization_id"),
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=DROP_MEMBERSHIP_FK_COLUMN,
                    reverse_sql=READD_MEMBERSHIP_FK_COLUMN_NULLABLE_AND_BACKFILL,
                ),
            ],
        ),
        # 4. Preserve OneToOne semantics via a partial unique constraint.
        migrations.AddConstraint(
            model_name="organizationinvitation",
            constraint=models.UniqueConstraint(
                condition=models.Q(("membership_user_id__isnull", False)),
                fields=("organization", "membership_user_id"),
                name="uniq_invitation_membership_user_per_org",
            ),
        ),
    ]
