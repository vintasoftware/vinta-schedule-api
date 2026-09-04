"""``CalendarGroup.duration``: the 0058 backfill migration (Phase 7 follow-up
of REST_CODE_GATED_SCHEDULING).

``CalendarPermissionService`` fails closed on a null ``duration`` for a
publicly-scheduling group -- see 0051's ``CalendarGroup.duration`` help_text.
0058 backfills ``duration = timedelta(minutes=30)`` onto every pre-existing
``CalendarGroup`` row with a NULL duration, public and private alike (see
``calendar_integration/migrations/_0058_backfill_helpers.py``'s module
docstring for why "private too" is deliberate, not an oversight).

Two layers, deliberately separate -- same shape as
``test_calendarmanagementtoken_kind_backfill.py`` and
``test_calendargroup_public_booking_slug_backfill.py``:

* the backfill helper itself
  (``calendar_integration.migrations._0058_backfill_helpers.
  backfill_calendargroup_duration``), driven directly against the database.
  Unlike the slug/kind backfills, 0058 makes NO schema change (``duration``
  was already nullable as of 0051 and stays nullable forever -- there is no
  later "make it NOT NULL" migration in this chain), so the schema is
  identical before and after 0058. The LIVE ``CalendarGroup`` model can
  therefore be used directly in these tests, with no need to bypass the ORM
  or step the database back to a schema the live model no longer matches;
* the ``0057 -> 0058`` migration chain end to end: forward, reverse (a
  no-op, since 0058 has no schema operations to undo and deliberately does
  not re-null data -- see the migration's own docstring), and re-apply,
  confirming data survives the round trip untouched.
"""

from __future__ import annotations

import datetime

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

import pytest
from model_bakery import baker

from calendar_integration.migrations._0058_backfill_helpers import (
    BATCH_SIZE,
    backfill_calendargroup_duration,
)
from calendar_integration.models import CalendarGroup
from common.testing.migration_replay import migration_replay, uninterruptible
from organizations.models import Organization


APP_LABEL = "calendar_integration"
BEFORE_BACKFILL = "0057_calendarmanagementtoken_kind_not_null"
AFTER_BACKFILL = "0058_backfill_calendargroup_duration"

TABLE = "calendar_integration_calendargroup"

THIRTY_MINUTES = datetime.timedelta(minutes=30)


@pytest.fixture
def organization():
    return baker.make(Organization, name="Duration Backfill Test Org")


def _durations_for(ids: list[int]) -> dict[int, datetime.timedelta | None]:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT id, duration FROM {TABLE} WHERE id = ANY(%s)", [ids])  # noqa: S608
        return dict(cursor.fetchall())


