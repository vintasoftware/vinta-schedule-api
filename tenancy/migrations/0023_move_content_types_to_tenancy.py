# Phase 1b of the vinta-django-orgs migration (see ai-plans/2026-08-12-VINTA_
# DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md).
#
# Phase 1a renamed this app's *code* from 'organizations' to 'tenancy' but did
# not touch `django_content_type` rows -- Django never rewrites an existing
# content type's app_label on an app rename; it only creates a *new* row for
# whatever label the currently-loaded app declares (via the `post_migrate`
# `create_contenttypes` signal). On a database that was migrated under the
# pre-rename app, this leaves the four content type rows for our models
# (Organization, OrganizationMembership, OrganizationInvitation,
# OrganizationBranding) still labelled 'organizations', stranding every
# `auth_permission` row that references them (and anything else with a plain
# FK to `django_content_type`, e.g. `django_admin_log`) under a label nothing
# resolves to anymore.
#
# Idempotent both ways:
#   - No-collision case (the common one -- a fresh-since-rename database
#     never auto-created a 'tenancy' row because nothing ever asked for one
#     under the old label): relabel the existing row in place. Same id, so
#     every FK stays valid with zero further work.
#   - Collision case (a 'tenancy'-labelled row already exists, because some
#     migrate run against this database happened while the app was still
#     resolvable under both names): the 'tenancy' row wins -- it is the one
#     `ContentType.objects.get_for_model` resolves to for every
#     currently-loaded model class. Every `auth_permission` row under the old
#     content type is merged onto the matching-codename permission under the
#     new one (re-pointing any group/user grants first so they survive the
#     merge), or, if no matching-codename permission exists yet, simply
#     re-pointed at the new content type. The old, now-empty content type row
#     is deleted last.
#
# Explicitly out of scope: 'organizationtier' and 'subscriptionplan' (models
# deleted in 0015_remove_subscriptionplan_tier_and_more.py -- any stale
# content type for them predates this app's ownership question and belongs to
# a `remove_stale_contenttypes` pass, not this rename) and 'organizationsite'
# (vinta-django-orgs' own model; its app is not installed until Phase 1c).
from __future__ import annotations

from django.conf import settings
from django.db import migrations


# The four models this app owns as of Phase 1b. Deliberately not the full set
# of every model that has ever lived under the 'organizations' label -- see
# the module docstring above.
_TENANCY_MODEL_NAMES = (
    "organization",
    "organizationmembership",
    "organizationinvitation",
    "organizationbranding",
)


def _merge_permission_grants(apps, old_permission, new_permission) -> None:
    """Re-point every group/user grant from ``old_permission`` onto ``new_permission``.

    Uses historical models exclusively (``apps.get_model``) so this keeps
    working regardless of which app registers ``auth.Group`` or the
    swappable user model.
    """
    Group = apps.get_model("auth", "Group")
    for group in Group.objects.filter(permissions=old_permission):
        group.permissions.remove(old_permission)
        group.permissions.add(new_permission)

    user_app_label, user_model_name = settings.AUTH_USER_MODEL.split(".", 1)
    UserModel = apps.get_model(user_app_label, user_model_name)
    for user in UserModel.objects.filter(user_permissions=old_permission):
        user.user_permissions.remove(old_permission)
        user.user_permissions.add(new_permission)


def _move_or_merge_content_type(apps, model_name: str) -> None:
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")

    old_content_type = ContentType.objects.filter(
        app_label="organizations", model=model_name
    ).first()
    if old_content_type is None:
        # Idempotent: either already moved by a prior run of this migration,
        # or this database never had a content type under the old label for
        # this model at all.
        return

    new_content_type = ContentType.objects.filter(app_label="tenancy", model=model_name).first()

    if new_content_type is None:
        # No collision: relabel the existing row in place. Every FK pointing
        # at it (auth_permission, django_admin_log, ...) stays valid because
        # the row's id never changes.
        old_content_type.app_label = "tenancy"
        old_content_type.save(update_fields=["app_label"])
        return

    # Collision: both an 'organizations'- and a 'tenancy'-labelled row exist
    # for this model. The 'tenancy' row wins -- merge the old one into it.
    for old_permission in Permission.objects.filter(content_type=old_content_type):
        target_permission = Permission.objects.filter(
            content_type=new_content_type, codename=old_permission.codename
        ).first()
        if target_permission is not None:
            _merge_permission_grants(apps, old_permission, target_permission)
            old_permission.delete()
        else:
            old_permission.content_type = new_content_type
            old_permission.save(update_fields=["content_type"])

    old_content_type.delete()


def move_content_types_forward(apps, schema_editor) -> None:
    for model_name in _TENANCY_MODEL_NAMES:
        _move_or_merge_content_type(apps, model_name)


def move_content_types_backward(apps, schema_editor) -> None:
    """Best-effort reverse: relabel 'tenancy' rows back to 'organizations'.

    Not guaranteed lossless -- per the plan's "Pre-launch posture" Guiding
    Decision, a reverse only has to leave a working, idempotent state, not
    perfectly restore pre-forward history. When the forward migration took
    the no-collision branch, this exactly undoes it. When it took the
    collision branch, the merged-away old content type / permission rows are
    gone for good; this recreates a fresh 'organizations'-labelled row (with
    a new id) rather than raising, so the database is left consistent either
    way.
    """
    ContentType = apps.get_model("contenttypes", "ContentType")

    for model_name in _TENANCY_MODEL_NAMES:
        new_content_type = ContentType.objects.filter(app_label="tenancy", model=model_name).first()
        if new_content_type is None:
            continue
        if ContentType.objects.filter(app_label="organizations", model=model_name).exists():
            # Idempotent: an 'organizations'-labelled row already exists
            # (re-running the reverse, or the forward never touched this
            # model). Leave both alone rather than creating a duplicate.
            continue
        new_content_type.app_label = "organizations"
        new_content_type.save(update_fields=["app_label"])


class Migration(migrations.Migration):
    dependencies = [
        ("tenancy", "0022_organization_week_start"),
        ("contenttypes", "0001_initial"),
        ("auth", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(move_content_types_forward, move_content_types_backward),
    ]
