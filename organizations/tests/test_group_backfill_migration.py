"""The two group data migrations: the group seed and the membership backfill.

``0028_seed_permission_groups`` creates the three global groups, the four
capability permissions, and the mapping between them.
``0029_backfill_membership_groups`` puts every pre-existing membership in the
groups its two flat capability columns imply.

Three things are pinned here:

1. **The seed survives a migrate from zero.** ``auth.Permission`` rows are
   created by ``post_migrate``, which fires only after the whole migrate run has
   finished -- so nothing existed for ``0028`` to link groups to, and it has to
   create the permissions itself. ``TestTheSeedSurvivesAMigrateFromZero`` drops
   the three groups and calls ``0028``'s own ``seed_permission_groups`` against
   the live database, then asserts on what that call left behind.

   It used to read the ambient database instead, on the grounds that the test
   database every test in this repo runs against *is* one migrated from zero.
   That reasoning stopped holding when the root ``conftest.py`` registered
   ``vinta_orgs.testing``: its autouse ``seeded_organization_groups`` fixture
   recreates these three groups and their permission links before every test
   with a database (it has to -- a transactional test's flush wipes them for the
   rest of the worker's session), which is exactly the state asserted below. An
   observed-state assertion would now pass with ``0028`` deleted outright.
   Driving the migration is what makes it an assertion about the migration.
2. **Every combination of the two flat columns maps to the right groups**,
   asserted against the migration's own ``target_group_names``. Both columns
   were later dropped, so no live model carries them and the forward cannot be driven
   over rows *from here*; the reverse, which reads no column, still is. The loop
   that applies the mapping to rows -- and with it idempotency, additivity and
   inactive-membership handling -- is driven with ``MigrationExecutor`` in
   ``organizations/tests/test_membership_group_migration_executor.py``, which
   pays the cost this module declines to.
3. **The migration's frozen literals still agree with the live catalog.** The
   migrations deliberately do not import ``organizations.permission_catalog``
   (a data migration must keep meaning what it meant when written), so the two
   copies can drift silently. Both are checked against the same third set of
   literals spelled out here.

The reverse is driven by calling its function directly rather than by running
``MigrationExecutor`` over the shared per-worker database: it only deletes M2M
through rows, so the live tables have the exact shape it ran against. That also
keeps this module out of the known parallel-load flake class that
migration-executor tests fall into.
"""

from __future__ import annotations

import importlib

from django.apps import apps as global_apps
from django.contrib.auth.models import Group, Permission
from django.db import connection

import pytest
from model_bakery import baker

from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import GROUP_PERMISSIONS as CATALOG_GROUP_PERMISSIONS
from organizations.permission_catalog import PERMISSIONS as CATALOG_PERMISSIONS
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


def target(role: str, billing_owner: bool) -> list[str]:
    """The migration's own mapping function, under its published name."""
    return BACKFILL_MIGRATION.target_group_names(role, billing_owner)


def _run_seed() -> None:
    SEED_MIGRATION.seed_permission_groups(global_apps, connection.schema_editor())


@pytest.mark.django_db
class TestTheSeedSurvivesAMigrateFromZero:
    """``post_migrate`` creates ``auth_permission`` rows *after* every migration
    has run, so on a fresh database ``0028`` finds none and must create its own.

    Everything below describes what ``0028``'s own ``seed_permission_groups``
    left behind, not what happens to be in the database when the test starts --
    see this module's docstring for why that distinction is now load-bearing.
    """

    @pytest.fixture(autouse=True)
    def _seeded_by_the_migration(self):
        Group.objects.filter(name__in=list(EXPECTED_GROUP_PERMISSIONS)).delete()

        _run_seed()

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
        """Reads ``0028``'s own frozen ``PERMISSIONS`` -- reading the live catalog here
        would compare it with itself and this class would pin nothing."""
        owners = {
            f"{app_label}.{codename}": (app_label, model)
            for app_label, model, codename, _name in SEED_MIGRATION.PERMISSIONS
        }

        assert owners == EXPECTED_PERMISSION_OWNERS

    def test_the_live_catalogs_permission_owners_match(self):
        """The other copy. ``organizations.permission_catalog.PERMISSIONS`` is what the
        runtime seeder creates rows from, so it can drift from the migration's copy in
        either direction."""
        owners = {
            f"{app_label}.{codename}": (app_label, model)
            for app_label, model, codename, _name in CATALOG_PERMISSIONS
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


class TestTheMappingTheBackfillApplied:
    """Both flat columns, all four cells.

    Asserted against ``target_group_names``, the migration's own pure function,
    rather than by running the backfill over rows. Both columns were later
    dropped, so no live model carries them any more and the only way to build the
    input state is to drive ``MigrationExecutor`` back to ``0028`` -- which would
    put this module into the migration-executor flake class the module header
    exists to stay out of. What this class pins is the *mapping*; the loop that
    applies it -- and the three properties that go with row writing, idempotency,
    additivity and inactive-membership handling -- is pinned over real rows in
    ``organizations/tests/test_membership_group_migration_executor.py``. Nothing
    is left uncovered here on purpose; the split is by cost, not by scope.

    The fourth case this class used to carry, "the backfill does not write
    ``role`` / ``is_billing_owner``", is genuinely obsolete rather than relocated:
    ``0030`` dropped both columns, so there is nothing left to write.
    """

    def test_all_four_combinations(self):
        assert target("member", False) == ["organization_member"]
        assert target("member", True) == ["organization_billing_owner"]
        assert target("admin", False) == ["organization_admin"]
        assert target("admin", True) == [
            "organization_admin",
            "organization_billing_owner",
        ]

    def test_a_billing_owner_does_not_also_get_the_member_group(self):
        """``organization_member`` is the "no capabilities" marker, so a
        membership that *has* a capability does not carry it. Spelled out
        because "everything else -> organization_member" reads both ways."""
        assert "organization_member" not in target("member", True)

    def test_every_cell_names_only_seeded_groups(self):
        """A name the seed did not create would ``KeyError`` in the backfill."""
        for role in ("member", "admin"):
            for billing_owner in (False, True):
                assert set(target(role, billing_owner)) <= set(EXPECTED_GROUP_PERMISSIONS)


@pytest.mark.django_db
class TestTheBackfillsReverse:
    """The reverse still runs, and still has to, on any backwards plan.

    It reads no dropped column -- it detaches groups -- so unlike the forward it
    is exercised here directly. The starting state is written with
    ``groups.add`` rather than by running the forward, for the reason in
    ``TestTheMappingTheBackfillApplied``.
    """

    @staticmethod
    def _membership(organization):
        return OrganizationMembership.objects.create(
            user=baker.make(User),
            organization=organization,
        )

    def test_it_detaches_only_the_three_seeded_groups(self):
        organization = baker.make(Organization, name="Reverse Co", slug="reverse-co")
        unrelated = Group.objects.create(name="another_unrelated_group")
        membership = self._membership(organization)
        membership.groups.add(unrelated, Group.objects.get(name="organization_admin"))

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
        membership = self._membership(organization)
        membership.groups.add(Group.objects.get(name="organization_admin"))

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
        The check runs before the membership queryset is touched, which is why
        this still works with the columns gone.
        """
        Group.objects.filter(name__in=list(CATALOG_GROUP_PERMISSIONS)).delete()

        with pytest.raises(BACKFILL_MIGRATION.SeededGroupsMissingError):
            BACKFILL_MIGRATION.backfill_membership_groups(global_apps, connection.schema_editor())
