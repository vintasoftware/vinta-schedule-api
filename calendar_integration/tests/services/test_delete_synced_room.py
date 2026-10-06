"""Integration tests for ``CalendarService.delete_synced_resource_calendar``.

Rooms are created through ``create_synced_resource_calendar`` and pushed to
``FakeRoomDirectory`` (a Microsoft room directory), installed by overriding the
container's ``resource_directory_adapter_resolver``. Bookings are events on an
organizer's internal calendar that allocate the room. The preview, validation and
apply are the container's real ``BookingResolutionService``; only the organizer
emails are caught, by overriding ``room_sync_notifier``.

Event times are relative to the real clock rather than frozen, because the preview
fingerprint reads each event's ``modified`` timestamp.
"""

import datetime
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock, patch

from django.utils import timezone

import pytest

from audit_integration.constants import AuditAction
from calendar_integration.constants import (
    BookingCancelMode,
    BookingRejectionReason,
    BookingRoomChange,
    CalendarProvider,
    CalendarType,
    CalendarVisibility,
    RecurrenceFrequency,
    ResourceSyncStatus,
    RoomDeletionOutcome,
)
from calendar_integration.exceptions import (
    ResourceCalendarProviderSyncNotEnabledError,
    StaleBookingPreviewError,
)
from calendar_integration.factories import create_resource_location
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarOwnership,
    RecurrenceRule,
    ResourceAllocation,
    ResourceCalendarProviderLink,
    ResourceLocation,
)
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    AbortDeletion,
    ApplyResult,
    CancelBooking,
    MoveBooking,
    RejectedBooking,
)
from calendar_integration.tests.room_sync_fakes import FakeRoomDirectory, FakeRoomDirectoryResolver
from common.feature_flags import RESOURCE_CALENDAR_PROVIDER_SYNC
from common.organization_context import organization_context
from organizations.models import Organization, OrganizationFeatureFlag, OrganizationMembership
from organizations.tests.helpers import make_admin_membership
from users.factories import UserFactory
from users.models import User


HOUR = datetime.timedelta(hours=1)


@pytest.fixture
def organization(db: Any) -> Organization:
    organization = Organization.objects.create(name="Room Delete Org")
    with organization_context(organization):
        OrganizationFeatureFlag.objects.create(
            organization=organization, key=RESOURCE_CALENDAR_PROVIDER_SYNC, enabled=True
        )
    return organization


@pytest.fixture
def bound(organization: Organization) -> Iterator[None]:
    with organization_context(organization):
        yield


@pytest.fixture
def admin(organization: Organization) -> User:
    user = UserFactory().create_user(email="room-deleter@example.com")
    make_admin_membership(user=user, organization=organization)
    return user


@pytest.fixture
def directory() -> FakeRoomDirectory:
    return FakeRoomDirectory(CalendarProvider.MICROSOFT)


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
def notifier(di_container: Any) -> Iterator[MagicMock]:
    notifier = MagicMock()
    di_container.room_sync_notifier.override(notifier)
    try:
        yield notifier
    finally:
        di_container.room_sync_notifier.reset_override()


@pytest.fixture
def service(
    di_container: Any, organization: Organization, admin: User, resolver: Any, notifier: Any
) -> CalendarService:
    service = di_container.calendar_service()
    service.initialize_without_provider(user_or_token=admin, organization=organization)
    return service


@pytest.fixture
def location(organization: Organization, bound: None) -> ResourceLocation:
    return create_resource_location(organization=organization, provider=CalendarProvider.MICROSOFT)


@pytest.fixture
def persist_audit() -> Iterator[MagicMock]:
    with patch("vinta_audit_logs.tasks.persist_audit_record") as persist_task:
        yield persist_task


def _room(
    service: CalendarService,
    location: ResourceLocation,
    capture: Callable[..., Any],
    name: str,
    *,
    push: bool = True,
) -> Calendar:
    """A Microsoft room made through the real create; synced when ``push`` runs the push."""
    with capture(execute=push):
        room = service.create_synced_resource_calendar(
            provider=CalendarProvider.MICROSOFT,
            location_id=location.id,
            name=name,
            capacity=10,
        )
    room.refresh_from_db()
    return room


@pytest.fixture
def room_a(
    service: CalendarService, location: ResourceLocation, django_capture_on_commit_callbacks
):
    return _room(service, location, django_capture_on_commit_callbacks, "Room A")


