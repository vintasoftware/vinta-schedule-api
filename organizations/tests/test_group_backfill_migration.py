"""The two Phase 3 data migrations: the group seed and the membership backfill.

``0028_seed_permission_groups`` creates the three global groups, the four
capability permissions, and the mapping between them.
``0029_backfill_membership_groups`` puts every pre-existing membership in the
groups its ``role`` / ``is_billing_owner`` imply.

Three things are pinned here:

1. **The seed survives a migrate from zero.** ``auth.Permission`` rows are
   created by ``post_migrate``, which fires only after the whole migrate run has
   finished -- so nothing existed for ``0028`` to link groups to, and it has to
   create the permissions itself. The test database every test in this repo runs
   against *is* a database migrated from zero, so the assertions in
   ``TestTheSeedSurvivesAMigrateFromZero`` are that proof, not a simulation of
   it. (``organizations/tests/test_permission_backend.py`` leans on the same
   fact from the other end.)
2. **Every ``role`` x ``is_billing_owner`` combination maps to the right
   groups**, and re-running the backfill changes nothing.
3. **The migration's frozen literals still agree with the live catalog.** The
   migrations deliberately do not import ``organizations.permission_catalog``
   (a data migration must keep meaning what it meant when written), so the two
   copies can drift silently. Both are checked against the same third set of
   literals spelled out here.

The backfill is driven by calling its function directly rather than by running
``MigrationExecutor`` over the shared per-worker database: ``0027``-``0029``
change no schema at all (``AlterModelOptions`` emits no SQL, and both data
migrations only write rows), so the live tables already have the exact shape the
migration ran against. That also keeps this module out of the known
parallel-load flake class that migration-executor tests fall into.
"""

from __future__ import annotations

import importlib

from django.apps import apps as global_apps
from django.contrib.auth.models import Group, Permission
from django.db import connection

import pytest
from model_bakery import baker

from organizations.models import Organization, OrganizationMembership, OrganizationRole
from organizations.permission_catalog import GROUP_PERMISSIONS as CATALOG_GROUP_PERMISSIONS
from payments.models import Subscription
from users.models import User


# Migration module names start with a digit, so they can only be reached through
# ``importlib`` -- the same way ``organizations/tests/test_slug_backfill.py``
# reaches its own.
SEED_MIGRATION = importlib.import_module("organizations.migrations.0028_seed_permission_groups")
BACKFILL_MIGRATION = importlib.import_module(
    "organizations.migrations.0029_backfill_membership_groups"
)


# The literals. Everything below compares against these -- never against the
# module under test's own copy of them.
EXPECTED_GROUP_PERMISSIONS = {
    "organization_admin": {
        "organizations.manage_members",
        "organizations.manage_organization",
        "organizations.manage_branding",
        "payments.manage_billing",
    },
    "organization_billing_owner": {"payments.manage_billing"},
    "organization_member": set(),
}

EXPECTED_PERMISSION_OWNERS = {
    "organizations.manage_organization": ("organizations", "organization"),
    "organizations.manage_branding": ("organizations", "organization"),
    "organizations.manage_members": ("organizations", "organizationmembership"),
    "payments.manage_billing": ("payments", "subscription"),
}


def _labelled(permissions) -> set[str]:
    return {
        f"{app_label}.{codename}"
        for app_label, codename in permissions.values_list("content_type__app_label", "codename")
    }


def _group_names(membership: OrganizationMembership) -> set[str]:
    return set(membership.groups.values_list("name", flat=True))


def _run_backfill() -> None:
    BACKFILL_MIGRATION.backfill_membership_groups(global_apps, connection.schema_editor())


@pytest.mark.django_db
class TestTheSeedSurvivesAMigrateFromZero:
    """``post_migrate`` creates ``auth_permission`` rows *after* every migration
    has run, so on a fresh database ``0028`` finds none and must create its own.
    This test database was built by exactly that path."""

    def test_the_three_groups_exist_with_exactly_the_expected_permissions(self):
        for group_name, expected in EXPECTED_GROUP_PERMISSIONS.items():
            group = Group.objects.get(name=group_name)

            assert _labelled(group.permissions) == expected

    def test_each_permission_hangs_off_the_model_that_owns_the_capability(self):
        for label, (app_label, model) in EXPECTED_PERMISSION_OWNERS.items():
            group = Group.objects.get(name="organization_admin")
            permission = group.permissions.get(codename=label.split(".", 1)[1])

            assert permission.content_type.app_label == app_label
            assert permission.content_type.model == model

    def test_post_migrate_did_not_leave_a_duplicate_of_any_of_them(self):
        """``0028`` creates rows ``django.contrib.auth``'s ``create_permissions``
        would also create. It de-duplicates on ``(content_type, codename)``, so
        a second row would mean the migration wrote a *different* key than the
        one ``Meta.permissions`` declares."""
        for label, (app_label, model) in EXPECTED_PERMISSION_OWNERS.items():
            codename = label.split(".", 1)[1]

            assert (
                Permission.objects.filter(
                    content_type__app_label=app_label,
                    content_type__model=model,
                    codename=codename,
                ).count()
                == 1
            )


