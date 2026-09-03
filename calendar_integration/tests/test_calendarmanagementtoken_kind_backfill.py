"""``CalendarManagementToken.kind``: the 0055 -> 0056 -> 0057 backfill migration
chain (Phase 7 of REST_CODE_GATED_SCHEDULING).

Two layers, deliberately separate -- same shape as
``test_calendargroup_public_booking_slug_backfill.py`` (Phase 3b's own
migration-chain test):

* the backfill helper itself
  (``calendar_integration.migrations._0056_backfill_helpers.
  backfill_calendar_management_token_kind``), driven directly against a real
  database via ``MigrationExecutor`` stepped back to the pre-``kind`` schema,
  so the idempotent-rerun and drain-loop guarantees are actually exercised;
* the ``0055``/``0056``/``0057`` migration chain end to end: forward, reverse,
  re-apply, plus a raw ``INSERT`` proving the deploy-window default works.

Classification fixture set
---------------------------
Seven raw rows, inserted with the exact column shape each real
``create_*_token`` call site (plus a REST-minted and a GraphQL-minted booking
code) leaves behind, so the backfill is proven against realistic data, not a
toy shape:

- owner token (``create_calendar_owner_token``)
- attendee token (``create_attendee_token``)
- external-attendee update token (``create_external_attendee_update_token``)
- external-attendee schedule token (``create_external_attendee_schedule_token``)
- booking code minted through the REST surface (``minted_by_membership_user_id``
  set -- ``create_booking_token(minted_by_user=...)``)
- booking code minted through GraphQL (``minted_by_system_user_id`` set --
  ``create_booking_token(minted_by=...)``)
- a "codeless" booking code with NEITHER set -- the shape Phase 8 will start
  producing routinely. For a PRE-EXISTING row like this one, the backfill
  must reproduce the OLD heuristic's classification exactly (``MANAGEMENT_TOKEN``
  -- the heuristic's known misclassification), because the backfill's job is
  to freeze historical classification, not retroactively fix it. Freshly
  minted rows going forward are unaffected: ``create_booking_token`` now sets
  ``kind=BOOKING_CODE`` explicitly regardless of actor (see
  ``test_calendar_permission_service.py::TestCalendarManagementTokenKindDiscriminator``
  for that forward-going guarantee).
"""

from __future__ import annotations

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

import pytest
from model_bakery import baker

from common.testing.migration_replay import migration_replay, uninterruptible
from organizations.models import Organization, OrganizationMembership


APP_LABEL = "calendar_integration"
BEFORE_ADD_FIELD = "0054_calendargroup_public_booking_slug_unique"
AFTER_ADD_FIELD = "0055_calendarmanagementtoken_kind"
AFTER_BACKFILL = "0056_backfill_calendarmanagementtoken_kind"
AFTER_NOT_NULL = "0057_calendarmanagementtoken_kind_not_null"

TABLE = "calendar_integration_calendarmanagementtoken"


@pytest.fixture
def organization():
    return baker.make(Organization, name="Kind Backfill Test Org")


@pytest.fixture
def another_user_membership(organization):
    """Create a distinct (user, membership) pair -- used repeatedly for the
    different actor rows below, each needs its own user so the composite FK
    on (user_id, organization_id) resolves against a real membership row."""

    def _make() -> OrganizationMembership:
        from users.models import User

        user = User.objects.create(email=f"member-{User.objects.count()}@example.com")
        return OrganizationMembership.objects.create(
            user=user, organization=organization, is_active=True
        )

    return _make