@pytest.fixture
def room_b(
    service: CalendarService, location: ResourceLocation, django_capture_on_commit_callbacks
):
    return _room(service, location, django_capture_on_commit_callbacks, "Room B")


@pytest.fixture
def organizer(organization: Organization) -> User:
    return UserFactory().create_user(email="organizer@example.com")


@pytest.fixture
def organizer_calendar(organization: Organization, organizer: User, bound: None) -> Calendar:
    calendar = Calendar.objects.create(
        organization=organization,
        name="Organizer",
        external_id="organizer",
        provider=CalendarProvider.INTERNAL,
    )
    OrganizationMembership.objects.create(user=organizer, organization=organization)
    CalendarOwnership.objects.create(
        calendar=calendar, membership_user_id=organizer.id, organization=organization
    )
    return calendar


def _wall_clock(days: int, hours: int = 0) -> datetime.datetime:
    """A naive UTC wall-clock ``days`` and ``hours`` from now, rounded down to the hour."""
    this_hour = timezone.now().replace(minute=0, second=0, microsecond=0, tzinfo=None)
    return this_hour + datetime.timedelta(days=days, hours=hours)


def _event(
    calendar: Calendar,
    start: datetime.datetime,
    *rooms: Calendar,
    title: str,
    weekly: bool = False,
) -> CalendarEvent:
    rule = (
        RecurrenceRule.objects.create(
            organization=calendar.organization, frequency=RecurrenceFrequency.WEEKLY, interval=1
        )
        if weekly
        else None
    )
    event = CalendarEvent.objects.create(
        organization=calendar.organization,
        calendar=calendar,
        title=title,
        start_time_tz_unaware=start,
        end_time_tz_unaware=start + HOUR,
        timezone="UTC",
        recurrence_rule=rule,
    )
    for room in rooms:
        ResourceAllocation.objects.create(
            organization=calendar.organization, event=event, calendar=room
        )
    event.refresh_from_db()
    return event


def _room_ids(event: CalendarEvent) -> set[int]:
    return set(
        ResourceAllocation.objects.filter(event=event).values_list("calendar_fk_id", flat=True)
    )


def _link(room: Calendar) -> ResourceCalendarProviderLink:
    return ResourceCalendarProviderLink.objects.get(calendar=room)


def _usage(organization: Organization) -> int:
    return (
        Calendar.objects.filter_by_organization(organization.id)
        .live_of_type(CalendarType.RESOURCE)
        .count()
    )


def _fingerprint(di_container: Any, room: Calendar) -> str:
    return di_container.booking_resolution_service().preview(room).fingerprint


def _future_bookings(di_container: Any, room: Calendar) -> tuple[int, ...]:
    """The event ids of the room's future bookings, as the deletion preview lists them."""
    preview = di_container.booking_resolution_service().preview(room)
    return tuple(booking.event_id for booking in preview.bookings)


