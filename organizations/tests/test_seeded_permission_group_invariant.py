"""The seeded permission groups must survive a transactional test's flush.

``0028_seed_permission_groups`` writes three ``auth_group`` rows and their
permission links as a **data migration**. Django's ``flush`` -- which every
``@pytest.mark.django_db(transaction=True)`` test runs at teardown -- re-emits
``post_migrate`` (so content types and permissions come back) but re-runs no data
migration, so those three rows do not. pytest-django orders all transactional
tests after all non-transactional ones, so on any worker they run as a single
block in which only the first one sees a seeded database.

Root ``conftest.py``'s ``restore_seeded_permission_groups`` re-establishes the
invariant at the start of every database test. This module is what keeps that
honest: it pins the hazard (``flush`` really does remove them), the repair
(``_reseed_permission_groups`` really does put back the *whole* catalog, links
included), and the wiring (the repair is autouse, so nothing has to remember it).

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

from conftest import _reseed_permission_groups
from organizations.permission_catalog import GROUP_PERMISSIONS


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


@pytest.mark.django_db(transaction=True)
class TestTheSeededGroupInvariant:
    def test_flush_removes_them_and_the_conftest_repair_restores_the_whole_catalog(self):
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
            "`restore_seeded_permission_groups` is now dead weight and should go "
            "-- but check that before deleting it."
        )

        _reseed_permission_groups()

        # The permission links matter as much as the group rows: every
        # authorization check in the codebase is `user.has_perm(...)`, which
        # resolves through them, so three empty groups would restore the
        # `MigrationExecutor` tests while leaving every permission check silently
        # denying.
        assert _live_groups() == _catalog()

    def test_the_repair_is_autouse_so_no_test_has_to_remember_it(self, request):
        """The repair only works because nothing opts into it.

        Its consumers are unrelated to each other and to whichever test flushed --
        the permission classes, ``assign_membership_groups``, and the ``finally``
        blocks of three ``MigrationExecutor`` modules across two apps. Dropping
        ``autouse`` (or renaming the fixture) would restore the original failure
        with no local signal, so the wiring is pinned rather than assumed.
        """
        assert "restore_seeded_permission_groups" in request.fixturenames