class TestBackfillHelperClassification:
    """The importable helper, driven directly against the real (stepped-back)
    schema -- no migration runner in the loop, matching how the helper module
    documents it can be called directly."""

    def _insert_row(self, *, organization_id: int, **columns) -> int:
        """Insert a CalendarManagementToken row straight through SQL, bypassing
        the ORM. Required here: the LIVE model still declares ``kind``
        regardless of which migration this test has stepped the DATABASE back
        to, so an ORM ``.create()`` would either error (asking about a column
        that transiently does not exist) or rely on ORM internals never
        designed for this mismatch. Same approach as the Phase 3b precedent's
        ``_insert_group_without_slug``.
        """
        column_names = ["organization_id", *columns.keys()]
        placeholders = ", ".join(["%s"] * len(column_names))
        values = [organization_id, *columns.values()]
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {TABLE}
                    (created, modified, meta, token_hash, {", ".join(column_names)})
                VALUES (NOW(), NOW(), '{{}}', %s, {placeholders})
                RETURNING id
                """,  # noqa: S608 -- TABLE is a module constant literal, no untrusted input
                [f"hash-{organization_id}-{len(columns)}-{columns}"[:64], *values],
            )
            return cursor.fetchone()[0]

    def _kinds_for(self, ids: list[int]) -> dict[int, str | None]:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT id, kind FROM {TABLE} WHERE id = ANY(%s)", [ids])  # noqa: S608
            return dict(cursor.fetchall())

    @migration_replay
    @pytest.mark.django_db(transaction=True)
    def test_backfill_matches_old_heuristic_for_every_mint_shape(
        self, organization, another_user_membership
    ):
        owner_membership = another_user_membership()
        attendee_membership = another_user_membership()
        rest_minter_membership = another_user_membership()

        # SystemUser is a public_api model; ExternalAttendee is a
        # calendar_integration model whose OWN table this migration chain
        # never touches. Both created before stepping the
        # calendar_integration_calendarmanagementtoken schema back.
        from calendar_integration.models import ExternalAttendee
        from public_api.models import SystemUser

        system_user = baker.make(SystemUser, organization=organization, is_active=True)
        external_attendee = baker.make(ExternalAttendee, organization=organization)

        row_ids: dict[str, int] = {}
        executor = MigrationExecutor(connection)
        try:
            # --- Step back to BEFORE `kind` exists, and insert one row per
            # realistic create_*_token shape. ---
            executor.migrate([(APP_LABEL, BEFORE_ADD_FIELD)])
            executor.loader.build_graph()

            row_ids["owner"] = self._insert_row(
                organization_id=organization.id,
                membership_user_id=owner_membership.user_id,
            )
            row_ids["attendee"] = self._insert_row(
                organization_id=organization.id,
                membership_user_id=attendee_membership.user_id,
            )
            row_ids["external_attendee_update"] = self._insert_row(
                organization_id=organization.id,
                external_attendee_fk_id=external_attendee.id,
            )
            row_ids["external_attendee_schedule"] = self._insert_row(
                organization_id=organization.id,
                external_attendee_fk_id=external_attendee.id,
            )
            row_ids["rest_booking_code"] = self._insert_row(
                organization_id=organization.id,
                minted_by_membership_user_id=rest_minter_membership.user_id,
            )
            row_ids["graphql_booking_code"] = self._insert_row(
                organization_id=organization.id,
                minted_by_system_user_id=system_user.id,
            )
            row_ids["codeless_historical_booking_code"] = self._insert_row(
                organization_id=organization.id,
            )

            # --- Forward through 0055 (nullable AddField) and 0056 (the
            # backfill) -- every pre-existing row must be classified exactly
            # as CalendarManagementTokenQuerySet.booking_codes()'s pre-Phase-7
            # heuristic would have. ---
            executor = MigrationExecutor(connection)
            executor.migrate([(APP_LABEL, AFTER_BACKFILL)])
            executor.loader.build_graph()

            kinds = self._kinds_for(list(row_ids.values()))
            assert kinds[row_ids["owner"]] == "management_token"
            assert kinds[row_ids["attendee"]] == "management_token"
            assert kinds[row_ids["external_attendee_update"]] == "management_token"
            assert kinds[row_ids["external_attendee_schedule"]] == "management_token"
            assert kinds[row_ids["rest_booking_code"]] == "booking_code"
            assert kinds[row_ids["graphql_booking_code"]] == "booking_code"
            # The heuristic's known blind spot, frozen as history: a
            # pre-existing actor-less row is classified MANAGEMENT_TOKEN,
            # not BOOKING_CODE. This is exactly why Phase 7 exists for
            # FUTURE mints (create_booking_token sets kind explicitly,
            # regardless of actor) -- but the backfill's job is to replicate
            # historical behavior faithfully, not rewrite it.
            assert kinds[row_ids["codeless_historical_booking_code"]] == "management_token"

            # --- Idempotency, proven for real: null two rows back out and
            # re-run the SAME importable helper the migration itself calls
            # (not the migration runner). Untouched rows keep their exact
            # prior values; nulled rows are reclassified identically. ---
            renulled = [row_ids["rest_booking_code"], row_ids["owner"]]
            with connection.cursor() as cursor:
                cursor.execute(f"UPDATE {TABLE} SET kind = NULL WHERE id = ANY(%s)", [renulled])  # noqa: S608

            import importlib

            backfill_helpers = importlib.import_module(
                f"{APP_LABEL}.migrations._0056_backfill_helpers"
            )
            backfill_helpers.backfill_calendar_management_token_kind()

            second_pass = self._kinds_for(list(row_ids.values()))
            assert second_pass == kinds, (
                "re-running the backfill must reproduce identical classifications"
            )

            # Running it again with nothing NULL left is a true no-op.
            backfill_helpers.backfill_calendar_management_token_kind()
            assert self._kinds_for(list(row_ids.values())) == second_pass

            # --- 0057: NOT NULL + DB-level default. ---
            executor = MigrationExecutor(connection)
            executor.migrate([(APP_LABEL, AFTER_NOT_NULL)])
            executor.loader.build_graph()

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT is_nullable, column_default FROM information_schema.columns "
                    f"WHERE table_name = '{TABLE}' AND column_name = 'kind'"  # noqa: S608
                )
                is_nullable, column_default = cursor.fetchone()
            assert is_nullable == "NO"
            assert column_default is not None and "management_token" in column_default

            # Data untouched by the NOT NULL / default step.
            assert self._kinds_for(list(row_ids.values())) == second_pass

            # --- Reverse ONLY 0057 and confirm data is completely untouched:
            # dropping NOT NULL / the default must not touch row values. ---
            executor = MigrationExecutor(connection)
            executor.migrate([(APP_LABEL, AFTER_BACKFILL)])
            executor.loader.build_graph()

            assert self._kinds_for(list(row_ids.values())) == second_pass

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT is_nullable, column_default FROM information_schema.columns "
                    f"WHERE table_name = '{TABLE}' AND column_name = 'kind'"  # noqa: S608
                )
                is_nullable, column_default = cursor.fetchone()
            assert is_nullable == "YES"
            assert column_default is None

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT conname FROM pg_constraint "
                    f"WHERE conrelid = '{TABLE}'::regclass "
                    "AND conname = 'calmgmttoken_kind_not_null_chk'"
                )
                assert cursor.fetchone() is None, (
                    "reversing 0057 must drop the CHECK constraint too -- no orphan"
                )

            # --- Re-apply forward once more (0057 again) -- must apply
            # cleanly a second time. ---
            executor = MigrationExecutor(connection)
            executor.migrate([(APP_LABEL, AFTER_NOT_NULL)])
            executor.loader.build_graph()
            assert self._kinds_for(list(row_ids.values())) == second_pass
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {TABLE} WHERE id = ANY(%s)", [list(row_ids.values())])  # noqa: S608
            # `uninterruptible`: see `common.testing.migration_replay`. The
            # alarm landing inside this restore leaves the worker's database
            # mid-graph and fails every test scheduled after it.
            with uninterruptible():
                executor = MigrationExecutor(connection)
                executor.migrate(executor.loader.graph.leaf_nodes())
                executor.loader.build_graph()


@pytest.mark.django_db
class TestRawInsertOmittingKindColumn:
    """Deploy-window safety net: an INSERT that omits ``kind`` entirely (an
    old-code pod compiled before this column existed) must still succeed and
    land a valid, fail-closed kind -- exercised at the CURRENT (leaf) schema,
    not a stepped-back one, since that is the schema this repo actually runs.
    """

    def test_raw_insert_omitting_kind_gets_fail_closed_default(self, organization):
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {TABLE}
                    (created, modified, meta, organization_id, token_hash, used_at, revoked_at)
                VALUES (NOW(), NOW(), '{{}}', %s, 'raw-insert-test-hash', NULL, NULL)
                RETURNING id, kind
                """,  # noqa: S608 -- TABLE is a module constant literal, no untrusted input
                [organization.id],
            )
            token_id, kind = cursor.fetchone()

        assert kind == "management_token"

        with connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {TABLE} WHERE id = %s", [token_id])  # noqa: S608
