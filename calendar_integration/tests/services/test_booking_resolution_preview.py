"""Tests for ``BookingResolutionService``: the deletion preview, the busy check and the validator.

Event times are relative to the real clock rather than frozen, because the
fingerprint reads each event's ``modified`` timestamp, which a frozen clock would
stop from changing on an edit.
"""

import datetime
from collections.abc import Collection

from django.utils import timezone

import pytest
from model_bakery import baker

from calendar_integration.constants import (
    BookingCancelMode,
    BookingRejectionReason,
    CalendarProvider,
    CalendarType,
    CalendarVisibility,
    RecurrenceFrequency,
    ResourceSyncStatus,
    RSVPStatus,
)
from calendar_integration.exceptions import (
    ResourceDirectoryNotWriteEnabledError,
    StaleBookingPreviewError,
)
from calendar_integration.factories import create_resource_provider_link
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    EventExternalAttendance,
    ExternalAttendee,
    RecurrenceRule,
    ResourceAllocation,
)
from calendar_integration.services.booking_resolution_service import BookingResolutionService
from calendar_integration.services.dataclasses import (
    AbortDeletion,
    BookingResolutionPlan,
    BusyWindow,
    CancelBooking,
    MoveBooking,
    RejectedBooking,
    ResolvedBooking,
    ResourceLocationData,
    RoomBooking,
    RoomDirectoryData,
    RoomWriteData,
)
from calendar_integration.services.protocols.resource_directory_adapter import (
    ResourceDirectoryAdapter,
)
from organizations.models import Organization


HOUR = datetime.timedelta(hours=1)
WEEK = datetime.timedelta(weeks=1)


class FakeRoomDirectory:
    """A room directory whose free/busy answers come from ``busy``, keyed by room email."""

    provider: str = CalendarProvider.GOOGLE

    def __init__(self) -> None:
        self.busy: dict[str, list[BusyWindow]] = {}
        self.free_busy_calls: list[str] = []

    def list_locations(self) -> list[ResourceLocationData]:
        return []

    def list_rooms(self) -> list[RoomDirectoryData]:
        return []

    def create_room(self, data: RoomWriteData) -> RoomDirectoryData:
        raise NotImplementedError

    def update_room(
        self, external_id: str, data: RoomWriteData, fields: Collection[str]
    ) -> RoomDirectoryData:
        raise NotImplementedError

    def delete_room(self, external_id: str) -> None:
        raise NotImplementedError

    def get_free_busy(
        self, room_email: str, start: datetime.datetime, end: datetime.datetime
    ) -> list[BusyWindow]:
        self.free_busy_calls.append(room_email)
        return [w for w in self.busy.get(room_email, []) if w.start < end and w.end > start]


class FakeResolver:
    def __init__(self, adapter: ResourceDirectoryAdapter, write_enabled: bool = True) -> None:
        self.adapter = adapter
        self.write_enabled = write_enabled

    def adapter_for(self, organization: Organization, provider: str) -> ResourceDirectoryAdapter:
        if not self.is_write_enabled(organization, provider):
            raise ResourceDirectoryNotWriteEnabledError()
        return self.adapter

    def is_write_enabled(self, organization: Organization, provider: str) -> bool:
        return self.write_enabled and provider == self.adapter.provider


def _wall_clock(days: int, hours: int) -> datetime.datetime:
    """A naive UTC wall-clock ``days`` and ``hours`` from now, rounded down to the hour.

    Anchored on now rather than on midnight, so whether an occurrence today has
    already ended does not depend on when the test runs.
    """
    this_hour = timezone.now().replace(minute=0, second=0, microsecond=0, tzinfo=None)
    return this_hour + datetime.timedelta(days=days, hours=hours)


def _utc(naive: datetime.datetime) -> datetime.datetime:
    return naive.replace(tzinfo=datetime.UTC)


@pytest.fixture
def organization(db) -> Organization:
    return baker.make(Organization)


@pytest.fixture
def directory() -> FakeRoomDirectory:
    return FakeRoomDirectory()


@pytest.fixture
def service(directory: FakeRoomDirectory) -> BookingResolutionService:
    return BookingResolutionService(resource_directory_adapter_resolver=FakeResolver(directory))


@pytest.fixture
def organizer_calendar(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        organization=organization, name="Organizer", provider=CalendarProvider.INTERNAL
    )