@pytest.mark.django_db
class TestTheMigrationsFrozenLiteralsStillMatchTheLiveCatalog:
    """The migrations carry their own copies of these strings on purpose. That
    is only safe if a drift is loud."""

    def test_the_seed_migrations_mapping_matches(self):
        seeded = SEED_MIGRATION.GROUP_PERMISSIONS

        assert {name: set(perms) for name, perms in seeded.items()} == EXPECTED_GROUP_PERMISSIONS

    def test_the_seed_migrations_permission_owners_match(self):
        owners = {
            f"{app_label}.{codename}": (app_label, model)
            for app_label, model, codename, _name in SEED_MIGRATION.PERMISSIONS
        }

        assert owners == EXPECTED_PERMISSION_OWNERS

    def test_the_live_catalog_matches(self):
        assert {
            name: set(perms) for name, perms in CATALOG_GROUP_PERMISSIONS.items()
        } == EXPECTED_GROUP_PERMISSIONS

    def test_the_models_meta_permissions_declare_the_same_codenames(self):
        declared = {
            ("organizations", "organization", codename)
            for codename, _name in Organization._meta.permissions
        }
        declared |= {
            ("organizations", "organizationmembership", codename)
            for codename, _name in OrganizationMembership._meta.permissions
        }
        declared |= {
            ("payments", "subscription", codename)
            for codename, _name in Subscription._meta.permissions
        }

        assert declared == {
            (app_label, model, label.split(".", 1)[1])
            for label, (app_label, model) in EXPECTED_PERMISSION_OWNERS.items()
        }


@pytest.mark.django_db
class TestTheBackfillMapsEveryCombination:
    """``role`` x ``is_billing_owner``, all four cells.

    Memberships are created with ``OrganizationMembership.objects.create``,
    which -- unlike ``OrganizationService`` -- performs no dual-write, so each
    one starts with no groups at all. That is the state a pre-Phase-3 row is in,
    which is what the backfill exists for.
    """

    @staticmethod
    def _membership(organization, *, role, is_billing_owner):
        return OrganizationMembership.objects.create(
            user=baker.make(User),
            organization=organization,
            role=role,
            is_billing_owner=is_billing_owner,
        )

    def test_all_four_combinations(self):
        organization = baker.make(Organization, name="Backfill Co", slug="backfill-co")
        plain = self._membership(organization, role=OrganizationRole.MEMBER, is_billing_owner=False)
        billing_owner = self._membership(
            organization, role=OrganizationRole.MEMBER, is_billing_owner=True
        )
        admin = self._membership(organization, role=OrganizationRole.ADMIN, is_billing_owner=False)
        admin_and_owner = self._membership(
            organization, role=OrganizationRole.ADMIN, is_billing_owner=True
        )

        assert _group_names(plain) == set()

        _run_backfill()

        assert _group_names(plain) == {"organization_member"}
        assert _group_names(billing_owner) == {"organization_billing_owner"}
        assert _group_names(admin) == {"organization_admin"}
        assert _group_names(admin_and_owner) == {
            "organization_admin",
            "organization_billing_owner",
        }

    def test_a_billing_owner_does_not_also_get_the_member_group(self):
        """``organization_member`` is the "no capabilities" marker, so a
        membership that *has* a capability does not carry it. Spelled out
        because "everything else -> organization_member" reads both ways."""
        organization = baker.make(Organization, name="Owner Co", slug="owner-co")
        billing_owner = self._membership(
            organization, role=OrganizationRole.MEMBER, is_billing_owner=True
        )

        _run_backfill()

        assert "organization_member" not in _group_names(billing_owner)

    def test_an_inactive_membership_is_backfilled_like_any_other(self):
        """``is_active`` is a separate axis the mapping does not read. A
        deactivated admin keeps its role today and keeps its group here; the
        gate that ignores it lives in the resolver, not in the group."""
        organization = baker.make(Organization, name="Inactive Co", slug="inactive-co")
        membership = OrganizationMembership.objects.create(
            user=baker.make(User),
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=False,
        )

        _run_backfill()

        assert _group_names(membership) == {"organization_admin"}

    def test_re_running_changes_nothing(self):
        organization = baker.make(Organization, name="Idempotent Co", slug="idempotent-co")
        memberships = [
            self._membership(organization, role=OrganizationRole.MEMBER, is_billing_owner=False),
            self._membership(organization, role=OrganizationRole.MEMBER, is_billing_owner=True),
            self._membership(organization, role=OrganizationRole.ADMIN, is_billing_owner=False),
            self._membership(organization, role=OrganizationRole.ADMIN, is_billing_owner=True),
        ]

        _run_backfill()
        after_first = [_group_names(m) for m in memberships]
        through_rows_after_first = OrganizationMembership.groups.through.objects.count()

        _run_backfill()
        _run_backfill()

        assert [_group_names(m) for m in memberships] == after_first
        assert OrganizationMembership.groups.through.objects.count() == through_rows_after_first

    def test_it_does_not_write_role_or_is_billing_owner(self):
        """The backfill reads those two columns; Phase 6 is what retires them."""
        organization = baker.make(Organization, name="Read Only Co", slug="read-only-co")
        membership = self._membership(
            organization, role=OrganizationRole.ADMIN, is_billing_owner=True
        )

        _run_backfill()
        membership.refresh_from_db()

        assert membership.role == OrganizationRole.ADMIN
        assert membership.is_billing_owner is True

    def test_a_group_a_membership_already_held_is_left_alone(self):
        """The backfill is additive. It must not clear an unrelated group."""
        organization = baker.make(Organization, name="Extra Group Co", slug="extra-group-co")
        unrelated = Group.objects.create(name="an_unrelated_group")
        membership = self._membership(
            organization, role=OrganizationRole.ADMIN, is_billing_owner=False
        )
        membership.groups.add(unrelated)

        _run_backfill()

        assert _group_names(membership) == {"organization_admin", "an_unrelated_group"}


