"""Migration 0068: ``Calendar.external_id`` turns nullable, blank ids become NULL.

Driven backwards and forwards against a real database via ``MigrationExecutor``,
because the state it converts -- an internal calendar holding ``""`` under a NOT NULL
column -- cannot be built while the current schema is in place.
"""

from __future__ import annotations

from django.db import IntegrityError, connection

import pytest
from model_bakery import baker

from common.testing.migration_replay import migrate_to, migration_replay, restore_leaf_nodes
from organizations.models import Organization


APP_LABEL = "calendar_integration"
BEFORE = "0067_webhook_subscription_opening_account"
AFTER = "0068_calendar_external_id_nullable"

TABLE = "calendar_integration_calendar"


@pytest.fixture
def organizations():
    return (
        baker.make(Organization, name="External Id Migration Org A"),
        baker.make(Organization, name="External Id Migration Org B"),
    )


@migration_replay
@pytest.mark.django_db(transaction=True)
class TestCalendarExternalIdNullableMigration:
    """Forward nulls blank ids and leaves provider ids alone; reverse restores ``""``.

    Restores the schema in ``finally`` -- see ``common.testing.migration_replay``.
    """

    def _insert_calendar(self, organization_id: int, provider: str, external_id: str | None) -> int:
        """Insert a Calendar row through SQL.

        The live ``Calendar`` model always matches the leaf schema, not the one this
        test stepped back to, so the rows are written with the columns directly.
        """
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {TABLE}
                (created, modified, meta, organization_id, name, description, email,
                 external_id, provider, calendar_type, manage_available_windows,
                 accepts_public_scheduling, visibility, sync_enabled)
                VALUES (NOW(), NOW(), '{{}}', %s, 'Calendar', '', '', %s, %s, 'virtual',
                        false, false, 'active', true)
                RETURNING id
                """,
                [organization_id, external_id, provider],
            )
            return cursor.fetchone()[0]

    def _external_ids_for(self, ids: list[int]) -> dict[int, str | None]:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT id, external_id FROM {TABLE} WHERE id = ANY(%s)", [ids])
            return dict(cursor.fetchall())

    def _external_id_is_nullable(self) -> bool:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT is_nullable FROM information_schema.columns "
                f"WHERE table_name = '{TABLE}' AND column_name = 'external_id'"
            )
            (is_nullable,) = cursor.fetchone()
        return is_nullable == "YES"

    def test_forward_nulls_blank_ids_and_reverse_restores_them(self, organizations):
        org_a, org_b = organizations
        ids: list[int] = []
        try:
            migrate_to((APP_LABEL, BEFORE))

            internal_a = self._insert_calendar(org_a.id, "internal", "")
            internal_b = self._insert_calendar(org_b.id, "internal", "")
            google = self._insert_calendar(org_a.id, "google", "google-calendar-id")
            ids += [internal_a, internal_b, google]

            migrate_to((APP_LABEL, AFTER))

            assert self._external_id_is_nullable()
            assert self._external_ids_for(ids) == {
                internal_a: None,
                internal_b: None,
                google: "google-calendar-id",
            }

            migrate_to((APP_LABEL, BEFORE))

            assert not self._external_id_is_nullable()
            assert self._external_ids_for(ids) == {
                internal_a: "",
                internal_b: "",
                google: "google-calendar-id",
            }

            migrate_to((APP_LABEL, AFTER))

            assert self._external_ids_for(ids) == {
                internal_a: None,
                internal_b: None,
                google: "google-calendar-id",
            }
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {TABLE} WHERE id = ANY(%s)", [ids])
            restore_leaf_nodes()

    def test_reverse_refuses_two_internal_calendars_in_one_organization(self, organizations):
        org_a, _ = organizations
        ids: list[int] = []
        try:
            migrate_to((APP_LABEL, AFTER))

            ids += [
                self._insert_calendar(org_a.id, "internal", None),
                self._insert_calendar(org_a.id, "internal", None),
            ]

            with pytest.raises(IntegrityError):
                migrate_to((APP_LABEL, BEFORE))
        finally:
            with connection.cursor() as cursor:
                cursor.execute(f"DELETE FROM {TABLE} WHERE id = ANY(%s)", [ids])
            restore_leaf_nodes()
