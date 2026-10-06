"""Unit tests for ``RoomSyncService``, the room provider push engine.

The provider is ``FakeRoomDirectory``, an in-memory room directory. The notifier
and the audit service are mocks, except in the acceptance test, which runs the
real ``RoomSyncNotifier`` over a mocked vintasend ``NotificationService`` to count
the admin emails. Queued pushes are caught by patching the task's ``delay``.
"""

import datetime
import uuid
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock, call, patch

from django.utils import timezone

import pytest
from freezegun import freeze_time
from vintasend.services.notification_service import NotificationService

from audit_integration.constants import AuditAction
from audit_integration.services import OrganizationAuditService
from calendar_integration.constants import (
    CalendarProvider,
    CalendarType,
    ResourceSyncOperation,
    ResourceSyncStatus,
)
from calendar_integration.exceptions import (
    ResourceDirectoryError,
    ResourceDirectoryInvalidInputError,
    ResourceDirectoryNotFoundError,
    ResourceDirectoryNotWriteEnabledError,
    ResourceDirectoryPermissionError,
    RoomSyncStateError,
)
from calendar_integration.factories import create_resource_location, create_resource_provider_link
from calendar_integration.models import Calendar, ResourceCalendarProviderLink, ResourceLocation
from calendar_integration.services.dataclasses import RoomDirectoryData, RoomWriteData
from calendar_integration.services.room_sync_notifier import RoomSyncNotifier, RoomSyncOperation
from calendar_integration.services.room_sync_service import (
    PUSH_RETRY_WINDOW,
    RoomPushOutcome,
    RoomSyncService,
)
from calendar_integration.signals import resource_room_archived, resource_room_synced
from calendar_integration.tasks import push_room_to_provider_task
from calendar_integration.tasks.room_sync_tasks import push_retry_countdown
from calendar_integration.tests.room_sync_fakes import FakeRoomDirectory, FakeRoomDirectoryResolver
from common.organization_context import organization_context
from organizations.models import Organization
from organizations.tests.helpers import make_admin_membership
from users.factories import UserFactory


