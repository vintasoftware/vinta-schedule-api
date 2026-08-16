"""The two row-writing halves of ``0029`` and ``0030``, driven over a real database.

Everything here exists because ``0030_drop_role_and_is_billing_owner``
removed the *only* way these two code paths could be reached from the live
models:

* ``0029_backfill_membership_groups`` reads ``OrganizationMembership.role`` and
  ``is_billing_owner``. ``0030`` dropped both columns, so no live model can build
  the input state its batched ``iterator`` + through-row loop consumes.
  ``organizations/tests/test_group_backfill_migration.py`` still pins the pure
  mapping function ``target_group_names`` against literals, which is the right
  test for the mapping -- but it leaves the loop that *applies* the mapping with
  no assertion at all, and ``0029`` still runs on every real database upgrading
  from before ``0030``.
* ``0030_drop_role_and_is_billing_owner``'s ``rename_role_values_to_group_names``
  reads ``OrganizationInvitation.role``, which ``0030`` itself renames. That
  remap is the entire justification for renaming the column rather than dropping
  and re-adding it (see the migration's header: dropping would silently demote
  every pending admin invitation to a member invitation), so leaving it
  unexecuted would mean the argument for the more expensive option was never
  checked.

Both are therefore driven with ``MigrationExecutor`` against the shared
per-worker test database, stepping backwards to the state that still has the
columns and forwards again. ``test_group_backfill_migration.py``'s header
explains why *it* stays out of the migration-executor flake class; this module
is the deliberate exception, kept separate so that choice is visible and so the
cheap assertions there are not dragged into the expensive class here.
``0029``'s own reverse-migration defect -- a reverse nothing executed -- is the
precedent for what an unexecuted migration branch costs.

Every method restores ``executor.loader.graph.leaf_nodes()`` in ``finally``, not
the migration it stepped to: stepping back unapplies *every* later migration in
the app, and restoring only as far as the local target would leave the rest
unapplied for every later test sharing this worker's database.
"""

from __future__ import annotations

import datetime
import importlib

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

import pytest
from model_bakery import baker

from organizations.models import Organization
from users.models import User


APP_LABEL = "organizations"

BEFORE_BACKFILL = (APP_LABEL, "0028_seed_permission_groups")
AFTER_BACKFILL = (APP_LABEL, "0029_backfill_membership_groups")
AFTER_DROP = (APP_LABEL, "0030_drop_role_and_is_billing_owner")

BACKFILL_MIGRATION = importlib.import_module(
    f"{APP_LABEL}.migrations.0029_backfill_membership_groups"
)


def _migrate_to(target: tuple[str, str]):
    """Step the database to ``target`` and hand back that state's historical apps."""
    executor = MigrationExecutor(connection)
    executor.migrate([target])
    executor.loader.build_graph()
    return executor.loader.project_state([target]).apps


def _restore_leaf_nodes() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    executor.loader.build_graph()


def _delete_rows(table: str, ids: list[int]) -> None:
    if not ids:
        return
    with connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {table} WHERE id = ANY(%s)", [ids])  # noqa: S608


