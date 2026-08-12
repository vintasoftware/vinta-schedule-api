"""Phase 1c: reparent Organization / OrganizationMembership onto the package bases.

``Organization`` now extends ``organizations.models.AbstractOrganization`` and
``OrganizationMembership`` extends ``AbstractOrganizationMembership``, in place of
``common.models.BaseModel``. What that costs and buys, operation by operation:

``meta`` dropped (both models)
    ``BaseModel`` mixed in a ``meta`` JSONField; the package's bases extend
    ``model_utils.TimeStampedModel``, which does not. Verified unread on both
    models before dropping: no ``organization.meta`` / ``membership.meta`` read
    or write anywhere in the repo, no ``meta`` key in either model's serializers,
    admin fieldsets, GraphQL types, virtual models or factories, no ``meta__``
    lookup against either table, and every ``.meta`` hit in the codebase belongs
    to ``payments``, ``calendar_integration`` or ``users``. **Forward-only in
    effect**: the reverse re-adds the column with its ``dict`` default, not the
    JSON that was in it.

``created`` / ``modified`` de-indexed (both models)
    ``BaseModel`` set ``db_index=True`` on both; ``TimeStampedModel`` does not.
    Neither model is queried by timestamp range -- ``created`` is used only as a
    deterministic *ordering* key on already-filtered membership sets
    (``active_for_user``), which an index on the whole table does not help.

``groups`` / ``permissions`` added (membership)
    The two M2Ms the composite primary key made impossible, which
    ``0024_unwind_membership_composite_pk`` cleared the way for. They ship empty
    and nothing reads them until Phase 3.

``organization`` de-indexed, both foreign keys renamed (membership)
    ``AbstractOrganizationMembership`` declares ``db_index=False`` on
    ``organization`` because ``SingleOrganizationModelMixin``'s ``class_prepared``
    receiver replaces that single-column index with the composite
    ``(organization, id)`` added at the end of this migration -- a prefix-equal
    but strictly more useful index (see the receiver's own docstring). The
    ``related_name`` on ``user`` moves from ``organization_memberships`` to the
    base's ``memberships``; state-only, no DDL.

Nothing here touches ``uniq_membership_user_organization``, the unique constraint
the five raw-SQL composite PROTECT FKs bind to. See
``0024_unwind_membership_composite_pk``'s docstring.

Deliberately *not* here: ``Organization.slug``'s NOT NULL. It cannot be applied
until every existing row has one -- see ``0026_backfill_organization_slugs`` and
``0027_organization_slug_not_null``.
"""

import django.db.models.deletion
import django.utils.timezone
import model_utils.fields
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Model-level reparenting onto vinta-django-orgs' abstract bases."""

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("tenancy", "0024_unwind_membership_composite_pk"),
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
            index=models.Index(fields=["organization", "id"], name="organizatio_organiz_5ad970_idx"),
        ),
    ]