def _room(
    organization: Organization,
    name: str,
    *,
    provider: str = CalendarProvider.GOOGLE,
    capacity: int | None = 10,
    sync_status: str | None = ResourceSyncStatus.SYNCED,
    **kwargs,
) -> Calendar:
    room = Calendar.objects.create(
        organization=organization,
        name=name,
        email=f"{name.lower().replace(' ', '-')}@resource.example.com",
        external_id=f"ext-{name}",
        provider=provider,
        calendar_type=CalendarType.RESOURCE,
        capacity=capacity,
        **kwargs,
    )
    if sync_status is not None:
        create_resource_provider_link(calendar=room, sync_status=sync_status)
    return room


@pytest.fixture
def room_a(organization: Organization) -> Calendar:
    return _room(organization, "Room A")


@pytest.fixture
def room_b(organization: Organization) -> Calendar:
    return _room(organization, "Room B")


def _event(
    calendar: Calendar,
    start: datetime.datetime,
    *,
    title: str = "Meeting",
    duration: datetime.timedelta = HOUR,
    rule: RecurrenceRule | None = None,
    parent: CalendarEvent | None = None,
) -> CalendarEvent:
    event = CalendarEvent.objects.create(
        organization=calendar.organization,
        calendar=calendar,
        title=title,
        start_time_tz_unaware=start,
        end_time_tz_unaware=start + duration,
        timezone="UTC",
        recurrence_rule=rule,
        parent_recurring_object=parent,
        is_recurring_exception=parent is not None,
    )
    event.refresh_from_db()
    return event


def _weekly(organization: Organization) -> RecurrenceRule:
    return RecurrenceRule.objects.create(
        organization=organization, frequency=RecurrenceFrequency.WEEKLY, interval=1
    )


def _allocate(
    event: CalendarEvent, room: Calendar, status: str = RSVPStatus.ACCEPTED
) -> ResourceAllocation:
    return ResourceAllocation.objects.create(
        organization=event.organization, event=event, calendar=room, status=status
    )


def _add_external_attendees(event: CalendarEvent, count: int) -> None:
    for index in range(count):
        attendee = ExternalAttendee.objects.create(
            organization=event.organization, email=f"guest{index}@example.com"
        )
        EventExternalAttendance.objects.create(
            organization=event.organization, event=event, external_attendee=attendee
        )


def _one_off(event: CalendarEvent) -> RoomBooking:
    return RoomBooking(
        event_id=event.id,
        calendar_id=event.calendar_fk_id,
        title=event.title,
        start=event.start_time,
        end=event.end_time,
        is_series=False,
        series_from=None,
    )


