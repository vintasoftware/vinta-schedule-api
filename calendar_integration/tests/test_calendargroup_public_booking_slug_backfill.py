"""``CalendarGroup.public_booking_slug``: generation, collision handling, and
the 0052 -> 0053 -> 0054 backfill migration chain (Phase 3b).

Two layers, deliberately separate -- same shape as
``organizations/tests/test_slug_backfill.py``:

* the generator itself (``generate_public_booking_slug`` on
  ``calendar_integration.models``, and the collision-checked
  ``_generate_unused_slug`` helper the migration's backfill uses), tested as
  plain functions -- no database, no migration machinery;
* the ``0052``/``0053``/``0054`` migration chain itself, driven backwards and
  forwards against a real database via ``MigrationExecutor``, so the
  idempotent-rerun and reverse-leaves-data-untouched guarantees are actually
  exercised rather than merely read off the code.
"""

from __future__ import annotations

import importlib

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

import pytest
from model_bakery import baker

from calendar_integration.models import generate_public_booking_slug
from common.testing.migration_replay import migration_replay, uninterruptible
from organizations.models import Organization


APP_LABEL = "calendar_integration"
BEFORE_ADD_FIELD = "0051_calendarmanagementtoken_minted_by_membership_and_calendargroup_duration"
AFTER_BACKFILL = "0053_backfill_calendargroup_public_booking_slug"
AFTER_UNIQUE = "0054_calendargroup_public_booking_slug_unique"

TABLE = "calendar_integration_calendargroup"


class TestGeneratePublicBookingSlug:
    """The model-level default callable, in isolation -- no database needed."""

    def test_produces_distinct_values_across_many_calls(self):
        slugs = {generate_public_booking_slug() for _ in range(2000)}

        assert len(slugs) == 2000

    def test_is_url_safe_and_reasonably_sized(self):
        slug = generate_public_booking_slug()

        assert slug
        assert len(slug) <= 32  # fits CharField(max_length=32)
        assert all(c.isalnum() or c in "-_" for c in slug)


class TestGenerateUnusedSlugCollisionHandling:
    """The migration backfill's own collision-checked generator, deterministically
    forced into a collision -- not left to the astronomical odds of a real one."""

    def test_regenerates_on_a_forced_collision(self, monkeypatch):
        from calendar_integration.migrations import _0053_backfill_helpers as helpers

        canned = iter(["taken-slug", "taken-slug", "fresh-slug"])
        monkeypatch.setattr(helpers.secrets, "token_urlsafe", lambda _n: next(canned))

        result = helpers._generate_unused_slug({"taken-slug"})  # noqa: SLF001 -- testing the private helper directly

        assert result == "fresh-slug"


@pytest.fixture
def organization():
    return baker.make(Organization, name="Public Slug Backfill Test Org")


