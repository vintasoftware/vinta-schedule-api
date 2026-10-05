"""``CalendarService.sync_microsoft_room_events``: app-only delta sync of a Microsoft room."""

import datetime

import pytest
from freezegun import freeze_time

from calendar_integration.constants import CalendarSyncStatus, CalendarSyncTriggerSource
from calendar_integration.models import BlockedTime, CalendarEvent, CalendarSync
from calendar_integration.services.calendar_sync_service import ROOM_EVENT_SYNC_WINDOW
from calendar_integration.tests.ms_room_graph import (
    ROOM_EMAIL,
    graph_event,
    make_room,
)
from common.organization_context import organization_context
from organizations.models import Organization


pytestmark = pytest.mark.django_db

NOW = datetime.datetime(2026, 10, 6, 15, 0, tzinfo=datetime.UTC)
TOMORROW_10 = datetime.datetime(2026, 10, 7, 10, 0, tzinfo=datetime.UTC)
TOMORROW_14 = datetime.datetime(2026, 10, 7, 14, 0, tzinfo=datetime.UTC)


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.create(name="Contoso")


def sync(di_container, organization, calendar):
    with organization_context(organization):
        calendar_service = di_container.calendar_service()
        calendar_service.initialize_without_provider(organization=organization)
        return calendar_service.sync_microsoft_room_events(calendar)


def room_blocks(organization, calendar) -> list[tuple[str, str, datetime.datetime]]:
    with organization_context(organization):
        return sorted(
            (block.external_id, block.reason, block.start_time)
            for block in BlockedTime.objects.filter(calendar_fk=calendar)
        )


@freeze_time(NOW)
class TestFirstSync:
    def test_stores_every_room_event_and_the_delta_token(
        self, di_container, organization, ms_room_graph
    ):
        calendar = make_room(organization)
        ms_room_graph.events = {
            "ev-1": graph_event("ev-1", "Standup", TOMORROW_10),
            "ev-2": graph_event("ev-2", "Review", TOMORROW_14),
        }

        calendar_sync = sync(di_container, organization, calendar)

        assert room_blocks(organization, calendar) == [
            ("ev-1", "Standup", TOMORROW_10),
            ("ev-2", "Review", TOMORROW_14),
        ]
        day_start = datetime.datetime(2026, 10, 6, tzinfo=datetime.UTC)
        assert (
            calendar_sync.status,
            calendar_sync.next_sync_token,
            calendar_sync.trigger_source,
            calendar_sync.start_datetime,
            calendar_sync.end_datetime,
        ) == (
            CalendarSyncStatus.SUCCESS,
            "token-1",
            CalendarSyncTriggerSource.WEBHOOK,
            day_start,
            day_start + ROOM_EVENT_SYNC_WINDOW,
        )
        # Two pages (page size 1): the initial window, then Graph's next link.
        assert ms_room_graph.delta_requests() == [
            {
                "startDateTime": day_start.isoformat(),
                "endDateTime": (day_start + ROOM_EVENT_SYNC_WINDOW).isoformat(),
            },
            None,
        ]

    def test_reads_the_room_through_its_mailbox_with_the_app_only_token(
        self, di_container, organization, ms_room_graph, ms_room_token_provider
    ):
        calendar = make_room(organization)

        sync(di_container, organization, calendar)

        ms_room_token_provider.get_token.assert_called_with("11111111-2222-3333-4444-555555555555")
        assert [path for _, path, _ in ms_room_graph.requests] == [
            "/places/place-room-a",
            f"/users/{ROOM_EMAIL}/calendarView/delta",
        ]


