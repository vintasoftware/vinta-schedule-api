"""Migration 0072: ``managed_calendar_is_free_in_range`` gains two arguments.

The new version is a new signature to Postgres. Both directions must leave exactly one
function behind; two would make every 4-argument call ambiguous, since the new
arguments have defaults.
"""

from __future__ import annotations

from django.db import connection

import pytest

from common.testing.migration_replay import migrate_to, migration_replay, restore_leaf_nodes


APP_LABEL = "calendar_integration"
BEFORE = "0071_calendar_event_meeting"
AFTER = "0072_managed_calendar_is_free_in_range_ignored_rows"

FOUR_ARGS = (
    "p_organization_id bigint, p_calendar_id bigint, p_start timestamp with time zone, "
    "p_end timestamp with time zone"
)
SIX_ARGS = f"{FOUR_ARGS}, p_ignored_event_id bigint, p_ignored_blocked_time_ids bigint[]"


def _signatures() -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_function_identity_arguments(oid) FROM pg_proc "
            "WHERE proname = 'managed_calendar_is_free_in_range'"
        )
        return [row[0] for row in cursor.fetchall()]


@migration_replay
@pytest.mark.django_db(transaction=True)
class TestManagedCalendarIsFreeInRangeMigration:
    """Restores the schema in ``finally`` -- see ``common.testing.migration_replay``."""

    def test_each_direction_leaves_only_its_own_signature(self):
        try:
            migrate_to((APP_LABEL, BEFORE))
            assert _signatures() == [FOUR_ARGS]

            migrate_to((APP_LABEL, AFTER))
            assert _signatures() == [SIX_ARGS]

            migrate_to((APP_LABEL, BEFORE))
            assert _signatures() == [FOUR_ARGS]
        finally:
            restore_leaf_nodes()