@pytest.mark.django_db
class TestTheBackfillsReverse:
    def test_it_detaches_only_the_three_seeded_groups(self):
        organization = baker.make(Organization, name="Reverse Co", slug="reverse-co")
        unrelated = Group.objects.create(name="another_unrelated_group")
        membership = OrganizationMembership.objects.create(
            user=baker.make(User), organization=organization, role=OrganizationRole.ADMIN
        )
        membership.groups.add(unrelated)

        _run_backfill()
        assert _group_names(membership) == {"organization_admin", "another_unrelated_group"}

        BACKFILL_MIGRATION.unassign_membership_groups(global_apps, connection.schema_editor())

        assert _group_names(membership) == {"another_unrelated_group"}

    def test_it_is_a_no_op_when_the_groups_are_already_gone(self):
        """The reverse must tolerate ``0028`` having already been reversed.

        Django runs this reverse whenever *anything* downstream of ``0028`` is
        unapplied -- including stepping ``payments`` backwards, because
        ``payments.0022`` is one of ``0028``'s dependencies. A test that walks
        another app's migrations therefore drags this reverse along, and by then
        ``0028``'s own reverse may already have deleted the groups.

        This is the shape that failed on CI: the reverse raised
        ``SeededGroupsMissingError`` and took two unrelated migration tests down
        with it. Nothing to detach is a completed no-op, not an error -- the
        *forward* is the direction that cannot proceed without the groups.
        """
        organization = baker.make(Organization, name="Gone Co", slug="gone-co")
        membership = OrganizationMembership.objects.create(
            user=baker.make(User), organization=organization, role=OrganizationRole.ADMIN
        )
        _run_backfill()
        assert _group_names(membership) == {"organization_admin"}

        # Exactly what ``0028``'s reverse does, and what leaves the database in
        # the state CI hit.
        Group.objects.filter(name__in=list(CATALOG_GROUP_PERMISSIONS)).delete()

        BACKFILL_MIGRATION.unassign_membership_groups(global_apps, connection.schema_editor())

        assert _group_names(membership) == set()

    def test_the_forward_still_refuses_when_the_groups_are_missing(self):
        """The tolerance above is one-directional, and this is the control for it.

        Forward is about to assign memberships to groups; a missing group means
        ``0028`` did not do its job, and silently assigning nothing would leave
        every membership ungrouped and ``billing_recipients`` returning no one.
        """
        Group.objects.filter(name__in=list(CATALOG_GROUP_PERMISSIONS)).delete()

        with pytest.raises(BACKFILL_MIGRATION.SeededGroupsMissingError):
            _run_backfill()