@freeze_time(NOW)
class TestSyncWithDeltaToken:
    def test_applies_edits_and_deletions_since_the_last_round(
        self, di_container, organization, ms_room_graph
    ):
        calendar = make_room(organization)
        ms_room_graph.events = {
            "ev-1": graph_event("ev-1", "Standup", TOMORROW_10),
            "ev-2": graph_event("ev-2", "Review", TOMORROW_14),
        }
        sync(di_container, organization, calendar)
        ms_room_graph.edit("ev-1", subject="Standup (renamed)")
        ms_room_graph.delete("ev-2")

        calendar_sync = sync(di_container, organization, calendar)

        assert room_blocks(organization, calendar) == [("ev-1", "Standup (renamed)", TOMORROW_10)]
        assert ms_room_graph.delta_requests()[-1] == {"$deltatoken": "token-1"}
        assert (calendar_sync.status, calendar_sync.next_sync_token) == (
            CalendarSyncStatus.SUCCESS,
            "token-2",
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Shared sync pipeline bug, outside this phase: _process_existing_blocked_time "
            "assigns the generated start_time / end_time, but the bulk update writes "
            "start_time_tz_unaware / end_time_tz_unaware, so a moved booking keeps its "
            "old time."
        ),
    )
    def test_a_moved_booking_moves(self, di_container, organization, ms_room_graph):
        calendar = make_room(organization)
        ms_room_graph.events = {"ev-1": graph_event("ev-1", "Standup", TOMORROW_10)}
        sync(di_container, organization, calendar)
        moved = TOMORROW_10 + datetime.timedelta(hours=1)
        ms_room_graph.edit("ev-1", **graph_event("ev-1", "Standup", moved))

        sync(di_container, organization, calendar)

        assert room_blocks(organization, calendar) == [("ev-1", "Standup", moved)]

    def test_removal_of_an_event_never_stored_creates_nothing(
        self, di_container, organization, ms_room_graph
    ):
        calendar = make_room(organization)
        ms_room_graph.events = {"ev-1": graph_event("ev-1", "Standup", TOMORROW_10)}
        sync(di_container, organization, calendar)
        ms_room_graph.events["ev-9"] = graph_event("ev-9", "Created and deleted", TOMORROW_14)
        ms_room_graph.delete("ev-9")

        sync(di_container, organization, calendar)

        assert room_blocks(organization, calendar) == [("ev-1", "Standup", TOMORROW_10)]
        with organization_context(organization):
            assert not CalendarEvent.objects.filter(calendar_fk=calendar).exists()

    def test_a_new_day_starts_a_new_round(self, di_container, organization, ms_room_graph):
        calendar = make_room(organization)
        ms_room_graph.events = {"ev-1": graph_event("ev-1", "Standup", TOMORROW_10)}
        sync(di_container, organization, calendar)

        with freeze_time(NOW + datetime.timedelta(days=1)):
            calendar_sync = sync(di_container, organization, calendar)

        next_day = datetime.datetime(2026, 10, 7, tzinfo=datetime.UTC)
        assert ms_room_graph.delta_requests()[-1] == {
            "startDateTime": next_day.isoformat(),
            "endDateTime": (next_day + ROOM_EVENT_SYNC_WINDOW).isoformat(),
        }
        assert calendar_sync.next_sync_token == "token-2"
        assert room_blocks(organization, calendar) == [("ev-1", "Standup", TOMORROW_10)]


@freeze_time(NOW)
class TestSkipped:
    @pytest.mark.parametrize(
        ("flag_on", "write_enabled"), [(False, True), (True, False)], ids=["flag-off", "no-write"]
    )
    def test_room_outside_a_flag_on_write_enabled_org_is_not_synced(
        self, di_container, organization, ms_room_graph, flag_on, write_enabled
    ):
        calendar = make_room(organization, flag_on=flag_on, write_enabled=write_enabled)
        ms_room_graph.events = {"ev-1": graph_event("ev-1", "Standup", TOMORROW_10)}

        result = sync(di_container, organization, calendar)

        assert result is None
        assert ms_room_graph.requests == []
        with organization_context(organization):
            assert not CalendarSync.objects.filter(calendar=calendar).exists()
        assert room_blocks(organization, calendar) == []

    def test_failed_round_is_recorded_and_stores_nothing(
        self, di_container, organization, ms_room_graph, ms_room_token_provider
    ):
        calendar = make_room(organization)
        ms_room_graph.events = {"ev-1": graph_event("ev-1", "Standup", TOMORROW_10)}
        ms_room_token_provider.get_token.side_effect = RuntimeError("token endpoint down")

        calendar_sync = sync(di_container, organization, calendar)

        assert calendar_sync.status == CalendarSyncStatus.FAILED
        assert room_blocks(organization, calendar) == []
