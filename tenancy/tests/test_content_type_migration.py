"""Verifies the ``0023_move_content_types_to_tenancy`` data migration.

Phase 1b of the vinta-django-orgs migration -- see ai-plans/2026-08-12-VINTA_
DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md.

Like ``payments/tests/test_backfill_migration.py``, this calls the migration's
own forward/backward functions directly against the *live* app registry
(``django.apps.apps``) rather than a historical one -- safe here because the
migration only calls ``apps.get_model`` for ``contenttypes.ContentType``,
``auth.Permission``, ``auth.Group``, and the user model, none of which this
phase (or anything between ``0001`` and head) changes the shape of.

Manipulating real ``django_content_type`` / ``auth_permission`` rows is safe
inside these ``@pytest.mark.django_db`` tests: pytest-django wraps each test
in a transaction that rolls back at teardown, so deleting/recreating the
content types the test database auto-created on migrate never leaks between
tests.
"""

from __future__ import annotations

import importlib

from django.apps import apps
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType

import pytest


migration_module = importlib.import_module("tenancy.migrations.0023_move_content_types_to_tenancy")
move_content_types_forward = migration_module.move_content_types_forward
move_content_types_backward = migration_module.move_content_types_backward
TENANCY_MODEL_NAMES = migration_module._TENANCY_MODEL_NAMES  # noqa: SLF001


@pytest.mark.django_db
class TestMoveContentTypesForwardNoCollision:
    def test_relabels_the_existing_row_in_place(self):
        # Simulate a pre-rename seeded database for `Organization`: only an
        # 'organizations'-labelled content type exists, nothing under
        # 'tenancy' yet.
        ContentType.objects.filter(app_label="tenancy", model="organization").delete()
        old_content_type = ContentType.objects.create(
            app_label="organizations", model="organization"
        )

        move_content_types_forward(apps, None)

        old_content_type.refresh_from_db()
        assert old_content_type.app_label == "tenancy"
        assert not ContentType.objects.filter(
            app_label="organizations", model="organization"
        ).exists()

    def test_every_affected_model_is_moved(self):
        for model_name in TENANCY_MODEL_NAMES:
            ContentType.objects.filter(app_label="tenancy", model=model_name).delete()
            ContentType.objects.create(app_label="organizations", model=model_name)

        move_content_types_forward(apps, None)

        for model_name in TENANCY_MODEL_NAMES:
            assert ContentType.objects.filter(app_label="tenancy", model=model_name).exists()
            assert not ContentType.objects.filter(
                app_label="organizations", model=model_name
            ).exists()

    def test_a_permission_referencing_the_old_row_stays_valid(self):
        ContentType.objects.filter(app_label="tenancy", model="organization").delete()
        old_content_type = ContentType.objects.create(
            app_label="organizations", model="organization"
        )
        permission = Permission.objects.create(
            content_type=old_content_type,
            codename="frob_organization",
            name="Can frob organization",
        )

        move_content_types_forward(apps, None)

        permission.refresh_from_db()
        assert permission.content_type_id == old_content_type.pk
        assert permission.content_type.app_label == "tenancy"


@pytest.mark.django_db
class TestMoveContentTypesForwardNoOp:
    def test_already_moved_database_is_a_clean_no_op(self):
        """The test DB is already at 'tenancy' for all four models with no
        'organizations' counterpart -- the common case for any database that
        was only ever migrated post-rename. Forward must not create or touch
        anything."""
        before = {
            model_name: ContentType.objects.get(app_label="tenancy", model=model_name).pk
            for model_name in TENANCY_MODEL_NAMES
        }

        move_content_types_forward(apps, None)

        for model_name in TENANCY_MODEL_NAMES:
            assert not ContentType.objects.filter(
                app_label="organizations", model=model_name
            ).exists()
            after = ContentType.objects.get(app_label="tenancy", model=model_name)
            assert after.pk == before[model_name]

    def test_idempotent_across_two_runs(self):
        ContentType.objects.filter(app_label="tenancy", model="organization").delete()
        ContentType.objects.create(app_label="organizations", model="organization")

        move_content_types_forward(apps, None)
        move_content_types_forward(apps, None)

        assert ContentType.objects.filter(app_label="tenancy", model="organization").count() == 1
        assert not ContentType.objects.filter(
            app_label="organizations", model="organization"
        ).exists()


