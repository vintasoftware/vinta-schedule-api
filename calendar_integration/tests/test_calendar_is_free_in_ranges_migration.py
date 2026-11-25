"""Migration 0074: ``calendar_is_free_in_ranges`` replaces ``managed_calendar_is_free_in_range``.

Going forward the new function exists and the old one is gone. Going back the old one
is created again, with the arguments 0072 gave it, and the new one is dropped.
"""

from __future__ import annotations

from django.db import connection

import pytest

from common.testing.migration_replay import migrate_to, migration_replay, restore_leaf_nodes


APP_LABEL = "calendar_integration"
BEFORE = "0073_event_and_blocked_time_ical_uid"
AFTER = "0074_calendar_is_free_in_ranges"

OLD_ARGS = (
    "p_organization_id bigint, p_calendar_id bigint, p_start timestamp with time zone, "
    "p_end timestamp with time zone, p_ignored_event_id bigint, "
    "p_ignored_blocked_time_ids bigint[]"
)
NEW_ARGS = (
    "p_organization_id bigint, p_calendar_id bigint, p_ranges tstzrange[], "
    "p_ignored_event_ids bigint[], p_ignored_blocked_time_ids bigint[]"
)


def _functions() -> dict[str, list[str]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT proname, pg_get_function_identity_arguments(oid) FROM pg_proc "
            "WHERE proname IN ('managed_calendar_is_free_in_range', 'calendar_is_free_in_ranges') "
            "ORDER BY proname"
        )
        functions: dict[str, list[str]] = {}
        for name, arguments in cursor.fetchall():
            functions.setdefault(name, []).append(arguments)
        return functions


@migration_replay
@pytest.mark.django_db(transaction=True)
class TestCalendarIsFreeInRangesMigration:
    """Restores the schema in ``finally`` -- see ``common.testing.migration_replay``."""

    def test_each_direction_leaves_only_its_own_function(self):
        try:
            migrate_to((APP_LABEL, BEFORE))
            assert _functions() == {"managed_calendar_is_free_in_range": [OLD_ARGS]}

            migrate_to((APP_LABEL, AFTER))
            assert _functions() == {"calendar_is_free_in_ranges": [NEW_ARGS]}

            migrate_to((APP_LABEL, BEFORE))
            assert _functions() == {"managed_calendar_is_free_in_range": [OLD_ARGS]}
        finally:
            restore_leaf_nodes()