@pytest.mark.usefixtures("bound")
class TestSpecScenarios:
    def test_scenario_4_mixed_resolutions_archive_the_room(
        self,
        service: CalendarService,
        di_container: Any,
        directory: FakeRoomDirectory,
        notifier: MagicMock,
        organization: Organization,
        organizer: User,
        organizer_calendar: Calendar,
        room_a: Calendar,
        room_b: Calendar,
        persist_audit: MagicMock,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        m1 = _event(organizer_calendar, _wall_clock(1, 2), room_a, title="M1")
        m2 = _event(organizer_calendar, _wall_clock(2, 4), room_a, title="M2")
        series = _event(organizer_calendar, _wall_clock(-15, 6), room_a, title="S", weekly=True)
        usage_before = _usage(organization)
        fingerprint = _fingerprint(di_container, room_a)

        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            result = service.delete_synced_resource_calendar(
                room_a.id,
                fingerprint,
                MoveBooking(room_b.id),
                {m2.id: CancelBooking(BookingCancelMode.CANCEL_EVENT)},
            )

            assert result.outcome == RoomDeletionOutcome.DELETED
            assert result.apply_result is not None
            assert result.apply_result.failed_at is None
            # Accepted: archived in Vinta Schedule, provider delete still queued.
            room_a.refresh_from_db()
            assert room_a.visibility == CalendarVisibility.INACTIVE
            link = _link(room_a)
            assert link.sync_status == ResourceSyncStatus.PENDING_DELETION
            assert link.archived_at is not None
            assert link.is_bookable is False
            assert _usage(organization) == usage_before - 1

        for callback in callbacks:
            callback()

        link.refresh_from_db()
        assert link.sync_status == ResourceSyncStatus.ARCHIVED
        assert directory.calls.count("delete_room") == 1
        assert room_a.external_id not in directory.rooms
        # No future booking references room A.
        assert _future_bookings(di_container, room_a) == ()
        assert _room_ids(m1) == {room_b.id}
        assert not CalendarEvent.objects.filter(id=m2.id).exists()
        # The series keeps room A for its past occurrences.
        series.refresh_from_db()
        assert _room_ids(series) == {room_a.id}
        notified = {call.args[2] for call in notifier.notify_booking_room_changed.call_args_list}
        assert notified == {BookingRoomChange.MOVED, BookingRoomChange.EVENT_CANCELLED}
        deletes = [
            call.args[0]
            for call in persist_audit.delay.call_args_list
            if call.args[0]["action_key"] == AuditAction.DELETE
            and call.args[0]["subject"]["subject_type"] == "calendar_integration.calendar"
            and call.args[0]["subject"]["subject_id"] == str(room_a.id)
        ]
        assert len(deletes) == 1

    def test_scenario_5_busy_target_rejects_and_changes_nothing(
        self,
        service: CalendarService,
        di_container: Any,
        directory: FakeRoomDirectory,
        organizer_calendar: Calendar,
        room_a: Calendar,
        room_b: Calendar,
    ) -> None:
        m1 = _event(organizer_calendar, _wall_clock(1, 2), room_a, title="M1")
        _event(organizer_calendar, _wall_clock(2, 4), room_a, title="M2")
        _event(organizer_calendar, _wall_clock(-15, 6), room_a, title="S", weekly=True)
        # Room B is already booked during M1.
        _event(organizer_calendar, _wall_clock(1, 2), room_b, title="Other")
        fingerprint = _fingerprint(di_container, room_a)

        result = service.delete_synced_resource_calendar(
            room_a.id, fingerprint, MoveBooking(room_b.id)
        )

        assert result.outcome == RoomDeletionOutcome.REJECTED
        assert result.rejected == (
            RejectedBooking(event_id=m1.id, reason=BookingRejectionReason.TARGET_BUSY),
        )
        room_a.refresh_from_db()
        assert room_a.visibility == CalendarVisibility.ACTIVE
        assert _link(room_a).sync_status == ResourceSyncStatus.SYNCED
        assert len(_future_bookings(di_container, room_a)) == 3
        assert "delete_room" not in directory.calls


@pytest.mark.usefixtures("bound")
class TestDeleteSyncedRoom:
    def test_stale_fingerprint_is_rejected(
        self,
        service: CalendarService,
        di_container: Any,
        organizer_calendar: Calendar,
        room_a: Calendar,
        room_b: Calendar,
    ) -> None:
        fingerprint = _fingerprint(di_container, room_a)
        # A booking arrives after the preview.
        _event(organizer_calendar, _wall_clock(1, 2), room_a, title="Late")

        with pytest.raises(StaleBookingPreviewError):
            service.delete_synced_resource_calendar(room_a.id, fingerprint, MoveBooking(room_b.id))

        assert _link(room_a).sync_status == ResourceSyncStatus.SYNCED

    def test_abort_with_a_booking_returns_the_bookings_and_changes_nothing(
        self,
        service: CalendarService,
        di_container: Any,
        organizer_calendar: Calendar,
        room_a: Calendar,
    ) -> None:
        m1 = _event(organizer_calendar, _wall_clock(1, 2), room_a, title="M1")
        fingerprint = _fingerprint(di_container, room_a)

        result = service.delete_synced_resource_calendar(room_a.id, fingerprint, AbortDeletion())

        assert result.outcome == RoomDeletionOutcome.ABORTED
        assert [booking.event_id for booking in result.bookings] == [m1.id]
        assert _link(room_a).sync_status == ResourceSyncStatus.SYNCED
        assert _room_ids(m1) == {room_a.id}

    def test_abort_with_no_bookings_deletes(
        self, service: CalendarService, di_container: Any, room_a: Calendar
    ) -> None:
        fingerprint = _fingerprint(di_container, room_a)

        result = service.delete_synced_resource_calendar(room_a.id, fingerprint, AbortDeletion())

        assert result.outcome == RoomDeletionOutcome.DELETED
        assert _link(room_a).sync_status == ResourceSyncStatus.PENDING_DELETION

    def test_pending_creation_room_is_archived_without_a_provider_call(
        self,
        service: CalendarService,
        di_container: Any,
        directory: FakeRoomDirectory,
        organization: Organization,
        location: ResourceLocation,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        room = _room(service, location, django_capture_on_commit_callbacks, "Room C", push=False)
        assert _link(room).sync_status == ResourceSyncStatus.PENDING_CREATION
        usage_before = _usage(organization)

        with django_capture_on_commit_callbacks(execute=True):
            result = service.delete_synced_resource_calendar(
                room.id, _fingerprint(di_container, room), AbortDeletion()
            )

        assert result.outcome == RoomDeletionOutcome.DELETED
        assert _link(room).sync_status == ResourceSyncStatus.ARCHIVED
        assert directory.calls == []
        assert _usage(organization) == usage_before - 1

    def test_delete_twice_is_a_no_op_success(
        self,
        service: CalendarService,
        di_container: Any,
        directory: FakeRoomDirectory,
        room_a: Calendar,
        room_b: Calendar,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        fingerprint = _fingerprint(di_container, room_a)
        with django_capture_on_commit_callbacks(execute=True):
            first = service.delete_synced_resource_calendar(
                room_a.id, fingerprint, MoveBooking(room_b.id)
            )
        with django_capture_on_commit_callbacks(execute=True) as second_callbacks:
            second = service.delete_synced_resource_calendar(
                room_a.id, "any fingerprint", MoveBooking(room_b.id)
            )

        assert first.deleted is True
        assert second.deleted is True
        assert second_callbacks == []
        assert _link(room_a).sync_status == ResourceSyncStatus.ARCHIVED
        assert directory.calls.count("delete_room") == 1

    def test_partial_apply_leaves_the_room_synced(
        self,
        service: CalendarService,
        di_container: Any,
        directory: FakeRoomDirectory,
        organizer_calendar: Calendar,
        room_a: Calendar,
        room_b: Calendar,
    ) -> None:
        m1 = _event(organizer_calendar, _wall_clock(1, 2), room_a, title="M1")
        m2 = _event(organizer_calendar, _wall_clock(2, 4), room_a, title="M2")
        fingerprint = _fingerprint(di_container, room_a)
        real_update_event = CalendarService.update_event

        def failing_on_m2(
            self: CalendarService, calendar_id: Any, event_id: int, *a: Any, **kw: Any
        ):
            if event_id == m2.id:
                raise RuntimeError("provider down")
            return real_update_event(self, calendar_id, event_id, *a, **kw)

        with patch.object(CalendarService, "update_event", failing_on_m2):
            result = service.delete_synced_resource_calendar(
                room_a.id, fingerprint, MoveBooking(room_b.id)
            )

        assert result.outcome == RoomDeletionOutcome.INCOMPLETE
        assert result.apply_result == ApplyResult(
            applied=(m1.id,), pending=(m2.id,), failed_at=m2.id
        )
        room_a.refresh_from_db()
        assert room_a.visibility == CalendarVisibility.ACTIVE
        assert _link(room_a).sync_status == ResourceSyncStatus.SYNCED
        assert _room_ids(m2) == {room_a.id}
        assert "delete_room" not in directory.calls

    def test_manual_room_is_rejected(self, service: CalendarService) -> None:
        room = service.create_resource_calendar(name="Manual room")

        with pytest.raises(ValueError, match="use disableResourceCalendar"):
            service.delete_synced_resource_calendar(room.id, "fp", AbortDeletion())


@pytest.mark.usefixtures("bound")
class TestFlagOff:
    def test_delete_is_rejected_and_disable_is_unchanged(
        self,
        service: CalendarService,
        organization: Organization,
        room_a: Calendar,
    ) -> None:
        OrganizationFeatureFlag.objects.filter_by_organization(organization.id).update(
            enabled=False
        )

        with pytest.raises(ResourceCalendarProviderSyncNotEnabledError):
            service.delete_synced_resource_calendar(room_a.id, "fp", AbortDeletion())

        assert _link(room_a).sync_status == ResourceSyncStatus.SYNCED
        disabled = service.disable_resource_calendar(room_a.id)
        assert disabled.visibility == CalendarVisibility.INACTIVE
