"""``sync_microsoft_room_events_task``: organization binding and re-runs."""

import datetime

import pytest
from freezegun import freeze_time

from calendar_integration.constants import CalendarSyncStatus
from calendar_integration.models import BlockedTime, CalendarSync
from calendar_integration.tasks import sync_microsoft_room_events_task
from calendar_integration.tests.ms_room_graph import (
    graph_event,
    make_room,
)
from common.organization_context import organization_context
from organizations.models import Organization


pytestmark = pytest.mark.django_db

NOW = datetime.datetime(2026, 10, 6, 15, 0, tzinfo=datetime.UTC)
TOMORROW_10 = datetime.datetime(2026, 10, 7, 10, 0, tzinfo=datetime.UTC)
TOMORROW_14 = datetime.datetime(2026, 10, 7, 14, 0, tzinfo=datetime.UTC)


def room_event_ids(organization, calendar) -> list[str]:
    with organization_context(organization):
        return sorted(
            BlockedTime.objects.filter(calendar_fk=calendar).values_list("external_id", flat=True)
        )


def sync_statuses(organization, calendar) -> list[str]:
    with organization_context(organization):
        return list(
            CalendarSync.objects.filter(calendar=calendar)
            .order_by("created")
            .values_list("status", flat=True)
        )


@freeze_time(NOW)
class TestSyncMicrosoftRoomEventsTask:
    def test_syncs_the_rooms_events_and_then_a_deletion(self, ms_room_graph):
        organization = Organization.objects.create(name="Contoso")
        calendar = make_room(organization)
        ms_room_graph.events = {
            "ev-1": graph_event("ev-1", "Standup", TOMORROW_10),
            "ev-2": graph_event("ev-2", "Review", TOMORROW_14),
        }

        sync_microsoft_room_events_task(calendar.id, organization.id)
        after_first_run = room_event_ids(organization, calendar)
        ms_room_graph.delete("ev-2")
        sync_microsoft_room_events_task(calendar.id, organization.id)

        assert after_first_run == ["ev-1", "ev-2"]
        assert room_event_ids(organization, calendar) == ["ev-1"]

    def test_a_re_run_with_nothing_new_changes_nothing(self, ms_room_graph):
        organization = Organization.objects.create(name="Contoso")
        calendar = make_room(organization)
        ms_room_graph.events = {"ev-1": graph_event("ev-1", "Standup", TOMORROW_10)}

        sync_microsoft_room_events_task(calendar.id, organization.id)
        sync_microsoft_room_events_task(calendar.id, organization.id)

        assert room_event_ids(organization, calendar) == ["ev-1"]
        assert sync_statuses(organization, calendar) == [
            CalendarSyncStatus.SUCCESS,
            CalendarSyncStatus.SUCCESS,
        ]
        assert ms_room_graph.delta_requests()[-1] == {"$deltatoken": "token-1"}

    def test_calendar_of_another_organization_is_not_synced(self, ms_room_graph):
        contoso = Organization.objects.create(name="Contoso")
        fabrikam = Organization.objects.create(name="Fabrikam")
        calendar = make_room(contoso)
        ms_room_graph.events = {"ev-1": graph_event("ev-1", "Standup", TOMORROW_10)}

        sync_microsoft_room_events_task(calendar.id, fabrikam.id)

        assert ms_room_graph.requests == []
        assert room_event_ids(contoso, calendar) == []

    def test_missing_organization_does_nothing(self, ms_room_graph):
        sync_microsoft_room_events_task(1, 999_999)

        assert ms_room_graph.requests == []
