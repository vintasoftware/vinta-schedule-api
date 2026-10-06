"""Integration tests for ``CalendarService.resolve_flagged_resource_bookings``.

A room the provider deleted is archived by the resync, and its future bookings are
flagged (``flagged_bookings_at``). The room is made through the real create, then put
in that state; the preview, validation and apply are the container's real
``BookingResolutionService``, and only the organizer emails are caught.

Event times are relative to the real clock rather than frozen, because the preview
fingerprint reads each event's ``modified`` timestamp.
"""

import datetime
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock, patch

from django.utils import timezone

import pytest

from calendar_integration.constants import (
    BookingCancelMode,
    BookingRejectionReason,
    CalendarProvider,
    FlaggedBookingsOutcome,
    RecurrenceFrequency,
    ResourceSyncStatus,
)
from calendar_integration.exceptions import (
    ResourceCalendarProviderSyncNotEnabledError,
    RoomSyncStateError,
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
    CancelBooking,
    MoveBooking,
)
from calendar_integration.tests.room_resync_fakes import make_room
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
    organization = Organization.objects.create(name="Flagged Bookings Org")
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
    user = UserFactory().create_user(email="room-resolver@example.com")
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


def _fingerprint(di_container: Any, room: Calendar) -> str:
    return di_container.booking_resolution_service().preview(room).fingerprint


def _future_bookings(di_container: Any, room: Calendar) -> tuple[int, ...]:
    preview = di_container.booking_resolution_service().preview(room)
    return tuple(booking.event_id for booking in preview.bookings)


@pytest.fixture
def flag(
    di_container: Any, organization: Organization, directory: FakeRoomDirectory
) -> Callable[[Calendar], None]:
    """Flag ``room`` the way the provider does: delete it there and run the real resync."""

    def _flag(room: Calendar) -> None:
        directory.rooms.pop(room.external_id)
        if not directory.rooms:
            # A listing with no rooms at all is never trusted to archive anything.
            other = make_room("other-room", "Other room", building=None)
            directory.rooms[other.external_id] = other
        di_container.room_resync_service().resync(organization, CalendarProvider.MICROSOFT)
        assert _link(room).sync_status == ResourceSyncStatus.ARCHIVED

    return _flag


