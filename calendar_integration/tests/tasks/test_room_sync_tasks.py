"""Integration tests for ``push_room_to_provider_task``.

The task binds the link's organization, runs ``RoomSyncService.push`` and queues
the next attempt with a backoff countdown. These tests cover the binding, the
countdown sequence, the wiring through the DI container, and that two pushes of
one link, or a push and an edit, serialize on the link's row lock.
"""

import datetime
import threading
import uuid
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

from django.db import connection

import pytest
from freezegun import freeze_time

from calendar_integration.constants import (
    CalendarProvider,
    CalendarType,
    ResourceSyncOperation,
    ResourceSyncStatus,
)
from calendar_integration.factories import create_resource_location, create_resource_provider_link
from calendar_integration.models import Calendar, ResourceCalendarProviderLink
from calendar_integration.services.room_sync_notifier import RoomSyncNotifier
from calendar_integration.services.room_sync_service import RoomPushOutcome, RoomSyncService
from calendar_integration.tasks import push_room_to_provider_task
from calendar_integration.tasks.room_sync_tasks import push_retry_countdown
from calendar_integration.tests.room_sync_fakes import FakeRoomDirectory, FakeRoomDirectoryResolver
from common.organization_context import get_current_organization, organization_context
from organizations.models import Organization


NOW = datetime.datetime(2026, 10, 5, 12, 0, tzinfo=datetime.UTC)


def _make_pending_room(organization: Organization) -> ResourceCalendarProviderLink:
    with organization_context(organization):
        location = create_resource_location(organization=organization)
        room = Calendar.objects.create(
            organization=organization,
            name="Boardroom",
            capacity=8,
            external_id=f"pending-{uuid.uuid4()}",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.RESOURCE,
        )
        return create_resource_provider_link(
            calendar=room,
            location=location,
            sync_status=ResourceSyncStatus.PENDING_CREATION,
            pending_fields={"name": "Boardroom"},
        )


class _RecordingService:
    """Stands in for ``RoomSyncService``: records what ``push`` saw and returns ``outcome``."""

    def __init__(self, outcome: RoomPushOutcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple[int, int, Organization | None]] = []

    def push(self, link_id: int, attempt_count: int) -> RoomPushOutcome:
        self.calls.append((link_id, attempt_count, get_current_organization()))
        return self.outcome


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Room Push Task Org")


class TestPushTask:
    def test_binds_the_organization_named_in_its_arguments(
        self, organization: Organization
    ) -> None:
        service = _RecordingService(RoomPushOutcome())

        push_room_to_provider_task(
            link_id=7,
            organization_id=organization.id,
            attempt_count=2,
            room_sync_service=service,  # type: ignore[arg-type]
        )

        assert service.calls == [(7, 2, organization)]
        assert get_current_organization() is None

    def test_missing_organization_does_nothing(self, db: Any) -> None:
        service = _RecordingService(RoomPushOutcome())

        push_room_to_provider_task(
            link_id=7,
            organization_id=999_999,
            attempt_count=0,
            room_sync_service=service,  # type: ignore[arg-type]
        )

        assert service.calls == []

    @freeze_time(NOW)
    def test_transient_failure_queues_the_next_attempt_with_backoff(
        self, organization: Organization
    ) -> None:
        service = _RecordingService(
            RoomPushOutcome(retry_attempt=3, retry_deadline=NOW + datetime.timedelta(hours=5))
        )

        with patch.object(push_room_to_provider_task, "apply_async") as apply_async:
            push_room_to_provider_task(
                link_id=7,
                organization_id=organization.id,
                attempt_count=2,
                room_sync_service=service,  # type: ignore[arg-type]
            )

        apply_async.assert_called_once_with(
            kwargs={"link_id": 7, "organization_id": organization.id, "attempt_count": 3},
            countdown=240,
        )

    def test_nothing_is_queued_when_the_push_is_done(self, organization: Organization) -> None:
        service = _RecordingService(RoomPushOutcome())

        with patch.object(push_room_to_provider_task, "apply_async") as apply_async:
            push_room_to_provider_task(
                link_id=7,
                organization_id=organization.id,
                attempt_count=0,
                room_sync_service=service,  # type: ignore[arg-type]
            )

        assert apply_async.call_count == 0

    def test_eager_mode_does_not_retry_inline(self, organization: Organization) -> None:
        # Eager Celery ignores countdowns, so a retry would run immediately, again
        # and again, until the deadline.
        service = _RecordingService(
            RoomPushOutcome(retry_attempt=1, retry_deadline=NOW + datetime.timedelta(hours=6))
        )

        with patch.object(push_room_to_provider_task, "apply_async") as apply_async:
            # `apply` is how Celery runs a task eagerly.
            push_room_to_provider_task.apply(
                kwargs={
                    "link_id": 7,
                    "organization_id": organization.id,
                    "attempt_count": 0,
                    "room_sync_service": service,
                }
            )

        assert apply_async.call_count == 0
        assert len(service.calls) == 1


class TestPushRetryCountdown:
    @pytest.mark.parametrize(
        ("attempt", "countdown"),
        [(1, 60), (2, 120), (3, 240), (4, 480), (5, 960), (6, 1800), (7, 1800), (500, 1800)],
    )
    def test_doubles_from_one_minute_up_to_thirty(self, attempt: int, countdown: int) -> None:
        assert push_retry_countdown(attempt) == countdown

    @freeze_time(NOW)
    def test_last_attempt_runs_at_the_deadline(self) -> None:
        deadline = NOW + datetime.timedelta(minutes=10)

        assert push_retry_countdown(7, deadline) == 600
        assert push_retry_countdown(1, deadline) == 60

    @freeze_time(NOW)
    def test_deadline_already_reached_waits_one_second(self) -> None:
        assert push_retry_countdown(7, NOW - datetime.timedelta(seconds=5)) == 1


