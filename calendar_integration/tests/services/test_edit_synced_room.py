"""Integration tests for editing a synced room and retrying its sync.

``CalendarService.update_resource_calendar`` on a room with a
``ResourceCalendarProviderLink``, and ``CalendarService.retry_resource_calendar_sync``.
The provider is ``FakeRoomDirectory``, installed by overriding the container's
``resource_directory_adapter_resolver``. Rooms are created through
``create_synced_resource_calendar``, and pushes run eagerly through
``push_room_to_provider_task`` when the on-commit callbacks run.
"""

import datetime
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from freezegun import freeze_time

from audit_integration.constants import AuditAction
from calendar_integration.constants import (
    CalendarProvider,
    CalendarVisibility,
    ResourceSyncOperation,
    ResourceSyncStatus,
)
from calendar_integration.exceptions import (
    InvalidResourceLocationError,
    ResourceCalendarProviderSyncNotEnabledError,
    ResourceDirectoryPermissionError,
    RoomSyncStateError,
)
from calendar_integration.factories import create_resource_location
from calendar_integration.models import Calendar, ResourceCalendarProviderLink, ResourceLocation
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.tests.room_sync_fakes import FakeRoomDirectory, FakeRoomDirectoryResolver
from common.feature_flags import RESOURCE_CALENDAR_PROVIDER_SYNC
from common.organization_context import organization_context
from organizations.models import Organization, OrganizationFeatureFlag
from organizations.tests.helpers import make_admin_membership
from users.factories import UserFactory
from users.models import User


