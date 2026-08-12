"""Verifies the ``0023_move_content_types_to_tenancy`` data migration.

Phase 1b of the vinta-django-orgs migration -- see ai-plans/2026-08-12-VINTA_
DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md.

Like ``payments/tests/test_backfill_migration.py``, this calls the migration's
own forward/backward functions directly against the *live* app registry
(``django.apps.apps``) rather than a historical one -- safe here because the
migration only calls ``apps.get_model`` for ``contenttypes.ContentType``,
``auth.Permission``, ``auth.Group``, ``admin.LogEntry``, and the user model,
none of which this phase (or anything between ``0001`` and head) changes the
shape of.

Manipulating real ``django_content_type`` / ``auth_permission`` rows is safe
inside these ``@pytest.mark.django_db`` tests: pytest-django wraps each test
in a transaction that rolls back at teardown, so deleting/recreating the
content types the test database auto-created on migrate never leaks between
tests. The autouse fixture below additionally clears Django's process-level
``ContentType`` cache on teardown -- Django only clears it itself on
``post_migrate`` / ``setting_changed``, so a test that deletes/relabels real
rows would otherwise leave stale cache entries for the next test in this
worker.
"""

from __future__ import annotations

import importlib

from django.apps import apps
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType

import pytest


migration_module = importlib.import_module("tenancy.migrations.0023_move_content_types_to_tenancy")
move_content_types_forward = migration_module.move_content_types_forward
move_content_types_backward = migration_module.move_content_types_backward
TENANCY_MODEL_NAMES = migration_module._TENANCY_MODEL_NAMES  # noqa: SLF001


