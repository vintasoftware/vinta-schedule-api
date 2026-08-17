"""Reparent ``Organization`` / ``OrganizationMembership`` onto the package's bases.

``Organization`` now extends ``vinta_orgs.models.AbstractOrganization`` and
``OrganizationMembership`` extends ``AbstractOrganizationMembership`` instead of
this project's ``common.models.BaseModel``. Neither model moves: the app label
is still ``organizations`` (the package labels its own app ``vinta_orgs``), so
every table keeps the name Django already derived for it and **no ``db_table``
is pinned anywhere**. ``organizations/tests/test_app_identity.py`` is the
regression gate for that.

What changes, and why
---------------------
* ``meta`` is dropped from both models. ``BaseModel`` gave every model a JSON
  ``meta`` column; only ``payments`` ever read one, never these two.
* ``created`` / ``modified`` lose their indexes. ``BaseModel`` indexed both;
  ``TimeStampedModel`` (what the package's bases extend) does not, and neither
  model is queried by timestamp range.
* ``slug`` becomes ``CharField(max_length=255)`` -- the base's declaration,
  inherited rather than overridden. It stays nullable *here*; ``0025`` fills the
  NULLs in and ``0026`` makes it NOT NULL, because the column cannot be made NOT
  NULL before the rows have values.
* ``groups`` / ``permissions`` many-to-many relations appear, with their two
  through tables. They stay empty and unread until ``0028_seed_permission_groups``
  and ``0029_backfill_membership_groups`` populate them; until then ``role`` and
  ``is_billing_owner`` still back every authorization decision.
* The membership's ``user`` foreign key changes its reverse accessor from
  ``user.organization_memberships`` to ``user.memberships`` -- a Python-level
  rename with no DDL.
* The membership's ``organization`` foreign key drops its single-column index in
  favour of the composite ``(organization, id)`` one added below. That index is
  contributed by the package's ``class_prepared`` receiver rather than declared
  in ``Meta``, and it is a prefix-superset of the one it replaces: every query
  the single-column index could answer, the composite can, and the composite
  additionally lets a paged, organization-scoped read stop at the end of the
  page instead of sorting the tenant's whole set.

Lock audit
----------
Adding and dropping indexes is done non-concurrently, as this migration already
takes ``ACCESS EXCLUSIVE`` on ``organizations_organizationmembership`` for its
column drop and its two many-to-many table creations in the same transaction --
the same precedent ``0023`` sets on the same table, and the memberships table is
small (one row per user per organization).
"""

import django.db.models.deletion
import django.utils.timezone
import model_utils.fields
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("organizations", "0023_unwind_organizationmembership_composite_pk"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="organizationmembership",
            options={"default_manager_name": "objects"},
        ),
        migrations.RemoveField(
            model_name="organization",
            name="meta",
        ),
        migrations.RemoveField(
            model_name="organizationmembership",
            name="meta",
        ),
        migrations.AddField(
            model_name="organizationmembership",
            name="groups",
            field=models.ManyToManyField(
                blank=True, related_name="user_organization_groups", to="auth.group"
            ),
        ),
        migrations.AddField(
            model_name="organizationmembership",
            name="permissions",
            field=models.ManyToManyField(
                blank=True, related_name="user_organization_permissions", to="auth.permission"
            ),
        ),
        migrations.AlterField(
            model_name="organization",
            name="created",
            field=model_utils.fields.AutoCreatedField(
                default=django.utils.timezone.now, editable=False, verbose_name="created"
            ),
        ),
        migrations.AlterField(
            model_name="organization",
            name="modified",
            field=model_utils.fields.AutoLastModifiedField(
                default=django.utils.timezone.now, editable=False, verbose_name="modified"
            ),
        ),
        # Still nullable: 0025 backfills, 0026 tightens. Widening 63 -> 255 is a
        # metadata-only change on PostgreSQL (no table rewrite).
        migrations.AlterField(
            model_name="organization",
            name="slug",
            field=models.CharField(default=None, max_length=255, null=True, unique=True),
        ),
        migrations.AlterField(
            model_name="organizationmembership",
            name="created",
            field=model_utils.fields.AutoCreatedField(
                default=django.utils.timezone.now, editable=False, verbose_name="created"
            ),
        ),
        migrations.AlterField(
            model_name="organizationmembership",
            name="modified",
            field=model_utils.fields.AutoLastModifiedField(
                default=django.utils.timezone.now, editable=False, verbose_name="modified"
            ),
        ),
        migrations.AlterField(
            model_name="organizationmembership",
            name="organization",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="memberships",
                to=settings.ORGANIZATION_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="organizationmembership",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="memberships",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name="organizationmembership",
            index=models.Index(
                fields=["organization", "id"], name="organizatio_organiz_5ad970_idx"
            ),
        ),
    ]