def test_request_push_runs_the_task_through_the_container(
    organization: Organization,
    di_container: Any,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    link = _make_pending_room(organization)
    directory = FakeRoomDirectory()
    di_container.resource_directory_adapter_resolver.override(FakeRoomDirectoryResolver(directory))
    try:
        service = di_container.room_sync_service()
        with organization_context(organization), django_capture_on_commit_callbacks(execute=True):
            # Eager mode: the queued task runs on commit, through `Provide[...]`.
            service.request_push(link, ResourceSyncOperation.CREATE)
    finally:
        di_container.resource_directory_adapter_resolver.reset_override()

    assert directory.calls == ["create_room"]
    with organization_context(organization):
        link.refresh_from_db()
    assert link.sync_status == ResourceSyncStatus.SYNCED


@pytest.mark.django_db(transaction=True)
def test_two_pushes_of_one_link_serialize_on_the_row_lock() -> None:
    organization = Organization.objects.create(name="Room Push Lock Org")
    link = _make_pending_room(organization)
    directory = FakeRoomDirectory()
    service = RoomSyncService(
        resource_directory_adapter_resolver=FakeRoomDirectoryResolver(directory),
        room_sync_notifier=MagicMock(spec=RoomSyncNotifier),
    )
    first_in_provider = threading.Event()
    release_first = threading.Event()

    def hold_the_first_create(method: str) -> None:
        if not first_in_provider.is_set():
            first_in_provider.set()
            assert release_first.wait(timeout=10)

    directory.on_write = hold_the_first_create
    errors: list[BaseException] = []

    def run_push() -> None:
        try:
            push_room_to_provider_task(
                link_id=link.pk,
                organization_id=organization.id,
                attempt_count=0,
                room_sync_service=service,  # type: ignore[arg-type]
            )
        except BaseException as exc:  # noqa: BLE001 -- a thread error must fail the test
            errors.append(exc)
        finally:
            connection.close()

    first = threading.Thread(target=run_push)
    second = threading.Thread(target=run_push)
    first.start()
    assert first_in_provider.wait(timeout=10)
    second.start()

    # The second push must be waiting on the link's row lock, not running.
    assert _wait_for_a_lock_waiter() == 1
    assert directory.calls == ["create_room"]

    release_first.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert errors == []
    assert directory.calls == ["create_room"]
    assert len(directory.rooms) == 1
    with organization_context(organization):
        link.refresh_from_db()
    assert link.sync_status == ResourceSyncStatus.SYNCED


def _wait_for_a_lock_waiter() -> int:
    waiting = 0
    for _poll in range(100):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
            waiting = cursor.fetchone()[0]
        if waiting:
            break
        threading.Event().wait(0.05)
    return waiting


@pytest.mark.django_db(transaction=True)
def test_edit_made_while_a_push_is_in_flight_is_pushed_next() -> None:
    organization = Organization.objects.create(name="Room Edit Lock Org")
    link = _make_pending_room(organization)
    directory = FakeRoomDirectory()
    service = RoomSyncService(
        resource_directory_adapter_resolver=FakeRoomDirectoryResolver(directory),
        room_sync_notifier=MagicMock(spec=RoomSyncNotifier),
    )
    in_provider = threading.Event()
    release = threading.Event()

    def hold_the_update(method: str) -> None:
        if method == "update_room" and not in_provider.is_set():
            in_provider.set()
            assert release.wait(timeout=10)

    errors: list[BaseException] = []

    def in_thread(target: Callable[[], object]) -> threading.Thread:
        def run() -> None:
            try:
                with organization_context(organization):
                    target()
            except BaseException as exc:  # noqa: BLE001 -- a thread error must fail the test
                errors.append(exc)
            finally:
                connection.close()

        return threading.Thread(target=run)

    def edit() -> None:
        editor_copy = ResourceCalendarProviderLink.objects.get(pk=link.pk)
        service.request_push(editor_copy, ResourceSyncOperation.UPDATE, {"capacity": 12})

    # Pushes are run by hand here, so nothing queued may run on its own.
    with patch.object(push_room_to_provider_task, "delay"):
        with organization_context(organization):
            service.push(link.pk, 0)
            link.refresh_from_db()
            service.request_push(link, ResourceSyncOperation.UPDATE, {"capacity": 10})
        directory.on_write = hold_the_update

        pusher = in_thread(lambda: service.push(link.pk, 0))
        editor = in_thread(edit)
        pusher.start()
        assert in_provider.wait(timeout=10)
        editor.start()
        # The edit waits for the push to finish instead of writing under it.
        assert _wait_for_a_lock_waiter() == 1
        release.set()
        pusher.join(timeout=10)
        editor.join(timeout=10)

        assert errors == []
        with organization_context(organization):
            link.refresh_from_db()
            assert link.sync_status == ResourceSyncStatus.PENDING_UPDATE
            assert link.pending_fields == {"capacity": 12}
            assert link.provider_snapshot["capacity"] == 10

            service.push(link.pk, 0)

            link.refresh_from_db()

    assert directory.calls == ["create_room", "update_room", "update_room"]
    assert directory.rooms[f"vinta-{link.provisional_key}"].capacity == 12
    assert link.sync_status == ResourceSyncStatus.SYNCED
    assert link.pending_fields == {}