@migration_replay
@pytest.mark.django_db(transaction=True)
class TestPublicBookingSlugBackfillMigrationChain:
    """Drives 0052 -> 0053 -> 0054 against a real database, backwards then forwards.

    Necessary rather than incidental: ``public_booking_slug`` is NOT NULL +
    globally UNIQUE from 0054 onwards, so the pre-existing-NULL-row scenario
    the backfill exists to handle cannot be constructed at all while the
    current schema is in place. Reversing to 0051 (column absent entirely) is
    the only way to insert rows the backfill then has to fill.

    Restores the schema in ``finally`` -- see
    ``common.testing.migration_replay``'s module docstring for why that is
    not optional bookkeeping on a database this test's worker shares with
    every other test.
    """

    def _insert_group_without_slug(self, organization_id: int, name: str) -> int:
        """Insert a CalendarGroup row straight through SQL, bypassing the ORM.

        Bypassing the ORM is required here, not a style choice: the LIVE
        ``CalendarGroup`` model class still declares ``public_booking_slug``
        regardless of which migration this test has stepped the DATABASE back
        to, so an ORM ``.create()`` would either fail (asking about a column
        that transiently does not exist in the stepped-back schema) or
        silently rely on Django ORM internals never designed for this
        mismatch. Raw SQL, keyed to the exact columns 0051's schema has,
        sidesteps that entirely -- same approach
        ``test_slug_backfill.py``'s ``_insert_unslugged`` takes.
        """
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {TABLE}
                (created, modified, meta, name, description, organization_id,
                 accepts_public_scheduling)
                VALUES (NOW(), NOW(), '{{}}', %s, '', %s, false)
                RETURNING id
                """,
                [name, organization_id],
            )
            return cursor.fetchone()[0]

    def _slugs_for(self, ids: list[int]) -> dict[int, str | None]:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT id, public_booking_slug FROM {TABLE} WHERE id = ANY(%s)", [ids])
            return dict(cursor.fetchall())

    def test_backfill_is_distinct_idempotent_and_reverses_without_losing_data(self, organization):
        ids: list[int] = []
        executor = MigrationExecutor(connection)
        try:
            # --- Step back to BEFORE the column exists, and insert rows the
            # way pre-Phase-3b production data would look: no slug at all. ---
            executor.migrate([(APP_LABEL, BEFORE_ADD_FIELD)])
            executor.loader.build_graph()

            ids += [
                self._insert_group_without_slug(organization.id, f"Group {i}") for i in range(5)
            ]

            # --- Forward through 0052 (nullable AddField) and 0053 (the
            # backfill) -- every pre-existing row must come out with a
            # distinct, non-NULL slug. ---
            executor = MigrationExecutor(connection)
            executor.migrate([(APP_LABEL, AFTER_BACKFILL)])
            executor.loader.build_graph()

            first_pass = self._slugs_for(ids)
            assert all(first_pass[i] for i in ids)
            assert len(set(first_pass.values())) == len(ids)

            # --- Idempotency, proven for real: simulate a partial failure by
            # nulling two of the five rows back out, then re-run the SAME
            # importable helper the migration itself calls (not the migration
            # runner -- directly, exactly as the helper module's docstring
            # says tests can). Untouched rows must keep their EXACT prior
            # values; nulled rows must get NEW distinct values; nothing
            # collides. ---
            renulled = ids[:2]
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {TABLE} SET public_booking_slug = NULL WHERE id = ANY(%s)",
                    [renulled],
                )
            backfill_helpers = importlib.import_module(
                f"{APP_LABEL}.migrations._0053_backfill_helpers"
            )
            backfill_helpers.backfill_public_booking_slugs()

            second_pass = self._slugs_for(ids)
            for group_id in ids[2:]:
                assert second_pass[group_id] == first_pass[group_id], (
                    "a row the prior run already filled must be untouched by a rerun"
                )
            for group_id in renulled:
                assert second_pass[group_id] is not None
                assert second_pass[group_id] != first_pass[group_id]
            assert len(set(second_pass.values())) == len(ids)

            # Running it again with nothing NULL left is a true no-op.
            backfill_helpers.backfill_public_booking_slugs()
            assert self._slugs_for(ids) == second_pass

            # --- 0054: NOT NULL + globally UNIQUE, built CONCURRENTLY. ---
            executor = MigrationExecutor(connection)
            executor.migrate([(APP_LABEL, AFTER_UNIQUE)])
            executor.loader.build_graph()

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT conname, contype FROM pg_constraint "
                    f"WHERE conrelid = '{TABLE}'::regclass "
                    "AND conname = 'calendargroup_public_booking_slug_uniq'"
                )
                constraint = cursor.fetchone()
            assert constraint == ("calendargroup_public_booking_slug_uniq", "u")

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT is_nullable FROM information_schema.columns "
                    f"WHERE table_name = '{TABLE}' AND column_name = 'public_booking_slug'"
                )
                (is_nullable,) = cursor.fetchone()
            assert is_nullable == "NO"

            # --- Reverse ONLY 0054 (the AlterField/constraint step) and
            # confirm the backfilled data is completely untouched: dropping
            # the unique constraint and NOT NULL must not touch row values,
            # only the schema around them. ---
            executor = MigrationExecutor(connection)
            executor.migrate([(APP_LABEL, AFTER_BACKFILL)])
            executor.loader.build_graph()

            assert self._slugs_for(ids) == second_pass

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT conname FROM pg_constraint "
                    f"WHERE conrelid = '{TABLE}'::regclass "
                    "AND conname = 'calendargroup_public_booking_slug_uniq'"
                )
                assert cursor.fetchone() is None, "reversing 0054 must drop the constraint"
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT indexname FROM pg_indexes "
                    f"WHERE tablename = '{TABLE}' "
                    "AND indexname = 'calendargroup_public_booking_slug_uniq'"
                )
                assert cursor.fetchone() is None, (
                    "reversing 0054 must drop the index too -- no orphan"
                )
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {TABLE} WHERE id = ANY(%s)", [ids])
            # `uninterruptible`: see `common.testing.migration_replay`. The
            # alarm landing inside this restore leaves the worker's database
            # mid-graph and fails every test scheduled after it.
            with uninterruptible():
                executor = MigrationExecutor(connection)
                executor.migrate(executor.loader.graph.leaf_nodes())
                executor.loader.build_graph()
