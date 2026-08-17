"""The seeded permission groups must survive a transactional test's flush.

``0028_seed_permission_groups`` writes three ``auth_group`` rows and their
permission links as a **data migration**. Django's ``flush`` -- which every
``@pytest.mark.django_db(transaction=True)`` test runs at teardown -- re-emits
``post_migrate`` (so content types and permissions come back) but re-runs no data
migration, so those three rows do not. pytest-django orders all transactional
tests after all non-transactional ones, so on any worker they run as a single
block in which only the first one sees a seeded database.

**The repair is the package's**, not this repository's:
``vinta_orgs.testing``'s autouse ``seeded_organization_groups`` fixture, enabled
by ``pytest_plugins = ["vinta_orgs.testing"]`` in the root ``conftest.py``, and
pointed at our catalog by ``ORGANIZATION_GROUP_SEEDERS`` in
``vinta_schedule_api/settings/base.py``. Phase 6 of the vinta-django-orgs
migration deleted the repo-owned equivalent that used to live in ``conftest.py``
(see the plan's **Package owns the authorization substrate** Guiding Decision:
a test-support hook a stock package install would also need belongs upstream).

This module is what keeps that honest, and it stays *here* rather than upstream
because what it pins is **our** wiring: that the plugin is registered, that
``ORGANIZATION_GROUP_SEEDERS`` names our seeder rather than the package's
default, and that the catalog restored is ours -- the three groups and the four
capability permissions Phase 3 declared. A stock package install satisfies none
of those, so none of it would pass upstream unchanged.

Costing this wrong is expensive and has already been paid for twice. The loud
symptom is not a missing group -- it is
``organizations.0029_backfill_membership_groups`` raising ``SeededGroupsMissingError``
inside the ``finally`` of a ``MigrationExecutor`` test, which aborts that test's
restore and leaves the worker's database mid-graph. Every later test that writes an
``OrganizationMembership`` then fails with ``IntegrityError: null value in column
"role"`` -- a column no live model has -- in modules with no relationship to the one
that broke it. See the 2026-08-13 correction in
``ai-plans/TRACKING_VINTA_DJANGO_ORGS_MIGRATION.md``.
"""

from django.contrib.auth.models import Group
from django.core.management import call_command

import pytest
from vinta_orgs.testing import organization_group_seeders, reseed_organization_groups

from organizations.permission_catalog import GROUP_PERMISSIONS, seed_organization_groups


def _catalog() -> dict[str, set[str]]:
    return {name: set(labels) for name, labels in GROUP_PERMISSIONS.items()}


def _live_groups() -> dict[str, set[str]]:
    return {
        group.name: {
            f"{permission.content_type.app_label}.{permission.codename}"
            for permission in group.permissions.all()
        }
        for group in Group.objects.filter(name__in=list(GROUP_PERMISSIONS)).prefetch_related(
            "permissions__content_type"
        )
    }


def test_the_configured_seeder_is_ours_and_reads_the_live_catalog():
    """``ORGANIZATION_GROUP_SEEDERS`` must resolve to *our* seeder.

    Left unset, the package reseeds only its own ``organization_owner`` group --
    which this repository does not have -- and the three groups Phase 3 declared
    would stay gone after a flush with nothing red to say so. Pinned as identity
    against the function object rather than as a settings string, so a rename on
    either side fails here.

    The seeder reading ``GROUP_PERMISSIONS`` (head state) rather than
    ``0028``'s frozen literals is the second half of the same requirement: a
    data migration is entitled to stop describing what the code now expects, and
    a repair that reproduced the migration would restore the wrong catalog. The
    two are pinned against each other in
    ``organizations/tests/test_group_backfill_migration.py``.
    """
    assert organization_group_seeders() == [seed_organization_groups]


@pytest.mark.django_db(transaction=True)
class TestTheSeededGroupInvariant:
    def test_flush_removes_them_and_the_package_repair_restores_the_whole_catalog(self):
        """Both halves in one test on purpose.

        Split across two tests it would prove nothing: pytest-xdist's default
        ``--dist load`` scheduler hands out individual tests, so "the next test
        still sees them" can land on a different worker with a different database
        and pass without ever exercising the ordering it claims to.
        """
        assert _live_groups() == _catalog(), "the database did not start seeded"

        # Exactly what a `transaction=True` test's teardown does.
        call_command("flush", verbosity=0, interactive=False)

        assert _live_groups() == {}, (
            "`flush` no longer removes the seeded groups. If Django or "
            "pytest-django started restoring data-migration rows, "
            "`vinta_orgs.testing`'s fixture is now dead weight and the plugin "
            "registration could go -- but check that before deleting it."
        )

        reseed_organization_groups()

        # The permission links matter as much as the group rows: every
        # authorization check in the codebase resolves a permission through
        # them, so three empty groups would restore the `MigrationExecutor`
        # tests while leaving every permission check silently denying.
        assert _live_groups() == _catalog()

    def test_the_repair_is_autouse_so_no_test_has_to_remember_it(self, request):
        """The repair only works because nothing opts into it.

        Its consumers are unrelated to each other and to whichever test flushed --
        the permission classes, ``assign_membership_groups``, and the ``finally``
        blocks of three ``MigrationExecutor`` modules across two apps. Dropping
        the ``pytest_plugins`` registration (or the package dropping ``autouse``)
        would restore the original failure with no local signal, so the wiring is
        pinned rather than assumed.
        """
        assert "seeded_organization_groups" in request.fixturenames