NOW = datetime.datetime(2026, 10, 5, 12, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Room Sync Org")


@pytest.fixture
def bound(organization: Organization) -> Iterator[None]:
    with organization_context(organization):
        yield


@pytest.fixture
def location(organization: Organization, bound: None) -> ResourceLocation:
    return create_resource_location(
        organization=organization, external_building_id="hq", external_floor_id="2"
    )


@pytest.fixture
def room(organization: Organization, bound: None) -> Calendar:
    return Calendar.objects.create(
        organization=organization,
        name="Boardroom",
        description="Big table",
        capacity=8,
        external_id=f"pending-{uuid.uuid4()}",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.RESOURCE,
    )


@pytest.fixture
def directory() -> FakeRoomDirectory:
    return FakeRoomDirectory()


@pytest.fixture
def resolver(directory: FakeRoomDirectory) -> FakeRoomDirectoryResolver:
    return FakeRoomDirectoryResolver(directory)


@pytest.fixture
def notifier() -> MagicMock:
    return MagicMock(spec=RoomSyncNotifier)


@pytest.fixture
def audit_service() -> MagicMock:
    return MagicMock(spec=OrganizationAuditService)


@pytest.fixture
def service(
    resolver: FakeRoomDirectoryResolver, notifier: MagicMock, audit_service: MagicMock
) -> RoomSyncService:
    return RoomSyncService(
        resource_directory_adapter_resolver=resolver,
        room_sync_notifier=notifier,
        audit_service=audit_service,
    )


@pytest.fixture
def enqueued() -> Iterator[MagicMock]:
    """The push task's ``delay``, so tests see what was queued without running it."""
    with patch.object(push_room_to_provider_task, "delay") as delay:
        yield delay


@pytest.fixture
def sentry_capture() -> Iterator[MagicMock]:
    with patch(
        "calendar_integration.services.room_sync_service.sentry_sdk.capture_message"
    ) as capture:
        yield capture


@pytest.fixture
def signals_received() -> Iterator[list[tuple[str, dict[str, Any]]]]:
    received: list[tuple[str, dict[str, Any]]] = []

    def on_synced(sender: Any, **kwargs: Any) -> None:
        received.append(("synced", {k: v for k, v in kwargs.items() if k != "signal"}))

    def on_archived(sender: Any, **kwargs: Any) -> None:
        received.append(("archived", {k: v for k, v in kwargs.items() if k != "signal"}))

    resource_room_synced.connect(on_synced)
    resource_room_archived.connect(on_archived)
    try:
        yield received
    finally:
        resource_room_synced.disconnect(on_synced)
        resource_room_archived.disconnect(on_archived)


def _values(location: ResourceLocation | None, **overrides: Any) -> dict[str, Any]:
    """The room fixture's synced values, in the ``pending_fields`` shape."""
    return {
        "name": "Boardroom",
        "description": "Big table",
        "capacity": 8,
        "location_ref": location.location_ref if location is not None else None,
        **overrides,
    }


def _synced_on_provider(
    room: Calendar,
    directory: FakeRoomDirectory,
    location: ResourceLocation,
    **link_kwargs: Any,
) -> ResourceCalendarProviderLink:
    """A room that exists on the provider, with a ``SYNCED`` link by default."""
    external_id = f"vinta-{uuid.uuid4()}"
    directory.rooms[external_id] = RoomDirectoryData(
        external_id=external_id,
        email=f"{external_id}@resource.example.com",
        name="Boardroom",
        description="Big table",
        capacity=8,
        location_ref=None,
    )
    room.external_id = external_id
    room.email = f"{external_id}@resource.example.com"
    room.save(update_fields=["external_id", "email"])
    link_kwargs.setdefault("provider_snapshot", _values(location))
    link_kwargs.setdefault("last_synced_at", NOW - datetime.timedelta(days=1))
    return create_resource_provider_link(calendar=room, location=location, **link_kwargs)


def _state(link: ResourceCalendarProviderLink) -> dict[str, Any]:
    link.refresh_from_db()
    return {
        "sync_status": link.sync_status,
        "failed_operation": link.failed_operation,
        "last_error": link.last_error,
        "attempt_count": link.attempt_count,
        "retry_deadline": link.retry_deadline,
    }


def _queued(link: ResourceCalendarProviderLink, attempt_count: int = 0) -> Any:
    return call(link_id=link.pk, organization_id=link.organization_id, attempt_count=attempt_count)


# ---------------------------------------------------------------------------
# request_push
# ---------------------------------------------------------------------------


@freeze_time(NOW)
class TestRequestPush:
    def test_create_marks_pending_and_queues_after_commit(
        self,
        service: RoomSyncService,
        room: Calendar,
        location: ResourceLocation,
        enqueued: MagicMock,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        link = create_resource_provider_link(
            calendar=room,
            location=location,
            sync_status=ResourceSyncStatus.PENDING_CREATION,
            pending_fields=_values(location),
        )

        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            service.request_push(link, ResourceSyncOperation.CREATE)
            assert enqueued.call_count == 0
        for callback in callbacks:
            callback()

        assert _state(link) == {
            "sync_status": ResourceSyncStatus.PENDING_CREATION,
            "failed_operation": "",
            "last_error": "",
            "attempt_count": 0,
            "retry_deadline": NOW + PUSH_RETRY_WINDOW,
        }
        assert enqueued.call_args_list == [_queued(link)]

    def test_update_from_synced(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
        enqueued: MagicMock,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        link = _synced_on_provider(room, directory, location, pending_fields={"name": "Annex"})

        with django_capture_on_commit_callbacks(execute=True):
            service.request_push(link, ResourceSyncOperation.UPDATE, {"capacity": 10})

        assert link.sync_status == ResourceSyncStatus.PENDING_UPDATE
        assert link.pending_fields == {"name": "Annex", "capacity": 10}
        link.refresh_from_db()
        assert link.pending_fields == {"name": "Annex", "capacity": 10}
        assert _state(link) == {
            "sync_status": ResourceSyncStatus.PENDING_UPDATE,
            "failed_operation": "",
            "last_error": "",
            "attempt_count": 0,
            "retry_deadline": NOW + PUSH_RETRY_WINDOW,
        }
        assert enqueued.call_args_list == [_queued(link)]

    def test_update_while_pending_keeps_the_existing_deadline(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
        enqueued: MagicMock,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        deadline = NOW + datetime.timedelta(hours=1)
        link = _synced_on_provider(
            room,
            directory,
            location,
            sync_status=ResourceSyncStatus.PENDING_UPDATE,
            retry_deadline=deadline,
            attempt_count=3,
        )

        with django_capture_on_commit_callbacks(execute=True):
            service.request_push(link, ResourceSyncOperation.UPDATE)

        assert _state(link)["retry_deadline"] == deadline
        assert _state(link)["attempt_count"] == 3
        # Queued with the current count: it takes over the chain the scheduled
        # attempt belongs to, and whichever of the two runs second does nothing.
        assert enqueued.call_args_list == [_queued(link, attempt_count=3)]

    def test_update_while_pending_creation_is_merged_into_the_create(
        self,
        service: RoomSyncService,
        room: Calendar,
        location: ResourceLocation,
        enqueued: MagicMock,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        deadline = NOW + datetime.timedelta(hours=2)
        link = create_resource_provider_link(
            calendar=room,
            location=location,
            sync_status=ResourceSyncStatus.PENDING_CREATION,
            pending_fields=_values(location),
            retry_deadline=deadline,
        )

        with django_capture_on_commit_callbacks(execute=True):
            service.request_push(link, ResourceSyncOperation.UPDATE, {"capacity": 10})

        assert _state(link)["sync_status"] == ResourceSyncStatus.PENDING_CREATION
        assert _state(link)["retry_deadline"] == deadline
        assert link.pending_fields == _values(location, capacity=10)
        assert enqueued.call_args_list == [_queued(link)]

    @pytest.mark.parametrize(
        "failed_operation", [ResourceSyncOperation.CREATE, ResourceSyncOperation.UPDATE]
    )
    def test_update_while_sync_failed_waits_for_a_retry(
        self,
        service: RoomSyncService,
        room: Calendar,
        location: ResourceLocation,
        enqueued: MagicMock,
        django_capture_on_commit_callbacks: Callable[..., Any],
        failed_operation: str,
    ) -> None:
        link = create_resource_provider_link(
            calendar=room,
            location=location,
            sync_status=ResourceSyncStatus.SYNC_FAILED,
            failed_operation=failed_operation,
            last_error="Provider unavailable",
        )

        with django_capture_on_commit_callbacks(execute=True):
            service.request_push(link, ResourceSyncOperation.UPDATE, {"capacity": 10})

        assert _state(link)["sync_status"] == ResourceSyncStatus.SYNC_FAILED
        assert _state(link)["failed_operation"] == failed_operation
        assert link.pending_fields == {"capacity": 10}
        assert enqueued.call_count == 0

    @pytest.mark.parametrize(
        ("sync_status", "failed_operation"),
        [
            (ResourceSyncStatus.PENDING_DELETION, ""),
            (ResourceSyncStatus.SYNC_FAILED, ResourceSyncOperation.DELETE),
            (ResourceSyncStatus.ARCHIVED, ""),
        ],
    )
    def test_update_of_a_room_on_its_way_out_is_refused(
        self,
        service: RoomSyncService,
        room: Calendar,
        location: ResourceLocation,
        enqueued: MagicMock,
        sync_status: str,
        failed_operation: str,
    ) -> None:
        link = create_resource_provider_link(
            calendar=room,
            location=location,
            sync_status=sync_status,
            failed_operation=failed_operation,
        )

        with pytest.raises(RoomSyncStateError):
            service.request_push(link, ResourceSyncOperation.UPDATE, {"capacity": 10})

        assert _state(link)["sync_status"] == sync_status
        assert link.pending_fields == {}
        assert enqueued.call_count == 0

    @pytest.mark.parametrize(
        "sync_status",
        [ResourceSyncStatus.SYNCED, ResourceSyncStatus.PENDING_UPDATE],
    )
    def test_delete_of_a_room_on_the_provider(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
        enqueued: MagicMock,
        django_capture_on_commit_callbacks: Callable[..., Any],
        sync_status: str,
    ) -> None:
        link = _synced_on_provider(room, directory, location, sync_status=sync_status)

        with django_capture_on_commit_callbacks(execute=True):
            service.request_push(link, ResourceSyncOperation.DELETE)

        assert _state(link)["sync_status"] == ResourceSyncStatus.PENDING_DELETION
        assert link.archived_at == NOW
        assert enqueued.call_args_list == [_queued(link)]

    @pytest.mark.parametrize(
        ("sync_status", "failed_operation"),
        [
            (ResourceSyncStatus.PENDING_CREATION, ""),
            (ResourceSyncStatus.SYNC_FAILED, ResourceSyncOperation.CREATE),
        ],
    )
    def test_delete_before_the_create_reached_the_provider_archives_directly(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
        enqueued: MagicMock,
        django_capture_on_commit_callbacks: Callable[..., Any],
        sync_status: str,
        failed_operation: str,
    ) -> None:
        link = create_resource_provider_link(
            calendar=room,
            location=location,
            sync_status=sync_status,
            failed_operation=failed_operation,
            attempt_count=4,
            retry_deadline=NOW,
        )

        with django_capture_on_commit_callbacks(execute=True):
            service.request_push(link, ResourceSyncOperation.DELETE)

        link.refresh_from_db()
        assert link.sync_status == ResourceSyncStatus.ARCHIVED
        assert link.archived_at == NOW
        assert _state(link) == {
            "sync_status": ResourceSyncStatus.ARCHIVED,
            "failed_operation": "",
            "last_error": "",
            "attempt_count": 0,
            "retry_deadline": None,
        }
        assert enqueued.call_count == 0
        assert directory.calls == []

    def test_create_of_a_room_already_on_the_provider_is_refused(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
    ) -> None:
        link = _synced_on_provider(room, directory, location)

        with pytest.raises(RoomSyncStateError):
            service.request_push(link, ResourceSyncOperation.CREATE)

    def test_field_outside_the_synced_set_is_refused(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
        enqueued: MagicMock,
    ) -> None:
        link = _synced_on_provider(room, directory, location)

        with pytest.raises(ValueError, match="is_private"):
            service.request_push(link, ResourceSyncOperation.UPDATE, {"is_private": True})

        assert _state(link)["sync_status"] == ResourceSyncStatus.SYNCED
        assert enqueued.call_count == 0

    def test_repeated_edits_of_a_failing_push_keep_one_retry_chain(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
        enqueued: MagicMock,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        link = _synced_on_provider(room, directory, location)
        with django_capture_on_commit_callbacks(execute=True):
            service.request_push(link, ResourceSyncOperation.UPDATE, {"capacity": 10})
            service.request_push(link, ResourceSyncOperation.UPDATE, {"capacity": 11})
            service.request_push(link, ResourceSyncOperation.UPDATE, {"name": "Annex"})
        assert enqueued.call_count == 3
        directory.failures = [ResourceDirectoryError("Rate limited")] * 3

        outcomes = [
            service.push(queued.kwargs["link_id"], queued.kwargs["attempt_count"])
            for queued in enqueued.call_args_list
        ]

        # The first task fails and queues attempt 1; the other two are stale.
        assert outcomes == [
            RoomPushOutcome(retry_attempt=1, retry_deadline=NOW + PUSH_RETRY_WINDOW),
            RoomPushOutcome(),
            RoomPushOutcome(),
        ]
        assert directory.calls == ["update_room"]
        assert _state(link)["attempt_count"] == 1


# ---------------------------------------------------------------------------
# push: success
# ---------------------------------------------------------------------------


@freeze_time(NOW)
class TestPushSuccess:
    def test_create(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
        audit_service: MagicMock,
        signals_received: list[tuple[str, dict[str, Any]]],
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        link = create_resource_provider_link(
            calendar=room,
            location=location,
            sync_status=ResourceSyncStatus.PENDING_CREATION,
            pending_fields=_values(location),
            attempt_count=2,
            retry_deadline=NOW + datetime.timedelta(hours=1),
        )

        with django_capture_on_commit_callbacks(execute=True):
            outcome = service.push(link.pk, link.attempt_count)

        assert outcome == RoomPushOutcome()
        external_id = f"vinta-{link.provisional_key}"
        assert directory.calls == ["create_room"]
        room.refresh_from_db()
        assert (room.external_id, room.email) == (
            external_id,
            f"{external_id}@resource.example.com",
        )
        link.refresh_from_db()
        assert _state(link) == {
            "sync_status": ResourceSyncStatus.SYNCED,
            "failed_operation": "",
            "last_error": "",
            "attempt_count": 0,
            "retry_deadline": None,
        }
        assert link.pending_fields == {}
        assert link.provider_snapshot == _values(location)
        assert link.last_synced_at == NOW
        assert signals_received == [
            (
                "synced",
                {
                    "calendar_id": room.id,
                    "organization_id": room.organization_id,
                    "provider": CalendarProvider.GOOGLE,
                    "created": True,
                },
            )
        ]
        audit_service.record.assert_called_once_with(
            action=AuditAction.ROOM_PROVIDER_SYNCED,
            actor=audit_service.system_actor.return_value,
            subject=audit_service.subject_from_instance.return_value,
            diff={
                "operation": ResourceSyncOperation.CREATE,
                "provider": CalendarProvider.GOOGLE,
                "fields": ["capacity", "description", "location_ref", "name"],
            },
            scope=audit_service.scope_from_organization_id.return_value,
        )
        audit_service.scope_from_organization_id.assert_called_once_with(room.organization_id)

    def test_create_sends_calendar_values_under_the_pending_edits(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
    ) -> None:
        link = create_resource_provider_link(
            calendar=room,
            location=location,
            sync_status=ResourceSyncStatus.PENDING_CREATION,
            pending_fields={"name": "Boardroom West"},
        )

        service.push(link.pk, link.attempt_count)

        created = directory.rooms[f"vinta-{link.provisional_key}"]
        assert (created.name, created.description, created.capacity) == (
            "Boardroom West",
            "Big table",
            8,
        )
        assert created.location_ref is not None
        assert created.location_ref.to_dict() == location.location_ref

    def test_replayed_push_after_a_successful_create_does_nothing(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
        audit_service: MagicMock,
    ) -> None:
        link = create_resource_provider_link(
            calendar=room,
            location=location,
            sync_status=ResourceSyncStatus.PENDING_CREATION,
            pending_fields=_values(location),
        )
        service.push(link.pk, link.attempt_count)

        outcome = service.push(link.pk, link.attempt_count)

        assert outcome == RoomPushOutcome()
        assert directory.calls == ["create_room"]
        assert len(directory.rooms) == 1
        assert audit_service.record.call_count == 1

    def test_create_replayed_after_a_lost_commit_finds_the_room_it_made(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
    ) -> None:
        # The first attempt reached the provider, then the worker died before the
        # commit: the link is still pending creation, and the room already exists.
        link = create_resource_provider_link(
            calendar=room,
            location=location,
            sync_status=ResourceSyncStatus.PENDING_CREATION,
            pending_fields=_values(location),
        )
        directory.create_room(
            RoomWriteData.from_synced_values(_values(location), link.provisional_key)
        )

        service.push(link.pk, link.attempt_count)

        assert list(directory.rooms) == [f"vinta-{link.provisional_key}"]
        assert _state(link)["sync_status"] == ResourceSyncStatus.SYNCED

    def test_update_sends_only_the_pending_fields(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
        audit_service: MagicMock,
        signals_received: list[tuple[str, dict[str, Any]]],
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        link = _synced_on_provider(
            room,
            directory,
            location,
            sync_status=ResourceSyncStatus.PENDING_UPDATE,
            pending_fields={"capacity": 10},
            retry_deadline=NOW + datetime.timedelta(hours=1),
        )
        # The provider renamed the room since the last sync; the push must not undo it.
        directory.rooms[room.external_id].name = "Renamed by IT"

        with django_capture_on_commit_callbacks(execute=True):
            service.push(link.pk, link.attempt_count)

        provider_room = directory.rooms[room.external_id]
        assert (provider_room.name, provider_room.capacity) == ("Renamed by IT", 10)
        link.refresh_from_db()
        assert link.sync_status == ResourceSyncStatus.SYNCED
        assert link.pending_fields == {}
        assert link.provider_snapshot == _values(location, capacity=10)
        assert link.retry_deadline is None
        assert signals_received == []
        assert audit_service.record.call_args.kwargs["diff"] == {
            "operation": ResourceSyncOperation.UPDATE,
            "provider": CalendarProvider.GOOGLE,
            "fields": ["capacity"],
        }

    def test_update_with_nothing_pending_does_not_call_the_provider(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
    ) -> None:
        link = _synced_on_provider(
            room, directory, location, sync_status=ResourceSyncStatus.PENDING_UPDATE
        )

        service.push(link.pk, link.attempt_count)

        assert directory.calls == []
        assert _state(link)["sync_status"] == ResourceSyncStatus.SYNCED

    def test_delete(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
        audit_service: MagicMock,
        signals_received: list[tuple[str, dict[str, Any]]],
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        link = _synced_on_provider(
            room, directory, location, sync_status=ResourceSyncStatus.PENDING_DELETION
        )

        with django_capture_on_commit_callbacks(execute=True):
            service.push(link.pk, link.attempt_count)

        assert directory.rooms == {}
        link.refresh_from_db()
        assert link.sync_status == ResourceSyncStatus.ARCHIVED
        assert link.archived_at == NOW
        assert signals_received == [
            (
                "archived",
                {
                    "calendar_id": room.id,
                    "organization_id": room.organization_id,
                    "provider": CalendarProvider.GOOGLE,
                },
            )
        ]
        assert audit_service.record.call_args.kwargs["action"] == AuditAction.ROOM_PROVIDER_SYNCED

    def test_delete_keeps_an_archived_at_set_when_the_delete_was_accepted(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
    ) -> None:
        accepted_at = NOW - datetime.timedelta(minutes=3)
        link = _synced_on_provider(
            room,
            directory,
            location,
            sync_status=ResourceSyncStatus.PENDING_DELETION,
            archived_at=accepted_at,
        )

        service.push(link.pk, link.attempt_count)

        link.refresh_from_db()
        assert link.archived_at == accepted_at

    def test_delete_of_a_room_already_gone_on_the_provider_completes(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
        notifier: MagicMock,
    ) -> None:
        link = _synced_on_provider(
            room, directory, location, sync_status=ResourceSyncStatus.PENDING_DELETION
        )
        directory.rooms.clear()

        service.push(link.pk, link.attempt_count)

        assert _state(link)["sync_status"] == ResourceSyncStatus.ARCHIVED
        assert notifier.notify_sync_failed.call_count == 0

    @pytest.mark.parametrize(
        ("sync_status", "failed_operation"),
        [
            (ResourceSyncStatus.SYNCED, ""),
            (ResourceSyncStatus.SYNC_FAILED, ResourceSyncOperation.UPDATE),
            (ResourceSyncStatus.ARCHIVED, ""),
        ],
    )
    def test_push_of_a_link_that_is_not_pending_does_nothing(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
        sync_status: str,
        failed_operation: str,
    ) -> None:
        link = _synced_on_provider(
            room,
            directory,
            location,
            sync_status=sync_status,
            failed_operation=failed_operation,
            pending_fields={"capacity": 10},
        )
        before = _state(link)

        assert service.push(link.pk, link.attempt_count) == RoomPushOutcome()

        assert directory.calls == []
        assert _state(link) == before

    def test_push_of_a_missing_link_does_nothing(
        self, service: RoomSyncService, bound: None, directory: FakeRoomDirectory
    ) -> None:
        assert service.push(999_999, 0) == RoomPushOutcome()
        assert directory.calls == []

    def test_push_with_a_stale_attempt_count_does_nothing(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
    ) -> None:
        # Attempt 3 already ran and queued attempt 4; this is a leftover task.
        link = _synced_on_provider(
            room,
            directory,
            location,
            sync_status=ResourceSyncStatus.PENDING_UPDATE,
            pending_fields={"capacity": 10},
            attempt_count=4,
            retry_deadline=NOW + datetime.timedelta(hours=1),
        )
        before = _state(link)

        assert service.push(link.pk, 3) == RoomPushOutcome()

        assert directory.calls == []
        assert _state(link) == before


# ---------------------------------------------------------------------------
# push: failure
# ---------------------------------------------------------------------------


@freeze_time(NOW)
class TestPushFailure:
    @pytest.mark.parametrize(
        "error",
        [
            ResourceDirectoryError("Provider unavailable"),
            ResourceDirectoryPermissionError("Access denied"),
        ],
        ids=["transient", "permission"],
    )
    def test_retryable_error_before_the_deadline_reschedules(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
        notifier: MagicMock,
        sentry_capture: MagicMock,
        error: ResourceDirectoryError,
    ) -> None:
        deadline = NOW + datetime.timedelta(hours=5)
        link = _synced_on_provider(
            room,
            directory,
            location,
            sync_status=ResourceSyncStatus.PENDING_UPDATE,
            pending_fields={"capacity": 10},
            attempt_count=2,
            retry_deadline=deadline,
        )
        directory.failures = [error]

        outcome = service.push(link.pk, link.attempt_count)

        assert outcome == RoomPushOutcome(retry_attempt=3, retry_deadline=deadline)
        assert _state(link) == {
            "sync_status": ResourceSyncStatus.PENDING_UPDATE,
            "failed_operation": "",
            "last_error": str(error),
            "attempt_count": 3,
            "retry_deadline": deadline,
        }
        link.refresh_from_db()
        assert link.pending_fields == {"capacity": 10}
        assert notifier.notify_sync_failed.call_count == 0
        assert sentry_capture.call_count == 0

    def test_not_write_enabled_after_the_check_is_retried(
        self,
        service: RoomSyncService,
        resolver: FakeRoomDirectoryResolver,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
    ) -> None:
        # Write access is revoked between the flag check and the adapter lookup.
        link = _synced_on_provider(
            room,
            directory,
            location,
            sync_status=ResourceSyncStatus.PENDING_UPDATE,
            pending_fields={"capacity": 10},
            retry_deadline=NOW + datetime.timedelta(hours=1),
        )
        with patch.object(
            resolver, "adapter_for", side_effect=ResourceDirectoryNotWriteEnabledError()
        ):
            outcome = service.push(link.pk, link.attempt_count)

        assert outcome == RoomPushOutcome(
            retry_attempt=1, retry_deadline=NOW + datetime.timedelta(hours=1)
        )
        assert _state(link)["sync_status"] == ResourceSyncStatus.PENDING_UPDATE

    def test_transient_error_after_the_deadline_fails_notifies_and_alerts(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
        notifier: MagicMock,
        audit_service: MagicMock,
        sentry_capture: MagicMock,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        link = _synced_on_provider(
            room,
            directory,
            location,
            sync_status=ResourceSyncStatus.PENDING_UPDATE,
            pending_fields={"capacity": 10},
            attempt_count=11,
            retry_deadline=NOW - datetime.timedelta(seconds=1),
        )
        directory.failures = [ResourceDirectoryError("Provider unavailable")]

        with django_capture_on_commit_callbacks(execute=True):
            outcome = service.push(link.pk, link.attempt_count)

        assert outcome == RoomPushOutcome()
        assert _state(link) == {
            "sync_status": ResourceSyncStatus.SYNC_FAILED,
            "failed_operation": ResourceSyncOperation.UPDATE,
            "last_error": "Provider unavailable",
            "attempt_count": 12,
            "retry_deadline": NOW - datetime.timedelta(seconds=1),
        }
        notifier.notify_sync_failed.assert_called_once_with(
            room.id, RoomSyncOperation.UPDATE, "Provider unavailable"
        )
        sentry_capture.assert_called_once_with("Room provider sync failed", level="error")
        assert audit_service.record.call_args.kwargs["action"] == (
            AuditAction.ROOM_PROVIDER_SYNC_FAILED
        )
        assert audit_service.record.call_args.kwargs["diff"] == {
            "operation": ResourceSyncOperation.UPDATE,
            "provider": CalendarProvider.GOOGLE,
            "error": "Provider unavailable",
        }

    def test_sentry_event_carries_only_opaque_ids(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        link = _synced_on_provider(
            room,
            directory,
            location,
            sync_status=ResourceSyncStatus.PENDING_UPDATE,
            pending_fields={"capacity": 10},
        )
        directory.failures = [ResourceDirectoryInvalidInputError("Boardroom: bad capacity")]
        scope = MagicMock()

        with (
            patch(
                "calendar_integration.services.room_sync_service.sentry_sdk.new_scope"
            ) as new_scope,
            patch("calendar_integration.services.room_sync_service.sentry_sdk.capture_message"),
            django_capture_on_commit_callbacks(execute=True),
        ):
            new_scope.return_value.__enter__.return_value = scope
            service.push(link.pk, link.attempt_count)

        tags = {call.args[0]: call.args[1] for call in scope.set_tag.call_args_list}
        assert tags == {
            "room_sync.link_id": str(link.pk),
            "room_sync.calendar_id": str(room.id),
            "room_sync.organization_id": str(room.organization_id),
            "room_sync.provider": CalendarProvider.GOOGLE,
            "room_sync.operation": ResourceSyncOperation.UPDATE,
            "room_sync.error_type": "ResourceDirectoryInvalidInputError",
        }

    @pytest.mark.parametrize(
        ("sync_status", "error", "failed_operation"),
        [
            (
                ResourceSyncStatus.PENDING_CREATION,
                ResourceDirectoryInvalidInputError("Unknown building."),
                ResourceSyncOperation.CREATE,
            ),
            (
                ResourceSyncStatus.PENDING_UPDATE,
                ResourceDirectoryInvalidInputError("Capacity must be positive."),
                ResourceSyncOperation.UPDATE,
            ),
            (
                ResourceSyncStatus.PENDING_UPDATE,
                ResourceDirectoryNotFoundError("Room not found."),
                ResourceSyncOperation.UPDATE,
            ),
            (
                ResourceSyncStatus.PENDING_DELETION,
                ResourceDirectoryInvalidInputError("Room cannot be deleted."),
                ResourceSyncOperation.DELETE,
            ),
        ],
    )
    def test_non_transient_error_fails_immediately(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
        notifier: MagicMock,
        sentry_capture: MagicMock,
        django_capture_on_commit_callbacks: Callable[..., Any],
        sync_status: str,
        error: ResourceDirectoryError,
        failed_operation: str,
    ) -> None:
        link = _synced_on_provider(
            room,
            directory,
            location,
            sync_status=sync_status,
            pending_fields={"capacity": 10},
            retry_deadline=NOW + PUSH_RETRY_WINDOW,
        )
        directory.failures = [error]

        with django_capture_on_commit_callbacks(execute=True):
            outcome = service.push(link.pk, link.attempt_count)

        assert outcome == RoomPushOutcome()
        assert _state(link) == {
            "sync_status": ResourceSyncStatus.SYNC_FAILED,
            "failed_operation": failed_operation,
            "last_error": str(error),
            "attempt_count": 1,
            "retry_deadline": NOW + PUSH_RETRY_WINDOW,
        }
        notifier.notify_sync_failed.assert_called_once_with(
            room.id, RoomSyncOperation(failed_operation), str(error)
        )
        assert sentry_capture.call_count == 1

    def test_unexpected_adapter_error_is_retried_with_a_generic_message(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
    ) -> None:
        link = _synced_on_provider(
            room,
            directory,
            location,
            sync_status=ResourceSyncStatus.PENDING_UPDATE,
            pending_fields={"capacity": 10},
            retry_deadline=NOW + PUSH_RETRY_WINDOW,
        )
        directory.failures = [KeyError("capacity")]

        outcome = service.push(link.pk, link.attempt_count)

        assert outcome == RoomPushOutcome(retry_attempt=1, retry_deadline=NOW + PUSH_RETRY_WINDOW)
        assert _state(link)["last_error"] == (
            "Unexpected error while pushing the room to the provider."
        )

    def test_failure_with_no_deadline_starts_one(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
    ) -> None:
        link = _synced_on_provider(
            room,
            directory,
            location,
            sync_status=ResourceSyncStatus.PENDING_UPDATE,
            pending_fields={"capacity": 10},
        )
        directory.failures = [ResourceDirectoryError("Provider unavailable")]

        assert service.push(link.pk, link.attempt_count) == RoomPushOutcome(
            retry_attempt=1, retry_deadline=NOW + PUSH_RETRY_WINDOW
        )
        assert _state(link)["retry_deadline"] == NOW + PUSH_RETRY_WINDOW

    def test_flag_turned_off_mid_flight_leaves_the_link_untouched(
        self,
        service: RoomSyncService,
        resolver: FakeRoomDirectoryResolver,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
        notifier: MagicMock,
    ) -> None:
        link = _synced_on_provider(
            room,
            directory,
            location,
            sync_status=ResourceSyncStatus.PENDING_UPDATE,
            pending_fields={"capacity": 10},
            attempt_count=1,
            retry_deadline=NOW - datetime.timedelta(minutes=1),
        )
        resolver.write_enabled = False
        before = _state(link)

        assert service.push(link.pk, link.attempt_count) == RoomPushOutcome()

        assert _state(link) == before
        link.refresh_from_db()
        assert link.pending_fields == {"capacity": 10}
        assert directory.calls == []
        assert notifier.notify_sync_failed.call_count == 0


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------


@freeze_time(NOW)
class TestRetry:
    @pytest.mark.parametrize(
        ("failed_operation", "expected_status"),
        [
            (ResourceSyncOperation.CREATE, ResourceSyncStatus.PENDING_CREATION),
            (ResourceSyncOperation.UPDATE, ResourceSyncStatus.PENDING_UPDATE),
            (ResourceSyncOperation.DELETE, ResourceSyncStatus.PENDING_DELETION),
        ],
    )
    def test_retry_repushes_the_failed_operation(
        self,
        service: RoomSyncService,
        room: Calendar,
        location: ResourceLocation,
        enqueued: MagicMock,
        django_capture_on_commit_callbacks: Callable[..., Any],
        failed_operation: str,
        expected_status: str,
    ) -> None:
        link = create_resource_provider_link(
            calendar=room,
            location=location,
            sync_status=ResourceSyncStatus.SYNC_FAILED,
            failed_operation=failed_operation,
            last_error="Provider unavailable",
            attempt_count=12,
            retry_deadline=NOW - datetime.timedelta(hours=1),
        )

        with django_capture_on_commit_callbacks(execute=True):
            service.retry(link)

        assert link.sync_status == expected_status
        assert _state(link) == {
            "sync_status": expected_status,
            "failed_operation": "",
            "last_error": "",
            "attempt_count": 0,
            "retry_deadline": NOW + PUSH_RETRY_WINDOW,
        }
        assert enqueued.call_args_list == [_queued(link)]

    def test_retry_of_a_failed_update_sends_the_edits_made_while_it_was_failed(
        self,
        service: RoomSyncService,
        room: Calendar,
        directory: FakeRoomDirectory,
        location: ResourceLocation,
        enqueued: MagicMock,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        link = _synced_on_provider(
            room,
            directory,
            location,
            sync_status=ResourceSyncStatus.SYNC_FAILED,
            failed_operation=ResourceSyncOperation.UPDATE,
            pending_fields={"capacity": 10},
        )
        service.request_push(link, ResourceSyncOperation.UPDATE, {"name": "Annex"})

        with django_capture_on_commit_callbacks(execute=True):
            service.retry(link)
        service.push(link.pk, 0)

        provider_room = directory.rooms[room.external_id]
        assert (provider_room.name, provider_room.capacity) == ("Annex", 10)
        assert _state(link)["sync_status"] == ResourceSyncStatus.SYNCED

    @pytest.mark.parametrize(
        "sync_status",
        [
            ResourceSyncStatus.PENDING_CREATION,
            ResourceSyncStatus.SYNCED,
            ResourceSyncStatus.PENDING_UPDATE,
            ResourceSyncStatus.PENDING_DELETION,
            ResourceSyncStatus.ARCHIVED,
        ],
    )
    def test_retry_is_only_allowed_from_sync_failed(
        self,
        service: RoomSyncService,
        room: Calendar,
        location: ResourceLocation,
        enqueued: MagicMock,
        sync_status: str,
    ) -> None:
        link = create_resource_provider_link(
            calendar=room, location=location, sync_status=sync_status
        )

        with pytest.raises(RoomSyncStateError):
            service.retry(link)

        assert _state(link)["sync_status"] == sync_status
        assert enqueued.call_count == 0


# ---------------------------------------------------------------------------
# Acceptance: 6 hours and 1 minute of transient failures
# ---------------------------------------------------------------------------


def test_transient_failures_past_the_window_end_in_sync_failed_and_one_email(
    organization: Organization,
    bound: None,
    room: Calendar,
    directory: FakeRoomDirectory,
    resolver: FakeRoomDirectoryResolver,
    location: ResourceLocation,
    enqueued: MagicMock,
    sentry_capture: MagicMock,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    make_admin_membership(
        user=UserFactory().create_user(email="admin@example.com"),
        organization=organization,
        is_active=True,
    )
    notification_service = MagicMock(spec=NotificationService)
    service = RoomSyncService(
        resource_directory_adapter_resolver=resolver,
        room_sync_notifier=RoomSyncNotifier(notification_service=notification_service),
    )
    link = _synced_on_provider(room, directory, location)

    with freeze_time(NOW) as frozen, django_capture_on_commit_callbacks(execute=True):
        service.request_push(link, ResourceSyncOperation.UPDATE, {"capacity": 10})
        # Fails transiently on every attempt until 6 hours and 1 minute have passed.
        give_up_at = NOW + PUSH_RETRY_WINDOW + datetime.timedelta(minutes=1)
        statuses = [_state(link)["sync_status"]]
        attempt_count = 0
        for _attempt in range(100):  # bounded, so a regression fails instead of hanging
            if timezone.now() < give_up_at:
                directory.failures = [ResourceDirectoryError("Provider unavailable")]
            outcome = service.push(link.pk, attempt_count)
            if outcome.retry_attempt is None:
                break
            attempt_count = outcome.retry_attempt
            statuses.append(_state(link)["sync_status"])
            countdown = push_retry_countdown(outcome.retry_attempt, outcome.retry_deadline)
            frozen.tick(datetime.timedelta(seconds=countdown))

    assert set(statuses) == {ResourceSyncStatus.PENDING_UPDATE}
    assert _state(link)["sync_status"] == ResourceSyncStatus.SYNC_FAILED
    assert _state(link)["failed_operation"] == ResourceSyncOperation.UPDATE
    assert notification_service.create_notification.call_count == 1
    assert sentry_capture.call_count == 1

    with freeze_time(NOW + datetime.timedelta(hours=7)):
        service.retry(link)

    assert _state(link)["sync_status"] == ResourceSyncStatus.PENDING_UPDATE