class TestPreview:
    def test_lists_future_bookings_from_the_room_calendar_and_from_allocations(
        self, service, organization, organizer_calendar, room_a, room_b
    ):
        on_room = _event(room_a, _wall_clock(2, 9), title="On the room")
        allocated = _event(organizer_calendar, _wall_clock(3, 10), title="Allocated")
        _allocate(allocated, room_a)
        past = _event(room_a, _wall_clock(-2, 9), title="Past")
        declined = _event(organizer_calendar, _wall_clock(4, 10), title="Declined")
        _allocate(declined, room_a, status=RSVPStatus.DECLINED)
        other_room = _event(room_b, _wall_clock(2, 9), title="Other room")

        preview = service.preview(room_a)

        assert preview.bookings == (_one_off(on_room), _one_off(allocated))
        listed = {booking.event_id for booking in preview.bookings}
        assert listed.isdisjoint({past.id, declined.id, other_room.id})

    def test_series_started_in_the_past_is_one_entry_from_now_on(
        self, service, organization, organizer_calendar, room_a
    ):
        # Today's occurrence ended two hours ago, so the next one is a week away.
        series = _event(
            organizer_calendar, _wall_clock(-14, -3), title="Weekly", rule=_weekly(organization)
        )
        _allocate(series, room_a)

        preview = service.preview(room_a)

        next_start = _utc(_wall_clock(7, -3))
        assert preview.bookings == (
            RoomBooking(
                event_id=series.id,
                calendar_id=organizer_calendar.id,
                title="Weekly",
                start=next_start,
                end=next_start + HOUR,
                is_series=True,
                series_from=next_start,
            ),
        )

    def test_series_starting_in_the_future_is_resolved_as_a_whole(
        self, service, organization, room_a
    ):
        series = _event(room_a, _wall_clock(5, 8), title="Weekly", rule=_weekly(organization))

        preview = service.preview(room_a)

        assert preview.bookings == (
            RoomBooking(
                event_id=series.id,
                calendar_id=room_a.id,
                title="Weekly",
                start=series.start_time,
                end=series.end_time,
                is_series=True,
                series_from=None,
            ),
        )

    def test_finished_series_is_not_listed(self, service, organization, room_a):
        rule = RecurrenceRule.objects.create(
            organization=organization, frequency=RecurrenceFrequency.WEEKLY, interval=1, count=2
        )
        _event(room_a, _wall_clock(-30, 8), rule=rule)

        assert service.preview(room_a).bookings == ()

    def test_exception_row_is_covered_by_its_series(self, service, organization, room_a):
        series = _event(room_a, _wall_clock(-14, -3), rule=_weekly(organization))
        _event(room_a, _wall_clock(14, -1), title="Moved occurrence", parent=series)

        preview = service.preview(room_a)

        assert [booking.event_id for booking in preview.bookings] == [series.id]

    def test_fingerprint_is_stable_and_changes_when_a_booking_is_added_or_edited(
        self, service, organizer_calendar, room_a
    ):
        meeting = _event(organizer_calendar, _wall_clock(2, 10))
        _allocate(meeting, room_a)
        first = service.preview(room_a).fingerprint
        assert service.preview(room_a).fingerprint == first

        _event(room_a, _wall_clock(3, 10))
        added = service.preview(room_a).fingerprint

        meeting.title = "Renamed"
        meeting.save()
        edited = service.preview(room_a).fingerprint

        assert len({first, added, edited}) == 3


class TestRoomBusyWindows:
    def test_merges_room_events_expanded_allocations_and_provider_free_busy(
        self, service, directory, organization, organizer_calendar, room_a
    ):
        on_room = _event(room_a, _wall_clock(1, 0))
        series = _event(organizer_calendar, _wall_clock(-7, 2), rule=_weekly(organization))
        _allocate(series, room_a)
        declined = _event(organizer_calendar, _wall_clock(1, 4))
        _allocate(declined, room_a, status=RSVPStatus.DECLINED)
        provider_window = BusyWindow(_utc(_wall_clock(2, 0)), _utc(_wall_clock(2, 1)))
        directory.busy[room_a.email] = [provider_window]

        windows = service.room_busy_windows(
            room_a, _utc(_wall_clock(0, 0)), _utc(_wall_clock(10, 0))
        )

        assert windows == [
            BusyWindow(_utc(_wall_clock(0, 2)), _utc(_wall_clock(0, 3))),
            BusyWindow(on_room.start_time, on_room.end_time),
            provider_window,
            BusyWindow(_utc(_wall_clock(7, 2)), _utc(_wall_clock(7, 3))),
        ]
        assert directory.free_busy_calls == [room_a.email]

    def test_room_without_a_bookable_link_skips_the_provider(
        self, service, directory, organization
    ):
        manual = _room(organization, "Manual", provider=CalendarProvider.INTERNAL, sync_status=None)
        pending = _room(organization, "Pending", sync_status=ResourceSyncStatus.PENDING_CREATION)

        service.room_busy_windows(manual, _utc(_wall_clock(0, 0)), _utc(_wall_clock(1, 0)))
        service.room_busy_windows(pending, _utc(_wall_clock(0, 0)), _utc(_wall_clock(1, 0)))

        assert directory.free_busy_calls == []

    def test_modified_occurrence_that_dropped_the_room_is_not_busy(
        self, service, organization, organizer_calendar, room_a
    ):
        series = _event(organizer_calendar, _wall_clock(-7, 2), rule=_weekly(organization))
        _allocate(series, room_a)
        # The occurrence a week from now was moved two hours later and no longer books
        # the room.
        moved = _event(organizer_calendar, _wall_clock(7, 4), parent=series)
        series.create_exception(_utc(_wall_clock(7, 2)), is_cancelled=False, modified_object=moved)

        windows = service.room_busy_windows(
            room_a, _utc(_wall_clock(6, 0)), _utc(_wall_clock(8, 0))
        )

        assert windows == []


