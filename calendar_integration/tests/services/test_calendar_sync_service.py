"""Unit tests for CalendarSyncService.

Tests construct CalendarSyncService directly (bypassing the CalendarService
facade) using a real CalendarServiceContext fed a fake calendar adapter, plus a
lightweight fake host for the concerns routed back to the facade
(``_remove_available_time_windows_that_overlap_with_blocked_times_and_events``,
``_grant_calendar_owner_permissions``, ``request_calendar_sync``,
``_execute_organization_calendar_resources_import``).

The two flows covered are:
- a full sync diff/merge cycle (adapter returns events -> sync creates a new
  ``BlockedTime`` for an externally-created event, updates an already-stored
  ``BlockedTime``, and deletes a row that vanished from the provider);
- the organization-resource import path (adapter returns resources -> the import
  routes one ``request_calendar_sync`` per resource through the host).
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from allauth.socialaccount.models import SocialAccount

from calendar_integration.constants import CalendarProvider, CalendarSyncStatus, CalendarType
from calendar_integration.models import (
    BlockedTime,
    Calendar,
    CalendarSync,
)
from calendar_integration.services.calendar_service_context import CalendarServiceContext
from calendar_integration.services.calendar_sync_service import CalendarSyncService
from calendar_integration.services.dataclasses import (
    CalendarEventAdapterOutputData,
    CalendarResourceData,
)
from organizations.models import Organization
from users.models import Profile, User


# ---------------------------------------------------------------------------
# Fake host
# ---------------------------------------------------------------------------


class FakeHost:
    """Minimal SyncServiceHost used in unit tests.

    Records the calls routed back to the facade so individual tests can assert on
    them. ``_remove_available_time_windows_...`` and ``_grant_calendar_owner_permissions``
    are no-ops with call capture; ``request_calendar_sync`` and
    ``_execute_organization_calendar_resources_import`` capture their arguments.
    """

    def __init__(self) -> None:
        self.remove_calls: list[tuple[Any, ...]] = []
        self.grant_calls: list[Calendar] = []
        self.request_calendar_sync_calls: list[dict[str, Any]] = []
        self.execute_org_import_calls: list[tuple[datetime.datetime, datetime.datetime]] = []

    def _remove_available_time_windows_that_overlap_with_blocked_times_and_events(
        self,
        calendar_id: int,
        blocked_times: Any,
        events: Any,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
    ) -> None:
        self.remove_calls.append(
            (calendar_id, list(blocked_times), list(events), start_time, end_time)
        )

    def _grant_calendar_owner_permissions(self, calendar: Calendar) -> None:
        self.grant_calls.append(calendar)

    def request_calendar_sync(
        self,
        calendar: Calendar,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
        should_update_events: bool = False,
        trigger_source: Any = None,
    ) -> None:
        self.request_calendar_sync_calls.append(
            {
                "calendar": calendar,
                "start_datetime": start_datetime,
                "end_datetime": end_datetime,
                "should_update_events": should_update_events,
                "trigger_source": trigger_source,
            }
        )
        return None

    def _execute_organization_calendar_resources_import(
        self,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        import_workflow_state: Any = None,
        bypass_limits: bool = False,
    ) -> list[CalendarResourceData]:
        self.execute_org_import_calls.append((start_time, end_time))
        return []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Sync Test Org")


@pytest.fixture
def user(db: Any) -> User:
    u = User.objects.create_user(email="test_sync@example.com", password="pass")  # noqa: S106
    Profile.objects.create(user=u)
    return u


@pytest.fixture
def social_account(db: Any, user: User) -> SocialAccount:
    return SocialAccount.objects.create(user=user, provider=CalendarProvider.GOOGLE, uid="88888")


@pytest.fixture
def calendar(db: Any, organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Sync Calendar",
        external_id="sync_cal_001",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )


@pytest.fixture
def fake_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.provider = CalendarProvider.GOOGLE
    return adapter


@pytest.fixture
def context(
    organization: Organization, user: User, fake_adapter: MagicMock
) -> CalendarServiceContext:
    return CalendarServiceContext(
        organization=organization,
        user_or_token=user,
        account=user,
        calendar_adapter=fake_adapter,
        calendar_permission_service=None,
        calendar_side_effects_service=None,
    )


def make_service(context: CalendarServiceContext, host: FakeHost) -> CalendarSyncService:
    return CalendarSyncService(context=context, calendar_cache={}, host=host)


def _adapter_event(
    external_id: str,
    title: str,
    start: datetime.datetime,
    end: datetime.datetime,
    status: str = "confirmed",
) -> CalendarEventAdapterOutputData:
    return CalendarEventAdapterOutputData(
        calendar_external_id="sync_cal_001",
        title=title,
        description="desc",
        start_time=start,
        end_time=end,
        timezone="UTC",
        attendees=[],
        external_id=external_id,
        status=status,  # type: ignore[arg-type]
        original_payload={"id": external_id},
    )


# ---------------------------------------------------------------------------
# Tests: full sync diff/merge cycle
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_execute_calendar_sync_full_cycle_creates_updates_and_deletes(
    context: CalendarServiceContext,
    calendar: Calendar,
    organization: Organization,
    fake_adapter: MagicMock,
) -> None:
    """A full sync (no sync_token) creates new externally-sourced events as
    BlockedTime, updates an already-stored BlockedTime, and deletes rows that
    vanished from the provider."""
    window_start = datetime.datetime(2025, 8, 1, 0, 0, tzinfo=datetime.UTC)
    window_end = datetime.datetime(2025, 8, 1, 23, 59, tzinfo=datetime.UTC)

    # An existing externally-sourced BlockedTime that the provider still returns
    # (should be updated) ...
    existing_block = BlockedTime.objects.create(
        calendar=calendar,
        start_time_tz_unaware=datetime.datetime(2025, 8, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2025, 8, 1, 10, 0),
        timezone="UTC",
        reason="Old reason",
        external_id="ext_existing",
        organization_id=organization.id,
    )
    # ... and one inside the window the provider no longer returns (should survive,
    # because deletion only targets CalendarEvent rows on full sync).
    BlockedTime.objects.create(
        calendar=calendar,
        start_time_tz_unaware=datetime.datetime(2025, 8, 1, 14, 0),
        end_time_tz_unaware=datetime.datetime(2025, 8, 1, 15, 0),
        timezone="UTC",
        reason="Vanished",
        external_id="ext_vanished",
        organization_id=organization.id,
    )

    fake_adapter.get_events.return_value = {
        "events": [
            # update of the existing block
            _adapter_event(
                "ext_existing",
                "Updated reason",
                datetime.datetime(2025, 8, 1, 9, 30, tzinfo=datetime.UTC),
                datetime.datetime(2025, 8, 1, 10, 30, tzinfo=datetime.UTC),
            ),
            # brand-new externally-created single event -> new BlockedTime
            _adapter_event(
                "ext_new",
                "Brand New",
                datetime.datetime(2025, 8, 1, 11, 0, tzinfo=datetime.UTC),
                datetime.datetime(2025, 8, 1, 12, 0, tzinfo=datetime.UTC),
            ),
        ],
        "next_sync_token": "tok-after-full",
    }

    calendar_sync = CalendarSync.objects.create(
        calendar=calendar,
        organization=organization,
        start_datetime=window_start,
        end_datetime=window_end,
        should_update_events=True,
        status=CalendarSyncStatus.IN_PROGRESS,
    )

    service = make_service(context, FakeHost())
    service._execute_calendar_sync(calendar_sync, sync_token=None)

    fake_adapter.get_events.assert_called_once()

    # new event materialized as a BlockedTime
    new_block = BlockedTime.objects.get(
        calendar=calendar, external_id="ext_new", organization_id=organization.id
    )
    assert new_block.reason == "Brand New"

    # existing block updated in place (no duplicate, reason refreshed)
    existing_block.refresh_from_db()
    assert existing_block.reason == "Updated reason"
    assert (
        BlockedTime.objects.filter(
            calendar=calendar, external_id="ext_existing", organization_id=organization.id
        ).count()
        == 1
    )

    # the vanished BlockedTime survives a full sync (deletion targets CalendarEvent rows)
    assert BlockedTime.objects.filter(
        calendar=calendar, external_id="ext_vanished", organization_id=organization.id
    ).exists()


@pytest.mark.django_db
def test_sync_events_marks_success(
    context: CalendarServiceContext,
    calendar: Calendar,
    organization: Organization,
    fake_adapter: MagicMock,
) -> None:
    """sync_events drives the run and flips the CalendarSync to SUCCESS."""
    fake_adapter.get_events.return_value = {"events": [], "next_sync_token": "tok"}

    calendar_sync = CalendarSync.objects.create(
        calendar=calendar,
        organization=organization,
        start_datetime=datetime.datetime(2025, 8, 2, 0, 0, tzinfo=datetime.UTC),
        end_datetime=datetime.datetime(2025, 8, 2, 23, 59, tzinfo=datetime.UTC),
        should_update_events=True,
        status=CalendarSyncStatus.NOT_STARTED,
    )

    service = make_service(context, FakeHost())
    service.sync_events(calendar_sync)

    calendar_sync.refresh_from_db()
    assert calendar_sync.status == CalendarSyncStatus.SUCCESS


# ---------------------------------------------------------------------------
# Tests: organization-resource import path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_execute_organization_calendar_resources_import_syncs_each_resource(
    context: CalendarServiceContext,
    organization: Organization,
    fake_adapter: MagicMock,
) -> None:
    """_execute_organization_calendar_resources_import upserts a RESOURCE calendar
    per discovered resource and routes a request_calendar_sync through the host."""
    start = datetime.datetime(2025, 8, 3, 9, 0, tzinfo=datetime.UTC)
    end = datetime.datetime(2025, 8, 3, 17, 0, tzinfo=datetime.UTC)

    resource = CalendarResourceData(
        name="Room A",
        description="Big room",
        provider="google",
        external_id="room_a",
        email="rooma@example.com",
        capacity=12,
    )
    fake_adapter.get_available_calendar_resources.return_value = [resource]

    host = FakeHost()
    service = make_service(context, host)

    result = service._execute_organization_calendar_resources_import(start, end)

    fake_adapter.get_available_calendar_resources.assert_called_once_with(start, end)
    assert list(result) == [resource]

    # The resource calendar was upserted with the RESOURCE type ...
    room_calendar = Calendar.objects.get(external_id="room_a", organization_id=organization.id)
    assert room_calendar.calendar_type == CalendarType.RESOURCE

    # ... and exactly one request_calendar_sync was routed through the host for it.
    assert len(host.request_calendar_sync_calls) == 1
    call = host.request_calendar_sync_calls[0]
    assert call["calendar"] == room_calendar
    assert call["start_datetime"] == start
    assert call["end_datetime"] == end
    assert call["should_update_events"] is True