@pytest.mark.usefixtures("bound")
class TestResolveFlaggedBookings:
    def test_move_resolves_every_booking_and_clears_the_flag(
        self,
        flag: Callable[[Calendar], None],
        service: CalendarService,
        di_container: Any,
        notifier: MagicMock,
        organizer_calendar: Calendar,
        room_a: Calendar,
        room_b: Calendar,
    ) -> None:
        m1 = _event(organizer_calendar, _wall_clock(1, 2), room_a, title="M1")
        m2 = _event(organizer_calendar, _wall_clock(2, 4), room_a, title="M2")
        flag(room_a)
        fingerprint = _fingerprint(di_container, room_a)

        result = service.resolve_flagged_resource_bookings(
            room_a.id, fingerprint, MoveBooking(room_b.id)
        )

        assert result.outcome == FlaggedBookingsOutcome.RESOLVED
        assert result.applied_event_ids == (m1.id, m2.id)
        assert _room_ids(m1) == {room_b.id}
        assert _room_ids(m2) == {room_b.id}
        assert _future_bookings(di_container, room_a) == ()
        assert _link(room_a).flagged_bookings_at is None
        # Still archived: resolving the bookings does not bring the room back.
        assert _link(room_a).sync_status == ResourceSyncStatus.ARCHIVED
        assert notifier.notify_booking_room_changed.call_count == 2

    def test_mixed_resolutions_with_an_override(
        self,
        flag: Callable[[Calendar], None],
        service: CalendarService,
        di_container: Any,
        organizer_calendar: Calendar,
        room_a: Calendar,
        room_b: Calendar,
    ) -> None:
        m1 = _event(organizer_calendar, _wall_clock(1, 2), room_a, title="M1")
        m2 = _event(organizer_calendar, _wall_clock(2, 4), room_a, title="M2")
        flag(room_a)

        result = service.resolve_flagged_resource_bookings(
            room_a.id,
            _fingerprint(di_container, room_a),
            MoveBooking(room_b.id),
            {m2.id: CancelBooking(BookingCancelMode.CANCEL_EVENT)},
        )

        assert result.outcome == FlaggedBookingsOutcome.RESOLVED
        assert _room_ids(m1) == {room_b.id}
        assert not CalendarEvent.objects.filter(id=m2.id).exists()
        assert _link(room_a).flagged_bookings_at is None

    def test_a_series_is_resolved_from_now_on(
        self,
        flag: Callable[[Calendar], None],
        service: CalendarService,
        di_container: Any,
        organizer_calendar: Calendar,
        room_a: Calendar,
    ) -> None:
        series = _event(organizer_calendar, _wall_clock(-15, 6), room_a, title="S", weekly=True)
        flag(room_a)

        result = service.resolve_flagged_resource_bookings(
            room_a.id,
            _fingerprint(di_container, room_a),
            CancelBooking(BookingCancelMode.REMOVE_ROOM),
        )

        assert result.outcome == FlaggedBookingsOutcome.RESOLVED
        assert _future_bookings(di_container, room_a) == ()
        # The past occurrences keep the room.
        series.refresh_from_db()
        assert _room_ids(series) == {room_a.id}
        assert _link(room_a).flagged_bookings_at is None

    def test_a_room_with_no_bookings_left_just_clears_the_flag(
        self,
        flag: Callable[[Calendar], None],
        service: CalendarService,
        di_container: Any,
        organizer_calendar: Calendar,
        room_a: Calendar,
    ) -> None:
        booking = _event(organizer_calendar, _wall_clock(1, 2), room_a, title="Gone")
        flag(room_a)
        # The booking went away on its own after the resync flagged the room.
        booking.delete()

        result = service.resolve_flagged_resource_bookings(
            room_a.id,
            _fingerprint(di_container, room_a),
            CancelBooking(BookingCancelMode.REMOVE_ROOM),
        )

        assert result.outcome == FlaggedBookingsOutcome.RESOLVED
        assert result.applied_event_ids == ()
        assert _link(room_a).flagged_bookings_at is None

    def test_a_partial_apply_keeps_the_flag_and_a_retry_finishes(
        self,
        flag: Callable[[Calendar], None],
        service: CalendarService,
        di_container: Any,
        organizer_calendar: Calendar,
        room_a: Calendar,
        room_b: Calendar,
    ) -> None:
        m1 = _event(organizer_calendar, _wall_clock(1, 2), room_a, title="M1")
        m2 = _event(organizer_calendar, _wall_clock(2, 4), room_a, title="M2")
        flag(room_a)
        fingerprint = _fingerprint(di_container, room_a)
        real_update_event = CalendarService.update_event

        def failing_on_m2(
            self: CalendarService, calendar_id: Any, event_id: int, *a: Any, **kw: Any
        ):
            if event_id == m2.id:
                raise RuntimeError("provider down")
            return real_update_event(self, calendar_id, event_id, *a, **kw)

        with patch.object(CalendarService, "update_event", failing_on_m2):
            result = service.resolve_flagged_resource_bookings(
                room_a.id, fingerprint, MoveBooking(room_b.id)
            )

        assert result.outcome == FlaggedBookingsOutcome.INCOMPLETE
        assert result.applied_event_ids == (m1.id,)
        assert result.pending_event_ids == (m2.id,)
        assert result.failed_at_event_id == m2.id
        assert _link(room_a).flagged_bookings_at is not None
        assert _room_ids(m2) == {room_a.id}

        retry = service.resolve_flagged_resource_bookings(
            room_a.id, _fingerprint(di_container, room_a), MoveBooking(room_b.id)
        )

        assert retry.outcome == FlaggedBookingsOutcome.RESOLVED
        assert _room_ids(m2) == {room_b.id}
        assert _link(room_a).flagged_bookings_at is None

    def test_an_invalid_resolution_rejects_everything_and_keeps_the_flag(
        self,
        flag: Callable[[Calendar], None],
        service: CalendarService,
        di_container: Any,
        organizer_calendar: Calendar,
        room_a: Calendar,
        room_b: Calendar,
    ) -> None:
        m1 = _event(organizer_calendar, _wall_clock(1, 2), room_a, title="M1")
        _event(organizer_calendar, _wall_clock(1, 2), room_b, title="Busy")
        flag(room_a)

        result = service.resolve_flagged_resource_bookings(
            room_a.id, _fingerprint(di_container, room_a), MoveBooking(room_b.id)
        )

        assert result.outcome == FlaggedBookingsOutcome.REJECTED
        assert [(r.event_id, r.reason) for r in result.rejected] == [
            (m1.id, BookingRejectionReason.TARGET_BUSY)
        ]
        assert _room_ids(m1) == {room_a.id}
        assert _link(room_a).flagged_bookings_at is not None

    def test_a_stale_fingerprint_is_refused(
        self,
        flag: Callable[[Calendar], None],
        service: CalendarService,
        di_container: Any,
        organizer_calendar: Calendar,
        room_a: Calendar,
        room_b: Calendar,
    ) -> None:
        _event(organizer_calendar, _wall_clock(1, 2), room_a, title="M1")
        flag(room_a)
        fingerprint = _fingerprint(di_container, room_a)
        _event(organizer_calendar, _wall_clock(3, 2), room_a, title="New")

        with pytest.raises(StaleBookingPreviewError):
            service.resolve_flagged_resource_bookings(
                room_a.id, fingerprint, MoveBooking(room_b.id)
            )

        assert _link(room_a).flagged_bookings_at is not None

    @pytest.mark.parametrize(
        "resolution_for",
        [
            lambda m1: (AbortDeletion(), {}),
            lambda m1: (CancelBooking(BookingCancelMode.REMOVE_ROOM), {m1.id: AbortDeletion()}),
        ],
        ids=["default", "override"],
    )
    def test_abort_is_not_accepted(
        self,
        flag: Callable[[Calendar], None],
        service: CalendarService,
        di_container: Any,
        organizer_calendar: Calendar,
        room_a: Calendar,
        resolution_for: Callable[[CalendarEvent], Any],
    ) -> None:
        m1 = _event(organizer_calendar, _wall_clock(1, 2), room_a, title="M1")
        flag(room_a)
        default, overrides = resolution_for(m1)

        with pytest.raises(ValueError, match="cannot be cancelled"):
            service.resolve_flagged_resource_bookings(
                room_a.id, _fingerprint(di_container, room_a), default, overrides
            )

        assert _room_ids(m1) == {room_a.id}
        assert _link(room_a).flagged_bookings_at is not None

    def test_a_room_that_is_not_flagged_is_rejected(
        self,
        service: CalendarService,
        di_container: Any,
        organizer_calendar: Calendar,
        room_a: Calendar,
        room_b: Calendar,
    ) -> None:
        m1 = _event(organizer_calendar, _wall_clock(1, 2), room_a, title="M1")

        with pytest.raises(RoomSyncStateError):
            service.resolve_flagged_resource_bookings(
                room_a.id, _fingerprint(di_container, room_a), MoveBooking(room_b.id)
            )

        assert _room_ids(m1) == {room_a.id}

    def test_an_archived_room_without_the_flag_is_rejected(
        self,
        flag: Callable[[Calendar], None],
        service: CalendarService,
        di_container: Any,
        room_a: Calendar,
        room_b: Calendar,
    ) -> None:
        flag(room_a)
        ResourceCalendarProviderLink.objects.filter(calendar=room_a).update(
            flagged_bookings_at=None
        )

        with pytest.raises(RoomSyncStateError):
            service.resolve_flagged_resource_bookings(
                room_a.id, _fingerprint(di_container, room_a), MoveBooking(room_b.id)
            )

    def test_a_manual_room_is_rejected(
        self, service: CalendarService, organization: Organization, room_b: Calendar
    ) -> None:
        manual = service.create_resource_calendar(name="Manual room")

        with pytest.raises(ValueError, match="not synced"):
            service.resolve_flagged_resource_bookings(manual.id, "x", MoveBooking(room_b.id))

    def test_flag_off_is_refused(
        self,
        flag: Callable[[Calendar], None],
        service: CalendarService,
        organization: Organization,
        room_a: Calendar,
        room_b: Calendar,
    ) -> None:
        flag(room_a)
        OrganizationFeatureFlag.objects.filter(
            organization=organization, key=RESOURCE_CALENDAR_PROVIDER_SYNC
        ).update(enabled=False)

        with pytest.raises(ResourceCalendarProviderSyncNotEnabledError):
            service.resolve_flagged_resource_bookings(room_a.id, "x", MoveBooking(room_b.id))

    def test_the_deletion_preview_lists_a_flagged_rooms_bookings(
        self,
        flag: Callable[[Calendar], None],
        service: CalendarService,
        organizer_calendar: Calendar,
        room_a: Calendar,
    ) -> None:
        m1 = _event(organizer_calendar, _wall_clock(1, 2), room_a, title="M1")
        flag(room_a)

        preview = service.preview_synced_resource_calendar_deletion(room_a.id)

        assert [booking.event_id for booking in preview.bookings] == [m1.id]