@pytest.fixture
def scenario(organization, organizer_calendar, room_a, room_b):
    """Spec acceptance scenario 4: room A has M1, M2 and a weekly series S; B is free."""
    m1 = _event(organizer_calendar, _wall_clock(3, 10), title="M1")
    _allocate(m1, room_a)
    m2 = _event(organizer_calendar, _wall_clock(4, 10), title="M2")
    _allocate(m2, room_a)
    series = _event(organizer_calendar, _wall_clock(-14, -3), title="S", rule=_weekly(organization))
    _allocate(series, room_a)
    return {"m1": m1, "m2": m2, "series": series}


class TestValidate:
    def test_valid_plan_applies_overrides_over_the_default(self, service, room_a, room_b, scenario):
        preview = service.preview(room_a)
        cancel = CancelBooking(BookingCancelMode.CANCEL_EVENT)

        result = service.validate(
            room_a,
            preview.fingerprint,
            MoveBooking(room_b.id),
            {scenario["m2"].id: cancel},
        )

        by_id = {booking.event_id: booking for booking in preview.bookings}
        assert result == BookingResolutionPlan(
            room_id=room_a.id,
            fingerprint=preview.fingerprint,
            bookings=(
                ResolvedBooking(by_id[scenario["m1"].id], MoveBooking(room_b.id)),
                ResolvedBooking(by_id[scenario["m2"].id], cancel),
                ResolvedBooking(by_id[scenario["series"].id], MoveBooking(room_b.id)),
            ),
        )

    def test_spec_scenario_5_busy_target_rejects_naming_m1(
        self, service, organizer_calendar, room_a, room_b, scenario
    ):
        busy = _event(organizer_calendar, _wall_clock(3, 10), title="Already in B")
        _allocate(busy, room_b)
        preview = service.preview(room_a)

        result = service.validate(room_a, preview.fingerprint, MoveBooking(room_b.id), {})

        assert result == [
            RejectedBooking(event_id=scenario["m1"].id, reason=BookingRejectionReason.TARGET_BUSY)
        ]
        assert BookingRejectionReason.TARGET_BUSY.label == "target room is busy"

    def test_target_busy_by_an_allocation_only(self, service, organizer_calendar, room_a, room_b):
        booking = _event(room_a, _wall_clock(2, 10))
        other = _event(organizer_calendar, _wall_clock(2, 10))
        _allocate(other, room_b)
        preview = service.preview(room_a)

        result = service.validate(room_a, preview.fingerprint, MoveBooking(room_b.id), {})

        assert result == [RejectedBooking(booking.id, BookingRejectionReason.TARGET_BUSY)]

    def test_target_busy_on_the_provider(self, service, directory, room_a, room_b):
        booking = _event(room_a, _wall_clock(2, 10))
        directory.busy[room_b.email] = [
            BusyWindow(
                _utc(_wall_clock(2, 10)) + datetime.timedelta(minutes=30), _utc(_wall_clock(2, 12))
            )
        ]
        preview = service.preview(room_a)

        result = service.validate(room_a, preview.fingerprint, MoveBooking(room_b.id), {})

        assert result == [RejectedBooking(booking.id, BookingRejectionReason.TARGET_BUSY)]

    def test_series_rejected_when_a_later_occurrence_is_busy(
        self, service, directory, organization, room_a, room_b
    ):
        series = _event(room_a, _wall_clock(-14, -3), rule=_weekly(organization))
        directory.busy[room_b.email] = [
            BusyWindow(_utc(_wall_clock(21, -3)), _utc(_wall_clock(21, -2)))
        ]
        preview = service.preview(room_a)

        result = service.validate(room_a, preview.fingerprint, MoveBooking(room_b.id), {})

        assert result == [RejectedBooking(series.id, BookingRejectionReason.TARGET_BUSY)]

    def test_two_bookings_moved_into_the_same_slot_conflict(self, service, room_a, room_b):
        first = _event(room_a, _wall_clock(2, 10))
        second = _event(room_a, _wall_clock(2, 10))
        preview = service.preview(room_a)

        result = service.validate(room_a, preview.fingerprint, MoveBooking(room_b.id), {})

        assert first.id < second.id
        assert result == [RejectedBooking(second.id, BookingRejectionReason.TARGET_BUSY)]

    def test_event_already_booking_the_target_is_not_busy_checked(
        self, service, directory, organizer_calendar, room_a, room_b
    ):
        meeting = _event(organizer_calendar, _wall_clock(2, 10))
        _allocate(meeting, room_a)
        _allocate(meeting, room_b)
        directory.busy[room_b.email] = [BusyWindow(meeting.start_time, meeting.end_time)]
        preview = service.preview(room_a)

        result = service.validate(room_a, preview.fingerprint, MoveBooking(room_b.id), {})

        assert isinstance(result, BookingResolutionPlan)

    def test_target_too_small(self, service, organization, room_a):
        booking = _event(room_a, _wall_clock(2, 10))
        _add_external_attendees(booking, 3)
        small = _room(organization, "Small", capacity=2)
        preview = service.preview(room_a)

        result = service.validate(room_a, preview.fingerprint, MoveBooking(small.id), {})

        assert result == [RejectedBooking(booking.id, BookingRejectionReason.TARGET_TOO_SMALL)]

    @pytest.mark.parametrize(
        ("target_kwargs", "reason"),
        [
            (
                {"sync_status": ResourceSyncStatus.PENDING_CREATION},
                BookingRejectionReason.TARGET_NOT_BOOKABLE,
            ),
            (
                {"sync_status": ResourceSyncStatus.ARCHIVED},
                BookingRejectionReason.TARGET_NOT_BOOKABLE,
            ),
            (
                {"sync_status": None, "visibility": CalendarVisibility.INACTIVE},
                BookingRejectionReason.TARGET_NOT_BOOKABLE,
            ),
            (
                {"provider": CalendarProvider.MICROSOFT},
                BookingRejectionReason.TARGET_ON_DIFFERENT_PROVIDER,
            ),
            (
                {"provider": CalendarProvider.INTERNAL, "sync_status": None},
                BookingRejectionReason.TARGET_ON_DIFFERENT_PROVIDER,
            ),
        ],
    )
    def test_invalid_target(self, service, organization, room_a, target_kwargs, reason):
        booking = _event(room_a, _wall_clock(2, 10))
        target = _room(organization, "Target", **target_kwargs)
        preview = service.preview(room_a)

        result = service.validate(room_a, preview.fingerprint, MoveBooking(target.id), {})

        assert result == [RejectedBooking(booking.id, reason)]

    def test_target_is_the_room_itself(self, service, room_a):
        booking = _event(room_a, _wall_clock(2, 10))
        preview = service.preview(room_a)

        result = service.validate(room_a, preview.fingerprint, MoveBooking(room_a.id), {})

        assert result == [RejectedBooking(booking.id, BookingRejectionReason.TARGET_IS_SAME_ROOM)]

    def test_target_that_is_not_a_room_is_not_found(self, service, organizer_calendar, room_a):
        booking = _event(room_a, _wall_clock(2, 10))
        preview = service.preview(room_a)

        result = service.validate(
            room_a, preview.fingerprint, MoveBooking(organizer_calendar.id), {}
        )

        assert result == [RejectedBooking(booking.id, BookingRejectionReason.TARGET_NOT_FOUND)]

    def test_override_for_an_event_not_in_the_preview(self, service, room_b, room_a):
        _event(room_a, _wall_clock(2, 10))
        stranger = _event(room_b, _wall_clock(2, 10))
        preview = service.preview(room_a)

        result = service.validate(
            room_a, preview.fingerprint, AbortDeletion(), {stranger.id: AbortDeletion()}
        )

        assert result == [RejectedBooking(stranger.id, BookingRejectionReason.NOT_IN_PREVIEW)]

    def test_stale_fingerprint_is_rejected(self, service, room_a):
        _event(room_a, _wall_clock(2, 10))
        fingerprint = service.preview(room_a).fingerprint
        _event(room_a, _wall_clock(3, 10))

        with pytest.raises(StaleBookingPreviewError):
            service.validate(room_a, fingerprint, AbortDeletion(), {})

    def test_provider_error_on_the_target_is_raised(self, organization, room_a, room_b):
        _event(room_a, _wall_clock(2, 10))
        service = BookingResolutionService(
            resource_directory_adapter_resolver=FakeResolver(
                FakeRoomDirectory(), write_enabled=False
            )
        )
        preview = service.preview(room_a)

        with pytest.raises(ResourceDirectoryNotWriteEnabledError):
            service.validate(room_a, preview.fingerprint, MoveBooking(room_b.id), {})
