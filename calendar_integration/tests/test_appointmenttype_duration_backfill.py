"""``AppointmentType.duration``: the 0058 backfill migration (Phase 7 follow-up
of REST_CODE_GATED_SCHEDULING).

``CalendarPermissionService`` fails closed on a null ``duration`` for a
publicly-scheduling appointment type -- see 0051's ``duration`` help_text.
0058 backfills ``duration = timedelta(minutes=30)`` onto every pre-existing
row with a NULL duration, public and private alike (see
``calendar_integration/migrations/_0058_backfill_helpers.py``'s module
docstring for why "private too" is deliberate, not an oversight).

Everything here addresses the model by the name it had then --
``CalendarGroup``, table ``calendar_integration_calendargroup``, helper
``backfill_calendargroup_duration``. 0061 renames it to ``AppointmentType``,
and a frozen migration keeps the vocabulary it shipped with.

That rename is also why both classes below step the database back to 0057
and work through raw SQL. The helper's ``UPDATE`` names
``calendar_integration_calendargroup`` as a string literal, so it only
resolves at a schema that still has that table; and the live
``AppointmentType`` model maps to the post-rename table, so it cannot be
used to seed rows the helper will then look for. Before 0061 this file
could use the ORM directly -- 0058 makes no schema change, so the live model
matched at every point -- and the diff that introduced the rename is the
one that took that shortcut away.

Two layers, deliberately separate -- same shape as
``test_calendarmanagementtoken_kind_backfill.py`` and
``test_appointmenttype_public_booking_slug_backfill.py``:

* the backfill helper itself, driven directly against the database;
* the ``0057 -> 0058`` migration chain end to end: forward, reverse (a
  no-op, since 0058 has no schema operations to undo and deliberately does
  not re-null data -- see the migration's own docstring), and re-apply,
  confirming data survives the round trip untouched.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from typing import Any

from django.db import connection

import pytest
from model_bakery import baker

from calendar_integration.migrations._0058_backfill_helpers import (
    BATCH_SIZE,
    backfill_calendargroup_duration,
)
from common.testing.migration_replay import migrate_to, migration_replay, restore_leaf_nodes
from organizations.models import Organization


APP_LABEL = "calendar_integration"
BEFORE_BACKFILL = "0057_calendarmanagementtoken_kind_not_null"
AFTER_BACKFILL = "0058_backfill_calendargroup_duration"

#: The table as it is named at this point in the graph. 0061 renames it to
#: ``calendar_integration_appointmenttype``.
TABLE = "calendar_integration_calendargroup"

THIRTY_MINUTES = datetime.timedelta(minutes=30)


@pytest.fixture
def organization():
    return baker.make(Organization, name="Duration Backfill Test Org")


@pytest.fixture
def at_pre_backfill_schema(transactional_db) -> Iterator[None]:  # noqa: ARG001
    """Step ``calendar_integration`` back to 0057, and put it back afterwards.

    Requests ``transactional_db`` explicitly so this fixture is set up after
    the database is, and therefore torn down *before* it -- the restore has to
    land before pytest-django flushes, which uses the live models and so needs
    the leaf schema.
    """
    migrate_to((APP_LABEL, BEFORE_BACKFILL))
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute(f"DELETE FROM {TABLE}")  # noqa: S608
        # `restore_leaf_nodes` cannot be interrupted by the timeout and retries an
        # autovacuum deadlock -- see `common.testing.migration_replay`.
        restore_leaf_nodes()


def _insert(
    organization_id: int,
    *,
    name: str,
    slug: str,
    duration: datetime.timedelta | None = None,
    accepts_public_scheduling: bool = False,
) -> int:
    """Insert a row straight through SQL, bypassing the ORM.

    Required, not a style choice: the live model maps to the post-0061 table,
    so an ORM ``.create()`` would write somewhere the helper under test never
    looks. Same approach as
    ``test_appointmenttype_public_booking_slug_backfill.py``'s
    ``_insert_appointment_type_without_slug``.
    """
    # ``list[Any]``: psycopg adapts ``timedelta`` to ``interval`` happily, but
    # django-stubs' parameter type does not list it.
    params: list[Any] = [name, organization_id, accepts_public_scheduling, duration, slug]
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO {TABLE}
                (created, modified, meta, name, description, organization_id,
                 accepts_public_scheduling, duration, public_booking_slug)
            VALUES (NOW(), NOW(), '{{}}', %s, '', %s, %s, %s, %s)
            RETURNING id
            """,  # noqa: S608 -- TABLE is a module constant literal, no untrusted input
            params,
        )
        return cursor.fetchone()[0]


