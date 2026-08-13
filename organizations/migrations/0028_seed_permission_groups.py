"""Seed the three global authorization groups and the four capability permissions.

**Why this migration creates ``auth_permission`` rows itself.**
``Meta.permissions`` does not create anything. The rows are created by
``django.contrib.auth.management.create_permissions``, which is a ``post_migrate``
receiver -- and ``post_migrate`` fires **after the whole migrate run has
finished**, once per app config. On a database migrated from zero there is
therefore no ``auth_permission`` row (and no ``django_content_type`` row) in
existence at the moment a ``RunPython`` step runs, no matter where in the graph
it sits. A seed that assumed otherwise would silently create three empty groups
on a fresh database and only work on a database that had already been migrated
once.

So this migration does what ``create_permissions`` would do, for exactly the
four permissions it needs, using ``get_or_create`` on ``(content_type,
codename)`` -- the same key ``create_permissions`` de-duplicates on. When
``post_migrate`` runs afterwards it finds them and creates nothing.

**Why the strings are literals rather than imports from
``organizations.permission_catalog``.** A data migration has to keep meaning
what it meant when it was written; importing live constants would let a later
rename retroactively change this migration's behaviour (see ``AGENTS.md`` on
data migrations). ``organizations/tests/test_group_backfill_migration.py`` pins
both copies against the same literals so a drift fails a test.

Idempotent in both directions. Forward uses ``get_or_create`` throughout and
``permissions.add(...)`` rather than ``.set(...)``, so re-running changes
nothing and an operator-granted extra permission on one of these groups is not
silently revoked. Reverse deletes the three groups (which is what un-seeds
them); the ``auth_permission`` rows are left alone, because ``post_migrate``
owns those from ``0027`` onward and would recreate them anyway.
"""

from django.db import migrations


# (app_label, model, codename, name) -- frozen copies of the ``Meta.permissions``
# declared in ``organizations/models.py`` and ``payments/models.py``.
PERMISSIONS = [
    (
        "organizations",
        "organization",
        "manage_organization",
        "Can manage the organization's settings",
    ),
    (
        "organizations",
        "organization",
        "manage_branding",
        "Can manage the organization's branding",
    ),
    (
        "organizations",
        "organizationmembership",
        "manage_members",
        "Can manage the organization's members",
    ),
    (
        "payments",
        "subscription",
        "manage_billing",
        "Can manage the organization's billing",
    ),
]

# Group name -> the ``app_label.codename`` permissions it carries.
#
# ``organization_member`` is deliberately empty: it exists so "has a membership
# and no capabilities" is distinguishable from "has no membership at all", and
# so a per-organization group layer has a base to extend later. A permission
# held by every member is indistinguishable from no check at all.
GROUP_PERMISSIONS = {
    "organization_admin": [
        "organizations.manage_members",
        "organizations.manage_organization",
        "organizations.manage_branding",
        "payments.manage_billing",
    ],
    "organization_billing_owner": ["payments.manage_billing"],
    "organization_member": [],
}


def seed_permission_groups(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")
    db_alias = schema_editor.connection.alias

    permissions_by_label = {}
    for app_label, model, codename, name in PERMISSIONS:
        content_type, _ = ContentType.objects.using(db_alias).get_or_create(
            app_label=app_label, model=model
        )
        permission, _ = Permission.objects.using(db_alias).get_or_create(
            content_type=content_type,
            codename=codename,
            defaults={"name": name},
        )
        permissions_by_label[f"{app_label}.{codename}"] = permission

    for group_name, permission_labels in GROUP_PERMISSIONS.items():
        group, _ = Group.objects.using(db_alias).get_or_create(name=group_name)
        # ``add`` rather than ``set``: additive and idempotent, and it does not
        # revoke a grant an operator added on purpose.
        for label in permission_labels:
            group.permissions.add(permissions_by_label[label])


def unseed_permission_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    db_alias = schema_editor.connection.alias

    Group.objects.using(db_alias).filter(name__in=list(GROUP_PERMISSIONS)).delete()


class Migration(migrations.Migration):
    dependencies = [
        # The ``Meta.permissions`` declarations these rows mirror. ``payments``
        # is a genuine cross-app dependency: ``manage_billing`` is declared on
        # ``payments.Subscription``, and seeding it before that declaration
        # exists would leave the catalog and the database disagreeing about
        # where the permission lives.
        ("organizations", "0027_capability_permissions"),
        ("payments", "0022_capability_permissions"),
        # ``auth.Group`` / ``auth.Permission`` and ``contenttypes.ContentType``
        # in their final shapes -- ``0002_remove_content_type_name`` is what
        # makes ``ContentType`` a two-column natural key.
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(seed_permission_groups, unseed_permission_groups),
    ]