@pytest.mark.django_db(transaction=True)
class TestTheBackfillOverRealRows:
    """``0029``'s loop, executed -- not just its mapping function.

    Four properties, all of which had assertions before ``0030`` deleted the
    half of the module that built rows from the two columns:

    * every cell of the mapping reaches the through table;
    * **additivity** -- a group the membership already holds for some unrelated
      reason survives (the loop only ever inserts);
    * **inactive memberships are backfilled too** -- ``is_active`` is not in the
      queryset's filter, and must not be: an inactive membership is reactivated
      by flipping one boolean, and a reactivated admin that lost its groups in
      the upgrade is a silent demotion;
    * **idempotency** -- a second run (a resumed or re-run migration) inserts no
      duplicate through rows, which is what ``ignore_conflicts=True`` buys.
    """

    @staticmethod
    def _group_names(membership_id: int) -> set[str]:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT g.name
                FROM organizations_organizationmembership_groups AS mg
                JOIN auth_group AS g ON g.id = mg.group_id
                WHERE mg.organizationmembership_id = %s
                """,
                [membership_id],
            )
            return {row[0] for row in cursor.fetchall()}

    @staticmethod
    def _through_row_count(membership_ids: list[int]) -> int:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM organizations_organizationmembership_groups "
                "WHERE organizationmembership_id = ANY(%s)",
                [membership_ids],
            )
            return cursor.fetchone()[0]

    def test_the_loop_maps_adds_only_and_reruns_clean(self):
        # Built with the live models *before* stepping back: neither table is
        # touched by the two migrations under test, and the historical
        # ``Organization`` would need every column spelled out by hand.
        organization = baker.make(Organization, name="Backfill Co", slug="backfill-co")
        users = [baker.make(User) for _ in range(5)]

        membership_ids: list[int] = []
        try:
            historical = _migrate_to(BEFORE_BACKFILL)
            Membership = historical.get_model(APP_LABEL, "OrganizationMembership")  # noqa: N806
            Group = historical.get_model("auth", "Group")  # noqa: N806

            # (role, is_billing_owner, is_active) -> the groups 0029 must produce.
            expected = {}
            rows = [
                ("admin", False, True, {"organization_admin"}),
                ("admin", True, True, {"organization_admin", "organization_billing_owner"}),
                ("member", True, True, {"organization_billing_owner"}),
                ("member", False, True, {"organization_member"}),
                # Inactive, and still an admin: the backfill does not filter on
                # ``is_active``, and reactivation must not find a demoted row.
                ("admin", False, False, {"organization_admin"}),
            ]
            for user, (role, is_billing_owner, is_active, want) in zip(users, rows, strict=True):
                membership = Membership.objects.create(
                    user_id=user.pk,
                    organization_id=organization.pk,
                    role=role,
                    is_billing_owner=is_billing_owner,
                    is_active=is_active,
                )
                membership_ids.append(membership.pk)
                expected[membership.pk] = want

            # Additivity: a group held for a reason 0029 knows nothing about. The
            # loop only inserts, so this must still be attached afterwards.
            unrelated = Group.objects.create(name="another_unrelated_group")
            Membership.objects.get(pk=membership_ids[0]).groups.add(unrelated)
            expected[membership_ids[0]] = expected[membership_ids[0]] | {"another_unrelated_group"}

            _migrate_to(AFTER_BACKFILL)

            for membership_id, want in expected.items():
                assert self._group_names(membership_id) == want

            # Idempotency: the migration is documented as resumable, so a second
            # run over an already-assigned table must converge, not duplicate.
            before = self._through_row_count(membership_ids)
            historical_after = _migrate_to(AFTER_BACKFILL)
            BACKFILL_MIGRATION.backfill_membership_groups(
                historical_after, connection.schema_editor()
            )

            assert self._through_row_count(membership_ids) == before
            for membership_id, want in expected.items():
                assert self._group_names(membership_id) == want
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM organizations_organizationmembership_groups "
                    "WHERE organizationmembership_id = ANY(%s)",
                    [membership_ids],
                )
            _delete_rows("organizations_organizationmembership", membership_ids)
            _restore_leaf_nodes()


@pytest.mark.django_db(transaction=True)
class TestTheInvitationValueRemap:
    """``0030``'s ``role`` -> ``group`` value remap, both directions.

    The migration renames the column rather than dropping and re-adding it
    specifically so a pending ``admin`` invitation stays an admin invitation.
    That claim is only worth the extra operation if the ``UPDATE`` that carries
    it actually runs, which is what this executes.
    """

    @staticmethod
    def _values(invitation_ids: list[int], column: str) -> list[str]:
        # ``group`` is a PostgreSQL reserved word, hence the quoting. Read
        # through raw SQL rather than a historical model because the column has
        # two different names on the two sides of the migration under test.
        with connection.cursor() as cursor:
            cursor.execute(
                f'SELECT id, "{column}" FROM organizations_organizationinvitation '  # noqa: S608
                "WHERE id = ANY(%s)",
                [invitation_ids],
            )
            by_id = dict(cursor.fetchall())
        return [by_id[invitation_id] for invitation_id in invitation_ids]

    def test_admin_and_member_survive_the_rename_and_come_back(self):
        organization = baker.make(Organization, name="Invite Co", slug="invite-co")

        invitation_ids: list[int] = []
        try:
            historical = _migrate_to(AFTER_BACKFILL)
            Invitation = historical.get_model(APP_LABEL, "OrganizationInvitation")  # noqa: N806

            expires_at = timezone.now() + datetime.timedelta(days=7)
            for email, role in (("admin@example.com", "admin"), ("member@example.com", "member")):
                invitation = Invitation.objects.create(
                    email=email,
                    organization_id=organization.pk,
                    role=role,
                    expires_at=expires_at,
                )
                invitation_ids.append(invitation.pk)

            assert self._values(invitation_ids, "role") == ["admin", "member"]

            _migrate_to(AFTER_DROP)

            # The whole point of the rename: the admin invitation is still an
            # admin invitation. Dropping the column instead would have left both
            # of these at the field default, ``organization_member``.
            assert self._values(invitation_ids, "group") == [
                "organization_admin",
                "organization_member",
            ]

            # Reverse. Lossless here, unlike the two membership column drops in
            # the same migration -- the header says so, and this is what says it
            # is true.
            _migrate_to(AFTER_BACKFILL)

            assert self._values(invitation_ids, "role") == ["admin", "member"]

            # ...and forward once more, so the round trip is closed rather than
            # merely half-checked.
            _migrate_to(AFTER_DROP)

            assert self._values(invitation_ids, "group") == [
                "organization_admin",
                "organization_member",
            ]
        finally:
            _delete_rows("organizations_organizationinvitation", invitation_ids)
            _restore_leaf_nodes()