def _durations_for(ids: list[int]) -> dict[int, datetime.timedelta | None]:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT id, duration FROM {TABLE} WHERE id = ANY(%s)", [ids])  # noqa: S608
        return dict(cursor.fetchall())


@migration_replay
@pytest.mark.usefixtures("at_pre_backfill_schema")
class TestBackfillHelper:
    """The importable helper, driven directly against a real database."""

    def test_fills_every_null_duration_public_and_private_alike(self, organization):
        public_id = _insert(
            organization.id, name="Public", slug="dur-public", accepts_public_scheduling=True
        )
        private_id = _insert(organization.id, name="Private", slug="dur-private")

        backfill_calendargroup_duration()

        durations = _durations_for([public_id, private_id])
        assert durations[public_id] == THIRTY_MINUTES
        assert durations[private_id] == THIRTY_MINUTES

    def test_appointment_type_with_existing_duration_is_left_untouched(self, organization):
        already_set_id = _insert(
            organization.id,
            name="Already set",
            slug="dur-already-set",
            duration=datetime.timedelta(seconds=45),
        )
        needs_fill_id = _insert(organization.id, name="Needs fill", slug="dur-needs-fill")

        backfill_calendargroup_duration()

        durations = _durations_for([already_set_id, needs_fill_id])
        assert durations[already_set_id] == datetime.timedelta(seconds=45), (
            "a row that already has a duration must never be overwritten"
        )
        assert durations[needs_fill_id] == THIRTY_MINUTES

        # Running it again with nothing NULL left is a true no-op.
        backfill_calendargroup_duration()
        assert _durations_for([already_set_id])[already_set_id] == datetime.timedelta(seconds=45)

    def test_rerun_after_partial_failure_fills_only_the_still_null_subset(self, organization):
        ids = [_insert(organization.id, name=f"Row {i}", slug=f"dur-rerun-{i}") for i in range(4)]

        backfill_calendargroup_duration()
        assert all(d == THIRTY_MINUTES for d in _durations_for(ids).values())

        # Simulate a partial failure recovery: null a subset back out, and
        # set one explicitly to a non-default value (simulating a human or
        # another process filling it in the interim). A rerun must fill
        # ONLY the renulled subset, leave the explicitly-set row alone, and
        # leave the row that was already correctly filled alone too.
        renulled_ids = ids[:2]
        untouched_id = ids[2]
        explicitly_set_id = ids[3]
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {TABLE} SET duration = NULL WHERE id = ANY(%s)",  # noqa: S608
                [renulled_ids],
            )
            cursor.execute(
                f"UPDATE {TABLE} SET duration = %s WHERE id = %s",  # noqa: S608
                [datetime.timedelta(hours=1), explicitly_set_id],
            )

        backfill_calendargroup_duration()

        renulled_durations = _durations_for(renulled_ids)
        assert all(d == THIRTY_MINUTES for d in renulled_durations.values()), renulled_durations

        after = _durations_for([untouched_id, explicitly_set_id])
        assert after[untouched_id] == THIRTY_MINUTES
        assert after[explicitly_set_id] == datetime.timedelta(hours=1), (
            "a value set explicitly between two backfill runs must never be overwritten"
        )

    def test_drain_loop_drains_multiple_batches_and_picks_up_concurrent_insert(
        self, organization, monkeypatch
    ):
        """Forces the drain loop to run more than one iteration by patching
        ``BATCH_SIZE`` down to 2, and simulates an old pod's concurrent
        ``INSERT`` landing between the first and second batch -- the exact
        Render deploy-window race ``_0058_backfill_helpers.py``'s "Drain
        loop" section defends against. Same shape as
        ``test_calendarmanagementtoken_kind_backfill.py``'s equivalent test.
        """
        from calendar_integration.migrations import _0058_backfill_helpers as helpers

        monkeypatch.setattr(helpers, "BATCH_SIZE", 2)

        ids = [_insert(organization.id, name=f"Drain {i}", slug=f"dur-drain-{i}") for i in range(5)]

        real_cursor = connection.cursor
        call_count = {"n": 0}
        late_id: dict[str, int] = {}

        def _patched_cursor(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2 and "id" not in late_id:
                # Simulate an old pod's concurrent INSERT landing mid-drain,
                # between the first and second batch. Uses ``real_cursor``
                # directly (not the patched ``connection.cursor``) to avoid
                # recursing back into this wrapper.
                with real_cursor() as c:
                    c.execute(
                        f"""
                        INSERT INTO {TABLE}
                            (created, modified, meta, name, description, organization_id,
                             accepts_public_scheduling, public_booking_slug)
                        VALUES (NOW(), NOW(), '{{}}', 'Concurrent', '', %s, false, %s)
                        RETURNING id
                        """,  # noqa: S608 -- TABLE is a module constant literal, no untrusted input
                        [organization.id, "concurrent-insert-slug"],
                    )
                    late_id["id"] = c.fetchone()[0]
            return real_cursor(*args, **kwargs)

        monkeypatch.setattr(connection, "cursor", _patched_cursor)

        helpers.backfill_calendargroup_duration()

        # Proves multiple batches actually ran: 6 rows (5 pre-existing + 1
        # inserted mid-drain) at BATCH_SIZE=2 cannot drain in one iteration,
        # and the loop's own termination check (an iteration that updates 0
        # rows) requires one more call beyond that.
        assert call_count["n"] >= 3
        assert "id" in late_id, (
            "the concurrent insert never landed -- test setup is broken, not the backfill"
        )

        durations = _durations_for([*ids, late_id["id"]])
        assert all(duration == THIRTY_MINUTES for duration in durations.values()), durations


def test_default_batch_size_is_500():
    # Sanity check that the module-level default the migration ships with
    # (before any test monkeypatches it) matches the documented value -- so
    # the "drains multiple batches" test above is provably patching a real
    # constant, not asserting against itself.
    assert BATCH_SIZE == 500


@migration_replay
@pytest.mark.django_db(transaction=True)
class TestBackfillMigrationChain:
    """Drives 0057 -> 0058 against a real database via ``MigrationExecutor``.

    0058 has no schema operations (only a ``RunPython`` data write), so
    stepping back to 0057 does not alter the table at all. Rows are still
    seeded through raw SQL rather than the ORM, because 0061 has moved the
    live model to a different table -- see this module's docstring.

    Restores the schema in ``finally`` -- see
    ``common.testing.migration_replay``'s module docstring for why that is
    not optional bookkeeping on a database this test's worker shares with
    every other test.
    """

    def test_forward_reverse_reapply_round_trip(self, organization):
        ids: list[int] = []
        try:
            migrate_to((APP_LABEL, BEFORE_BACKFILL))

            public_id = _insert(
                organization.id,
                name="Chain public",
                slug="chain-public",
                accepts_public_scheduling=True,
            )
            private_id = _insert(organization.id, name="Chain private", slug="chain-private")
            already_set_id = _insert(
                organization.id,
                name="Chain already set",
                slug="chain-already-set",
                duration=datetime.timedelta(seconds=45),
            )
            ids = [public_id, private_id, already_set_id]

            # --- Forward through 0058: every pre-existing NULL-duration row,
            # public and private, must come out at 30 minutes. The row that
            # already had a duration must be untouched. ---
            migrate_to((APP_LABEL, AFTER_BACKFILL))

            first_pass = _durations_for(ids)
            assert first_pass[public_id] == THIRTY_MINUTES
            assert first_pass[private_id] == THIRTY_MINUTES
            assert first_pass[already_set_id] == datetime.timedelta(seconds=45)

            # --- Reverse to 0057: RunPython.noop, and no schema operations
            # to undo -- data must be completely untouched. ---
            migrate_to((APP_LABEL, BEFORE_BACKFILL))

            assert _durations_for(ids) == first_pass, (
                "reversing 0058 must leave every duration value exactly as it was"
            )

            # --- Re-apply forward once more -- must apply cleanly a second
            # time, and (since every row is already non-NULL) leave data
            # exactly as it was. ---
            migrate_to((APP_LABEL, AFTER_BACKFILL))

            assert _durations_for(ids) == first_pass
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {TABLE} WHERE id = ANY(%s)", [ids])  # noqa: S608
            # `restore_leaf_nodes` cannot be interrupted by the timeout and retries an
            # autovacuum deadlock -- see `common.testing.migration_replay`.
            restore_leaf_nodes()