@pytest.fixture(autouse=True)
def _clear_content_type_cache():
    yield
    ContentType.objects.clear_cache()


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
    """Both an 'organizations'- and a 'tenancy'-labelled content type exist for
    the same model -- simulating a database migrated while the app was
    resolvable under both labels. The *original* ('organizations') row must
    win and keep its id: a duplicate-codename permission on the fresh row is
    merged onto the original (its group/user grant re-pointed, the fresh
    permission row deleted); a permission with no matching codename on the
    fresh row is simply re-pointed at the original; the fresh, now-empty
    content type is deleted last.
    """

    def test_merges_the_fresh_row_into_the_original_and_keeps_its_id(self):
        fresh_content_type = ContentType.objects.get(app_label="tenancy", model="organization")
        original_content_type = ContentType.objects.create(
            app_label="organizations", model="organization"
        )
        original_pk = original_content_type.pk

        target_permission = Permission.objects.filter(content_type=fresh_content_type).first()
        assert target_permission is not None, "expected auto-created permissions on the fresh CT"

        duplicate_fresh_permission = Permission.objects.create(
            content_type=fresh_content_type,
            codename="frob_organization",
            name="Can frob organization",
        )
        original_matching_permission = Permission.objects.create(
            content_type=original_content_type,
            codename="frob_organization",
            name="duplicate of a fresh permission",
        )
        group = Group.objects.create(name="frobbers")
        group.permissions.add(duplicate_fresh_permission)

        move_content_types_forward(apps, None)

        # The fresh content type row is gone; the original survives at the
        # same id, now labelled 'tenancy'.
        assert not ContentType.objects.filter(pk=fresh_content_type.pk).exists()
        original_content_type.refresh_from_db()
        assert original_content_type.pk == original_pk
        assert original_content_type.app_label == "tenancy"

        # The duplicate-codename permission from the fresh row was merged
        # away; the group's grant now points at the surviving,
        # matching-codename permission on the original content type.
        assert not Permission.objects.filter(pk=duplicate_fresh_permission.pk).exists()
        group.refresh_from_db()
        assert group.permissions.filter(pk=original_matching_permission.pk).exists()

        # The fresh permission with no codename match on the original was
        # re-pointed, not deleted.
        assert Permission.objects.filter(
            content_type=original_content_type, codename=target_permission.codename
        ).exists()

    def test_merges_a_users_direct_permission_grant(self, user):
        fresh_content_type = ContentType.objects.get(app_label="tenancy", model="organization")
        original_content_type = ContentType.objects.create(
            app_label="organizations", model="organization"
        )

        duplicate_fresh_permission = Permission.objects.create(
            content_type=fresh_content_type,
            codename="frob_organization",
            name="Can frob organization",
        )
        original_matching_permission = Permission.objects.create(
            content_type=original_content_type,
            codename="frob_organization",
            name="duplicate of a fresh permission",
        )
        user.user_permissions.add(duplicate_fresh_permission)

        move_content_types_forward(apps, None)

        user.refresh_from_db()
        assert user.user_permissions.filter(pk=original_matching_permission.pk).exists()
        assert not user.user_permissions.filter(pk=duplicate_fresh_permission.pk).exists()

    def test_admin_log_entry_on_the_fresh_row_is_repointed(self, user):
        fresh_content_type = ContentType.objects.get(app_label="tenancy", model="organization")
        original_content_type = ContentType.objects.create(
            app_label="organizations", model="organization"
        )
        log_entry = LogEntry.objects.create(
            user_id=user.pk,
            content_type=fresh_content_type,
            object_id="1",
            object_repr="Org",
            action_flag=1,
        )

        move_content_types_forward(apps, None)

        log_entry.refresh_from_db()
        assert log_entry.content_type_id == original_content_type.pk

    def test_idempotent_across_two_runs(self):
        ContentType.objects.get(app_label="tenancy", model="organization")
        original_content_type = ContentType.objects.create(
            app_label="organizations", model="organization"
        )
        Permission.objects.create(
            content_type=original_content_type,
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


@pytest.mark.django_db
class TestMoveContentTypesForwardReverseForwardLoop:
    """Proves the loop `add-migration`'s Verification section mandates (forward
    -> reverse -> forward) never loses the original content-type id, its
    `auth_permission` rows, or its `django_admin_log` linkage -- the defect
    the inverted merge direction was written to remove. See the migration
    module's docstring for the reasoning.
    """

    def test_original_id_permissions_and_admin_log_survive_the_loop(self, user):
        # --- Arrange: simulate the collision shape once, with a real grant
        # and a real admin-log entry pointing at the original row. ---
        fresh_content_type = ContentType.objects.get(app_label="tenancy", model="organization")
        original_content_type = ContentType.objects.create(
            app_label="organizations", model="organization"
        )
        original_pk = original_content_type.pk

        granted_permission = Permission.objects.create(
            content_type=original_content_type,
            codename="frob_organization",
            name="Can frob organization",
        )
        group = Group.objects.create(name="frobbers")
        group.permissions.add(granted_permission)

        log_entry = LogEntry.objects.create(
            user_id=user.pk,
            content_type=original_content_type,
            object_id="1",
            object_repr="Org",
            action_flag=1,
        )

        assert fresh_content_type.pk != original_pk

        # --- forward ---
        move_content_types_forward(apps, None)
        assert ContentType.objects.filter(pk=original_pk, app_label="tenancy").exists()

        # --- reverse ---
        move_content_types_backward(apps, None)
        assert ContentType.objects.filter(pk=original_pk, app_label="organizations").exists()

        # `post_migrate` would ordinarily recreate a fresh 'tenancy' row here;
        # simulate that explicitly since this test drives the migration
        # functions directly rather than through `manage.py migrate`.
        new_fresh_content_type = ContentType.objects.create(
            app_label="tenancy", model="organization"
        )
        assert new_fresh_content_type.pk != original_pk

        # --- forward again: re-enters the collision branch. ---
        move_content_types_forward(apps, None)

        # The original id survived every step.
        assert ContentType.objects.filter(pk=original_pk, app_label="tenancy").exists()
        # The freshly (re-)created row was merged away and deleted, not the
        # original.
        assert not ContentType.objects.filter(pk=new_fresh_content_type.pk).exists()

        # The permission and its group grant are intact.
        granted_permission.refresh_from_db()
        assert granted_permission.content_type_id == original_pk
        group.refresh_from_db()
        assert group.permissions.filter(pk=granted_permission.pk).exists()

        # The admin-log entry still points at the surviving, original id.
        log_entry.refresh_from_db()
        assert log_entry.content_type_id == original_pk