@pytest.mark.django_db
class TestMoveContentTypesForwardCollision:
    def test_merges_the_old_row_into_the_existing_tenancy_row(self):
        """Both an 'organizations'- and a 'tenancy'-labelled content type exist
        for the same model -- simulating a database migrated while the app was
        resolvable under both labels. The 'tenancy' row must win: a
        duplicate-codename permission on the old row is merged (its group
        grant re-pointed, the old permission row deleted); a permission with
        no matching codename on the new row is simply re-pointed; the old,
        now-empty content type is deleted.
        """
        new_content_type = ContentType.objects.get(app_label="tenancy", model="organization")
        old_content_type = ContentType.objects.create(
            app_label="organizations", model="organization"
        )

        target_permission = Permission.objects.filter(content_type=new_content_type).first()
        assert target_permission is not None, "expected auto-created permissions on the target CT"

        duplicate_old_permission = Permission.objects.create(
            content_type=old_content_type,
            codename=target_permission.codename,
            name="duplicate of an existing permission",
        )
        unique_old_permission = Permission.objects.create(
            content_type=old_content_type,
            codename="frob_organization",
            name="Can frob organization",
        )
        group = Group.objects.create(name="frobbers")
        group.permissions.add(duplicate_old_permission)

        move_content_types_forward(apps, None)

        # Old content type row is gone.
        assert not ContentType.objects.filter(pk=old_content_type.pk).exists()

        # The duplicate-codename permission was merged away; the group's
        # grant now points at the surviving, matching-codename permission.
        assert not Permission.objects.filter(pk=duplicate_old_permission.pk).exists()
        group.refresh_from_db()
        assert group.permissions.filter(pk=target_permission.pk).exists()

        # The permission with no codename match on the target was re-pointed,
        # not deleted.
        unique_old_permission.refresh_from_db()
        assert unique_old_permission.content_type_id == new_content_type.pk

    def test_merges_a_users_direct_permission_grant(self, user):
        new_content_type = ContentType.objects.get(app_label="tenancy", model="organization")
        old_content_type = ContentType.objects.create(
            app_label="organizations", model="organization"
        )

        target_permission = Permission.objects.filter(content_type=new_content_type).first()
        assert target_permission is not None

        duplicate_old_permission = Permission.objects.create(
            content_type=old_content_type,
            codename=target_permission.codename,
            name="duplicate of an existing permission",
        )
        user.user_permissions.add(duplicate_old_permission)

        move_content_types_forward(apps, None)

        user.refresh_from_db()
        assert user.user_permissions.filter(pk=target_permission.pk).exists()
        assert not user.user_permissions.filter(pk=duplicate_old_permission.pk).exists()

    def test_idempotent_across_two_runs(self):
        ContentType.objects.get(app_label="tenancy", model="organization")
        old_content_type = ContentType.objects.create(
            app_label="organizations", model="organization"
        )
        Permission.objects.create(
            content_type=old_content_type,
            codename="frob_organization",
            name="Can frob organization",
        )

        move_content_types_forward(apps, None)
        move_content_types_forward(apps, None)

        assert not ContentType.objects.filter(
            app_label="organizations", model="organization"
        ).exists()
        assert ContentType.objects.filter(app_label="tenancy", model="organization").count() == 1


@pytest.mark.django_db
class TestMoveContentTypesBackward:
    def test_reverse_relabels_back_to_organizations_when_no_collision(self):
        move_content_types_backward(apps, None)

        for model_name in TENANCY_MODEL_NAMES:
            assert ContentType.objects.filter(app_label="organizations", model=model_name).exists()
            assert not ContentType.objects.filter(app_label="tenancy", model=model_name).exists()

    def test_reverse_is_idempotent(self):
        move_content_types_backward(apps, None)
        move_content_types_backward(apps, None)

        assert (
            ContentType.objects.filter(app_label="organizations", model="organization").count() == 1
        )

    def test_reverse_no_ops_when_an_organizations_row_already_exists(self):
        pre_existing = ContentType.objects.create(app_label="organizations", model="organization")

        move_content_types_backward(apps, None)

        # The real 'tenancy' row is untouched (not merged, not deleted) and
        # no second 'organizations' row was created.
        assert ContentType.objects.filter(app_label="tenancy", model="organization").exists()
        assert (
            ContentType.objects.filter(app_label="organizations", model="organization").get()
            == pre_existing
        )

    def test_forward_then_reverse_round_trips_in_the_no_collision_case(self):
        ContentType.objects.filter(app_label="tenancy", model="organization").delete()
        ContentType.objects.create(app_label="organizations", model="organization")

        move_content_types_forward(apps, None)
        assert ContentType.objects.filter(app_label="tenancy", model="organization").exists()

        move_content_types_backward(apps, None)
        assert ContentType.objects.filter(app_label="organizations", model="organization").exists()
        assert not ContentType.objects.filter(app_label="tenancy", model="organization").exists()
