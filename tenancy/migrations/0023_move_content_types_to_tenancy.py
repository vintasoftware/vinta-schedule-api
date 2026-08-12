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
# Idempotent both ways, and the ORIGINAL content-type row (and therefore its
# id) always survives, in both branches below:
#   - No-collision case (the common one -- a fresh-since-rename database
#     never auto-created a 'tenancy' row because nothing ever asked for one
#     under the old label): relabel the existing row in place. Same id, so
#     every FK stays valid with zero further work.
#   - Collision case (a 'tenancy'-labelled row already exists, because some
#     migrate run against this database happened while the app was still
#     resolvable under both names): the *original*, 'organizations'-labelled
#     row wins -- it is the one every pre-existing `auth_permission` grant and
#     `django_admin_log` entry already points at. Every `auth_permission` row
#     under the fresh 'tenancy' content type is merged onto the
#     matching-codename permission under the original one (re-pointing any
#     group/user grants first so they survive the merge), or, if no
#     matching-codename permission exists yet, simply re-pointed at the
#     original content type. Any `django_admin_log` entry that already points
#     at the fresh row is re-pointed at the original one too, defensively --
#     belt-and-suspenders alongside the id-preserving design, since the fresh
#     row is not expected to have accumulated admin history of its own. The
#     fresh, now-empty content type row is deleted last, and the surviving
#     original row is relabelled to 'tenancy' in its place.
#
#   Inverting the merge this way (original row survives, fresh row is
#   discarded) instead of the other way round matters for more than the
#   single collision case: `migrate`'s `post_migrate` signal fires
#   `create_contenttypes` after *every* invocation, including a reverse. A
#   reverse relabels the (id-preserving) 'tenancy' row back to
#   'organizations', and the very next `post_migrate` recreates a fresh
#   'tenancy' row for the same model. A subsequent forward therefore always
#   re-enters the collision branch -- and because that branch always keeps the
#   original id and discards the fresh one, running forward -> reverse ->
#   forward any number of times never loses the original content-type id, its
#   permission grants, or its admin-log linkage.
#
# Explicitly out of scope: 'organizationtier' and 'subscriptionplan' (models
# deleted in 0015_remove_subscriptionplan_tier_and_more.py -- any stale
# content type for them predates this app's ownership question and belongs to
# a `remove_stale_contenttypes` pass, not this rename) and 'organizationsite'
# (vinta-django-orgs' own model; its app is not installed until Phase 1c).
from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.db import migrations


if TYPE_CHECKING:
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.migrations.state import StateApps


# The four models this app owns as of Phase 1b. Deliberately not the full set
# of every model that has ever lived under the 'organizations' label -- see
# the module docstring above.
_TENANCY_MODEL_NAMES = (
    "organization",
    "organizationmembership",
    "organizationinvitation",
    "organizationbranding",
)


def _merge_permission_grants(apps: StateApps, old_permission, new_permission) -> None:
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


def _repoint_admin_log(apps: StateApps, old_content_type, new_content_type) -> None:
    """Re-point any ``django_admin_log`` rows off ``old_content_type`` before it is deleted.

    Defensive only: in the direction this migration merges (the original
    content type survives; the fresh, just-created one is discarded), the
    discarded row is not expected to have accumulated admin history of its
    own. Guarded by ``LookupError`` because ``django.contrib.admin`` is not
    guaranteed to be a resolvable historical model in every project.
    """
    try:
        LogEntry = apps.get_model("admin", "LogEntry")
    except LookupError:
        return
    LogEntry.objects.filter(content_type=old_content_type).update(content_type=new_content_type)


def _move_or_merge_content_type(apps: StateApps, model_name: str) -> None:
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")

    original_content_type = ContentType.objects.filter(
        app_label="organizations", model=model_name
    ).first()
    if original_content_type is None:
        # Idempotent: either already moved by a prior run of this migration,
        # or this database never had a content type under the old label for
        # this model at all.
        return

    fresh_content_type = ContentType.objects.filter(app_label="tenancy", model=model_name).first()

    if fresh_content_type is None:
        # No collision: relabel the existing row in place. Every FK pointing
        # at it (auth_permission, django_admin_log, ...) stays valid because
        # the row's id never changes.
        original_content_type.app_label = "tenancy"
        original_content_type.save(update_fields=["app_label"])
        return

    # Collision: both an 'organizations'- and a 'tenancy'-labelled row exist
    # for this model. The *original* row wins -- every permission on the
    # fresh row is merged onto it (see module docstring for why), the fresh
    # row's admin-log entries (if any) are re-pointed defensively, and the
    # fresh, now-empty row is deleted.
    for fresh_permission in Permission.objects.filter(content_type=fresh_content_type):
        target_permission = Permission.objects.filter(
            content_type=original_content_type, codename=fresh_permission.codename
        ).first()
        if target_permission is not None:
            _merge_permission_grants(apps, fresh_permission, target_permission)
            fresh_permission.delete()
        else:
            fresh_permission.content_type = original_content_type
            fresh_permission.save(update_fields=["content_type"])

    _repoint_admin_log(apps, fresh_content_type, original_content_type)

    # Delete the now-empty fresh row *before* relabelling the original one
    # onto 'tenancy' -- both rows briefly share (app_label, model) otherwise,
    # which trips the unique constraint on django_content_type.
    fresh_content_type.delete()
    original_content_type.app_label = "tenancy"
    original_content_type.save(update_fields=["app_label"])


def move_content_types_forward(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    for model_name in _TENANCY_MODEL_NAMES:
        _move_or_merge_content_type(apps, model_name)


def move_content_types_backward(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Relabel 'tenancy' rows back to 'organizations'.

    Because the forward migration always keeps the *original* content-type id
    (both in the no-collision case, where it is relabelled in place, and in
    the collision case, where the fresh duplicate is the one merged away and
    deleted -- see the module docstring), there is only ever one 'tenancy'
    row per model by the time this runs, and it always carries the original
    id. Relabelling it back to 'organizations' is therefore an exact,
    lossless undo in both cases -- not a best-effort one.
    """
    ContentType = apps.get_model("contenttypes", "ContentType")

    for model_name in _TENANCY_MODEL_NAMES:
        tenancy_content_type = ContentType.objects.filter(
            app_label="tenancy", model=model_name
        ).first()
        if tenancy_content_type is None:
            continue
        if ContentType.objects.filter(app_label="organizations", model=model_name).exists():
            # Idempotent: an 'organizations'-labelled row already exists
            # (re-running the reverse, or the forward never touched this
            # model). Leave both alone rather than creating a duplicate.
            continue
        tenancy_content_type.app_label = "organizations"
        tenancy_content_type.save(update_fields=["app_label"])


class Migration(migrations.Migration):
    dependencies = [
        ("tenancy", "0022_organization_week_start"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(move_content_types_forward, move_content_types_backward),
    ]
