"""Migration 0073: ``ical_uid`` on events and blocked times, backfilled from the stored
provider payload.

Rows are written at the current schema, then the database steps back to 0072 (which
drops only the new column) and forward through 0073, which adds it empty and runs the
backfill over them.
"""

from __future__ import annotations

import datetime

from django.db import connection

import pytest
from model_bakery import baker

from calendar_integration.models import BlockedTime, Calendar, CalendarEvent
from common.testing.migration_replay import migrate_to, migration_replay, restore_leaf_nodes
from organizations.models import Organization


APP_LABEL = "calendar_integration"
BEFORE = "0072_managed_calendar_is_free_in_range_ignored_rows"
AFTER = "0073_event_and_blocked_time_ical_uid"


def _ical_uids(table: str, ids: list[int]) -> dict[int, str]:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT id, ical_uid FROM {table} WHERE id = ANY(%s)", [ids])  # noqa: S608
        return dict(cursor.fetchall())


@migration_replay
@pytest.mark.django_db(transaction=True)
class TestEventICalUIDMigration:
    """Forward fills the UID from Google's or Outlook's payload and leaves rows without
    one empty. Restores the schema in ``finally`` -- see ``common.testing.migration_replay``.
    """

    def test_forward_backfills_the_uid_from_the_stored_provider_payload(self):
        organization = baker.make(Organization, name="iCal UID Migration Org")
        calendar = baker.make(Calendar, organization=organization, external_id="cal")
        start = datetime.datetime(2026, 1, 5, 10, 0)
        end = datetime.datetime(2026, 1, 5, 11, 0)

        def event(external_id: str, meta: dict) -> int:
            return baker.make(
                CalendarEvent,
                organization=organization,
                calendar=calendar,
                timezone="UTC",
                start_time_tz_unaware=start,
                end_time_tz_unaware=end,
                external_id=external_id,
                meta=meta,
            ).id

        def block(external_id: str, meta: dict) -> int:
            return baker.make(
                BlockedTime,
                organization=organization,
                calendar=calendar,
                timezone="UTC",
                start_time_tz_unaware=start,
                end_time_tz_unaware=end,
                external_id=external_id,
                meta=meta,
            ).id

        google_event = event("g", {"latest_original_payload": {"iCalUID": "g@google.com"}})
        outlook_event = event("o", {"latest_original_payload": {"iCalUId": "0400000082"}})
        bare_event = event("bare", {})
        google_block = block("gb", {"latest_original_payload": {"iCalUID": "copy@google.com"}})
        outlook_block = block("ob", {"latest_original_payload": {"iCalUId": "0400000083"}})
        payload_without_uid = block("x", {"latest_original_payload": {"id": "x"}})
        event_ids = [google_event, outlook_event, bare_event]
        block_ids = [google_block, outlook_block, payload_without_uid]
        try:
            migrate_to((APP_LABEL, BEFORE))
            migrate_to((APP_LABEL, AFTER))

            assert _ical_uids("calendar_integration_calendarevent", event_ids) == {
                google_event: "g@google.com",
                outlook_event: "0400000082",
                bare_event: "",
            }
            assert _ical_uids("calendar_integration_blockedtime", block_ids) == {
                google_block: "copy@google.com",
                outlook_block: "0400000083",
                payload_without_uid: "",
            }
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM calendar_integration_blockedtime WHERE id = ANY(%s)", [block_ids]
                )
                cursor.execute(
                    "DELETE FROM calendar_integration_calendarevent WHERE id = ANY(%s)",
                    [event_ids],
                )
            restore_leaf_nodes()