@pytest.mark.django_db
class TestBackfillHelper:
    """The importable helper, driven directly against a real database."""

    def test_fills_every_null_duration_public_and_private_alike(self, organization):
        public_group = baker.make(
            CalendarGroup,
            organization=organization,
            accepts_public_scheduling=True,
            duration=None,
        )
        private_group = baker.make(
            CalendarGroup,
            organization=organization,
            accepts_public_scheduling=False,
            duration=None,
        )

        backfill_calendargroup_duration()

        public_group.refresh_from_db()
        private_group.refresh_from_db()
        assert public_group.duration == THIRTY_MINUTES
        assert private_group.duration == THIRTY_MINUTES

    def test_group_with_existing_duration_is_left_untouched(self, organization):
        already_set = baker.make(
            CalendarGroup,
            organization=organization,
            duration=datetime.timedelta(seconds=45),
        )
        needs_fill = baker.make(CalendarGroup, organization=organization, duration=None)

        backfill_calendargroup_duration()

        already_set.refresh_from_db()
        needs_fill.refresh_from_db()
        assert already_set.duration == datetime.timedelta(seconds=45), (
            "a row that already has a duration must never be overwritten"
        )
        assert needs_fill.duration == THIRTY_MINUTES

        # Running it again with nothing NULL left is a true no-op.
        backfill_calendargroup_duration()
        already_set.refresh_from_db()
        assert already_set.duration == datetime.timedelta(seconds=45)

    def test_rerun_after_partial_failure_fills_only_the_still_null_subset(self, organization):
        groups = [
            baker.make(CalendarGroup, organization=organization, duration=None) for _ in range(4)
        ]

        backfill_calendargroup_duration()
        for group in groups:
            group.refresh_from_db()
            assert group.duration == THIRTY_MINUTES

        # Simulate a partial failure recovery: null a subset back out, and
        # set one explicitly to a non-default value (simulating a human or
        # another process filling it in the interim). A rerun must fill
        # ONLY the renulled subset, leave the explicitly-set row alone, and
        # leave the row that was already correctly filled alone too.
        renulled_ids = [groups[0].id, groups[1].id]
        untouched = groups[2]
        explicitly_set = groups[3]
        # Raw SQL, not the org-scoped ORM manager: this is test setup
        # simulating an out-of-band write (a partial failure / a human
        # fix-up), not a request-scoped read that should go through
        # organization binding.
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {TABLE} SET duration = NULL WHERE id = ANY(%s)",  # noqa: S608
                [renulled_ids],
            )
        explicitly_set.duration = datetime.timedelta(hours=1)
        explicitly_set.save(update_fields=["duration"])

        backfill_calendargroup_duration()

        renulled_durations = _durations_for(renulled_ids)
        assert all(d == THIRTY_MINUTES for d in renulled_durations.values()), renulled_durations

        untouched.refresh_from_db()
        assert untouched.duration == THIRTY_MINUTES

        explicitly_set.refresh_from_db()
        assert explicitly_set.duration == datetime.timedelta(hours=1), (
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

        groups = [
            baker.make(CalendarGroup, organization=organization, duration=None) for _ in range(5)
        ]

        real_cursor = connection.cursor
        call_count = {"n": 0}
        late_group_id: dict[str, int] = {}

        def _patched_cursor(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2 and "id" not in late_group_id:
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
                        VALUES (NOW(), NOW(), '{{}}', 'Concurrent Group', '', %s, false, %s)
                        RETURNING id
                        """,  # noqa: S608 -- TABLE is a module constant literal, no untrusted input
                        [organization.id, "concurrent-insert-slug"],
                    )
                    late_group_id["id"] = c.fetchone()[0]
            return real_cursor(*args, **kwargs)

        monkeypatch.setattr(connection, "cursor", _patched_cursor)

        helpers.backfill_calendargroup_duration()

        # Proves multiple batches actually ran: 6 rows (5 pre-existing + 1
        # inserted mid-drain) at BATCH_SIZE=2 cannot drain in one iteration,
        # and the loop's own termination check (an iteration that updates 0
        # rows) requires one more call beyond that.
        assert call_count["n"] >= 3
        assert "id" in late_group_id, (
            "the concurrent insert never landed -- test setup is broken, not the backfill"
        )

        all_ids = [group.id for group in groups] + [late_group_id["id"]]
        durations = _durations_for(all_ids)
        assert all(duration == THIRTY_MINUTES for duration in durations.values()), durations

    def test_default_batch_size_is_500(self):
        # Sanity check that the module-level default the migration ships
        # with (before any test monkeypatches it) matches the documented
        # value -- so the "drains multiple batches" test above is provably
        # patching a real constant, not asserting against itself.
        assert BATCH_SIZE == 500


@migration_replay
@pytest.mark.django_db(transaction=True)
class TestBackfillMigrationChain:
    """Drives 0057 -> 0058 against a real database via ``MigrationExecutor``.

    0058 has no schema operations (only a ``RunPython`` data write), so
    stepping back to 0057 does not alter the table at all -- the live
    ``CalendarGroup`` model matches the schema at every point in this test,
    unlike the slug/kind backfill migration chain tests which must bypass
    the ORM to reach schemas the live model no longer describes.

    Restores the schema in ``finally`` -- see
    ``common.testing.migration_replay``'s module docstring for why that is
    not optional bookkeeping on a database this test's worker shares with
    every other test.
    """

    def test_forward_reverse_reapply_round_trip(self, organization):
        ids: list[int] = []
        executor = MigrationExecutor(connection)
        try:
            executor.migrate([(APP_LABEL, BEFORE_BACKFILL)])
            executor.loader.build_graph()

            public_group = baker.make(
                CalendarGroup,
                organization=organization,
                accepts_public_scheduling=True,
                duration=None,
            )
            private_group = baker.make(
                CalendarGroup,
                organization=organization,
                accepts_public_scheduling=False,
                duration=None,
            )
            already_set_group = baker.make(
                CalendarGroup,
                organization=organization,
                duration=datetime.timedelta(seconds=45),
            )
            ids = [public_group.id, private_group.id, already_set_group.id]

            # --- Forward through 0058: every pre-existing NULL-duration row,
            # public and private, must come out at 30 minutes. The row that
            # already had a duration must be untouched. ---
            executor = MigrationExecutor(connection)
            executor.migrate([(APP_LABEL, AFTER_BACKFILL)])
            executor.loader.build_graph()

            first_pass = _durations_for(ids)
            assert first_pass[public_group.id] == THIRTY_MINUTES
            assert first_pass[private_group.id] == THIRTY_MINUTES
            assert first_pass[already_set_group.id] == datetime.timedelta(seconds=45)

            # --- Reverse to 0057: RunPython.noop, and no schema operations
            # to undo -- data must be completely untouched. ---
            executor = MigrationExecutor(connection)
            executor.migrate([(APP_LABEL, BEFORE_BACKFILL)])
            executor.loader.build_graph()

            assert _durations_for(ids) == first_pass, (
                "reversing 0058 must leave every duration value exactly as it was"
            )

            # --- Re-apply forward once more -- must apply cleanly a second
            # time, and (since every row is already non-NULL) leave data
            # exactly as it was. ---
            executor = MigrationExecutor(connection)
            executor.migrate([(APP_LABEL, AFTER_BACKFILL)])
            executor.loader.build_graph()

            assert _durations_for(ids) == first_pass
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {TABLE} WHERE id = ANY(%s)", [ids])  # noqa: S608
            # `uninterruptible`: see `common.testing.migration_replay`. The
            # alarm landing inside this restore leaves the worker's database
            # mid-graph and fails every test scheduled after it.
            with uninterruptible():
                executor = MigrationExecutor(connection)
                executor.migrate(executor.loader.graph.leaf_nodes())
                executor.loader.build_graph()