NOW = datetime.datetime(2026, 10, 6, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def organization(db: Any) -> Organization:
    organization = Organization.objects.create(name="Room Edit Org")
    with organization_context(organization):
        OrganizationFeatureFlag.objects.create(
            organization=organization, key=RESOURCE_CALENDAR_PROVIDER_SYNC, enabled=True
        )
    return organization


@pytest.fixture
def admin(organization: Organization) -> User:
    user = UserFactory().create_user(email="room-editor@example.com")
    make_admin_membership(user=user, organization=organization)
    return user


@pytest.fixture
def bound(organization: Organization) -> Iterator[None]:
    with organization_context(organization):
        yield


@pytest.fixture
def location(organization: Organization, bound: None) -> ResourceLocation:
    return create_resource_location(
        organization=organization,
        external_building_id="hq",
        building_name="HQ",
        external_floor_id="4",
        floor_name="4",
    )


@pytest.fixture
def directory() -> FakeRoomDirectory:
    return FakeRoomDirectory(CalendarProvider.GOOGLE)


@pytest.fixture
def resolver(
    di_container: Any, directory: FakeRoomDirectory
) -> Iterator[FakeRoomDirectoryResolver]:
    resolver = FakeRoomDirectoryResolver(directory)
    di_container.resource_directory_adapter_resolver.override(resolver)
    try:
        yield resolver
    finally:
        di_container.resource_directory_adapter_resolver.reset_override()


@pytest.fixture
def service(
    di_container: Any, organization: Organization, admin: User, resolver: Any
) -> CalendarService:
    service = di_container.calendar_service()
    service.initialize_without_provider(user_or_token=admin, organization=organization)
    return service


@pytest.fixture
def persist_audit() -> Iterator[MagicMock]:
    with patch("vinta_audit_logs.tasks.persist_audit_record") as persist_task:
        yield persist_task


def _create_room(
    service: CalendarService,
    location: ResourceLocation,
    capture: Callable[..., Any],
    *,
    push: bool = True,
) -> Calendar:
    """A Google room made through the real create; synced when ``push`` runs the push."""
    with capture(execute=push):
        return service.create_synced_resource_calendar(
            provider=CalendarProvider.GOOGLE,
            location_id=location.id,
            name="Conf Room 4B",
            description="Fourth floor",
            capacity=8,
        )


def _link(room: Calendar) -> ResourceCalendarProviderLink:
    return ResourceCalendarProviderLink.objects.get(calendar=room)


@pytest.mark.usefixtures("bound")
@freeze_time(NOW)
class TestEditSyncedRoom:
    def test_edit_applies_right_away_and_pushes_the_synced_fields(
        self,
        service: CalendarService,
        location: ResourceLocation,
        directory: FakeRoomDirectory,
        persist_audit: MagicMock,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        room = _create_room(service, location, django_capture_on_commit_callbacks)
        assert _link(room).sync_status == ResourceSyncStatus.SYNCED
        room.refresh_from_db()
        provider_id = room.external_id

        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            edited = service.update_resource_calendar(
                room.id, capacity=10, manage_available_windows=True
            )
            link = _link(room)
            assert link.sync_status == ResourceSyncStatus.PENDING_UPDATE
            # Only the synced field is queued for the provider.
            assert link.pending_fields == {"capacity": 10}

        assert edited.capacity == 10
        room.refresh_from_db()
        assert (room.capacity, room.manage_available_windows) == (10, True)

        for callback in callbacks:
            callback()

        link.refresh_from_db()
        assert link.sync_status == ResourceSyncStatus.SYNCED
        assert link.pending_fields == {}
        assert directory.calls == ["create_room", "update_room"]
        assert directory.rooms[provider_id].capacity == 10
        updates = [
            call.args[0]
            for call in persist_audit.delay.call_args_list
            if call.args[0]["action_key"] == AuditAction.UPDATE
        ]
        assert len(updates) == 1

    def test_location_change_is_pushed_and_stored_on_the_link(
        self,
        service: CalendarService,
        location: ResourceLocation,
        organization: Organization,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        room = _create_room(service, location, django_capture_on_commit_callbacks)
        annex = create_resource_location(
            organization=organization, external_building_id="annex", external_floor_id="2"
        )

        with django_capture_on_commit_callbacks(execute=False):
            service.update_resource_calendar(room.id, location_id=annex.id)

        link = _link(room)
        assert link.location == annex
        assert link.sync_status == ResourceSyncStatus.PENDING_UPDATE
        assert link.pending_fields == {
            "location_ref": {"external_building_id": "annex", "external_floor_id": "2"}
        }

    def test_edit_during_pending_creation_merges_into_the_create(
        self,
        service: CalendarService,
        location: ResourceLocation,
        directory: FakeRoomDirectory,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        room = _create_room(service, location, django_capture_on_commit_callbacks, push=False)

        with django_capture_on_commit_callbacks(execute=True):
            service.update_resource_calendar(room.id, name="Conf Room 4C", capacity=12)

        link = _link(room)
        # The create carried the edit: one provider call, with the edited values.
        assert directory.calls == ["create_room"]
        provider_room = directory.rooms[f"vinta-{link.provisional_key}"]
        assert (provider_room.name, provider_room.capacity) == ("Conf Room 4C", 12)
        assert link.sync_status == ResourceSyncStatus.SYNCED

    def test_edit_while_pending_creation_keeps_the_status(
        self,
        service: CalendarService,
        location: ResourceLocation,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        room = _create_room(service, location, django_capture_on_commit_callbacks, push=False)

        with django_capture_on_commit_callbacks(execute=False):
            service.update_resource_calendar(room.id, capacity=12)

        link = _link(room)
        assert link.sync_status == ResourceSyncStatus.PENDING_CREATION
        assert link.pending_fields == {
            "name": "Conf Room 4B",
            "description": "Fourth floor",
            "capacity": 12,
            "location_ref": {"external_building_id": "hq", "external_floor_id": "4"},
        }

    def test_archived_room_is_rejected(
        self,
        service: CalendarService,
        location: ResourceLocation,
        di_container: Any,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        room = _create_room(service, location, django_capture_on_commit_callbacks, push=False)
        # Deleting a room whose create was never confirmed archives it right away.
        di_container.room_sync_service().request_push(_link(room), ResourceSyncOperation.DELETE)
        assert _link(room).sync_status == ResourceSyncStatus.ARCHIVED

        with pytest.raises(RoomSyncStateError):
            service.update_resource_calendar(room.id, manage_available_windows=True)

        room.refresh_from_db()
        assert room.manage_available_windows is False

    def test_room_pending_deletion_is_rejected(
        self,
        service: CalendarService,
        location: ResourceLocation,
        di_container: Any,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        room = _create_room(service, location, django_capture_on_commit_callbacks)
        with django_capture_on_commit_callbacks(execute=False):
            di_container.room_sync_service().request_push(_link(room), ResourceSyncOperation.DELETE)

        with pytest.raises(RoomSyncStateError):
            service.update_resource_calendar(room.id, capacity=3)

        room.refresh_from_db()
        assert room.capacity == 8

    def test_location_of_another_provider_is_rejected(
        self,
        service: CalendarService,
        location: ResourceLocation,
        organization: Organization,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        room = _create_room(service, location, django_capture_on_commit_callbacks)
        microsoft_location = create_resource_location(
            organization=organization, provider=CalendarProvider.MICROSOFT
        )

        with pytest.raises(InvalidResourceLocationError):
            service.update_resource_calendar(room.id, location_id=microsoft_location.id)

        link = _link(room)
        assert link.location == location
        assert link.sync_status == ResourceSyncStatus.SYNCED

    def test_setting_inactive_is_rejected(
        self,
        service: CalendarService,
        location: ResourceLocation,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        room = _create_room(service, location, django_capture_on_commit_callbacks)

        with pytest.raises(ValueError, match="use deleteResourceCalendar"):
            service.update_resource_calendar(room.id, visibility=CalendarVisibility.INACTIVE)

        room.refresh_from_db()
        assert room.visibility == CalendarVisibility.ACTIVE

    def test_disabling_is_rejected(
        self,
        service: CalendarService,
        location: ResourceLocation,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        room = _create_room(service, location, django_capture_on_commit_callbacks)

        with pytest.raises(ValueError, match="use deleteResourceCalendar"):
            service.disable_resource_calendar(room.id)

        room.refresh_from_db()
        assert room.visibility == CalendarVisibility.ACTIVE
        assert _link(room).sync_status == ResourceSyncStatus.SYNCED

    def test_manual_room_rejects_location_id(
        self, service: CalendarService, location: ResourceLocation
    ) -> None:
        room = service.create_resource_calendar(name="Manual room")

        with pytest.raises(ValueError, match="location_id only applies"):
            service.update_resource_calendar(room.id, location_id=location.id)


@pytest.mark.usefixtures("bound")
class TestPermissionRevokedThenRetried:
    def test_sync_failed_then_retry_then_synced(
        self,
        service: CalendarService,
        location: ResourceLocation,
        directory: FakeRoomDirectory,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        """Spec acceptance scenario 7."""
        with freeze_time(NOW):
            room = _create_room(service, location, django_capture_on_commit_callbacks)
            room.refresh_from_db()
            # The IT admin revokes the permission, and the edit is queued.
            directory.failures = [ResourceDirectoryPermissionError("Access denied")]
            with django_capture_on_commit_callbacks(execute=False) as callbacks:
                service.update_resource_calendar(room.id, capacity=10)

        # The push only runs after the retry window is over: sync failed.
        with freeze_time(NOW + datetime.timedelta(hours=7)):
            for callback in callbacks:
                callback()
        link = _link(room)
        assert link.sync_status == ResourceSyncStatus.SYNC_FAILED
        assert link.failed_operation == ResourceSyncOperation.UPDATE
        assert link.last_error == "Access denied"
        assert link.pending_fields == {"capacity": 10}

        # The permission is restored and an admin retries.
        with (
            freeze_time(NOW + datetime.timedelta(hours=8)),
            django_capture_on_commit_callbacks(execute=True),
        ):
            retried = service.retry_resource_calendar_sync(room.id)

        assert retried.id == room.id
        link.refresh_from_db()
        assert link.sync_status == ResourceSyncStatus.SYNCED
        assert link.failed_operation == ""
        assert link.pending_fields == {}
        assert directory.rooms[room.external_id].capacity == 10

    def test_retry_of_a_room_that_did_not_fail_is_rejected(
        self,
        service: CalendarService,
        location: ResourceLocation,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        room = _create_room(service, location, django_capture_on_commit_callbacks)

        with pytest.raises(RoomSyncStateError):
            service.retry_resource_calendar_sync(room.id)

    def test_retry_of_a_manual_room_is_rejected(self, service: CalendarService) -> None:
        room = service.create_resource_calendar(name="Manual room")

        with pytest.raises(RoomSyncStateError):
            service.retry_resource_calendar_sync(room.id)


@pytest.mark.usefixtures("bound")
class TestFlagOff:
    def test_google_room_is_rejected_with_todays_message(
        self,
        service: CalendarService,
        location: ResourceLocation,
        organization: Organization,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        room = _create_room(service, location, django_capture_on_commit_callbacks)
        OrganizationFeatureFlag.objects.filter_by_organization(organization.id).update(
            enabled=False
        )

        with pytest.raises(ValueError) as exc_info:
            service.update_resource_calendar(room.id, capacity=10)

        assert str(exc_info.value) == (
            f"Calendar {room.id} is synced from an external provider "
            "(provider=google) and cannot be edited."
        )
        room.refresh_from_db()
        assert room.capacity == 8
        assert _link(room).sync_status == ResourceSyncStatus.SYNCED

    def test_disabling_a_google_room_is_unchanged(
        self,
        service: CalendarService,
        location: ResourceLocation,
        organization: Organization,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        room = _create_room(service, location, django_capture_on_commit_callbacks)
        OrganizationFeatureFlag.objects.filter_by_organization(organization.id).update(
            enabled=False
        )

        disabled = service.disable_resource_calendar(room.id)

        assert disabled.visibility == CalendarVisibility.INACTIVE

    def test_retry_is_not_enabled(
        self,
        service: CalendarService,
        location: ResourceLocation,
        organization: Organization,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        room = _create_room(service, location, django_capture_on_commit_callbacks)
        OrganizationFeatureFlag.objects.filter_by_organization(organization.id).update(
            enabled=False
        )

        with pytest.raises(ResourceCalendarProviderSyncNotEnabledError):
            service.retry_resource_calendar_sync(room.id)
