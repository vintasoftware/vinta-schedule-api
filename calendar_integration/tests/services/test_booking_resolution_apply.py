"""Tests for ``BookingResolutionService.apply``: applying a validated room deletion plan.

Plans are built through the real route (``preview`` then ``validate``) and applied
through the container's ``CalendarService``, which ``apply`` binds as the room
resolution actor. Organizer calendars are internal, so no provider is called for the
events. The room directory is a fake that reports every room free.

The bulk-modification tests call the facade directly, as a public-API token scoped
to the organizer calendar's owner, which may create events on that calendar.

Event times are relative to the real clock rather than frozen, because the preview
fingerprint reads each event's ``modified`` timestamp.
"""

import datetime
from collections.abc import Collection
from unittest.mock import MagicMock, call

from django.core.exceptions import PermissionDenied
from django.utils import timezone

import pytest

from calendar_integration.constants import (
    BookingCancelMode,
    BookingRoomChange,
    CalendarProvider,
    CalendarType,
    RecurrenceFrequency,
    ResourceSyncStatus,
    RSVPStatus,
)
from calendar_integration.factories import create_resource_provider_link
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarOwnership,
    RecurrenceRule,
    ResourceAllocation,
    ResourceCalendarProviderLink,
)
from calendar_integration.services.booking_resolution_service import BookingResolutionService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    AbortDeletion,
    ApplyResult,
    BookingResolution,
    BookingResolutionPlan,
    BusyWindow,
    CancelBooking,
    MoveBooking,
    ResourceAllocationInputData,
    ResourceLocationData,
    RoomDirectoryData,
    RoomWriteData,
)
from calendar_integration.services.protocols.booking_room_change_notifier import (
    BookingRoomChangeNotifier,
)
from calendar_integration.services.protocols.resource_directory_adapter import (
    ResourceDirectoryAdapter,
)
from organizations.models import Organization, OrganizationMembership
from public_api.services import PublicAPIAuthService
from users.models import Profile, User


HOUR = datetime.timedelta(hours=1)
WEEK = datetime.timedelta(weeks=1)
# Far enough to hold every future occurrence the tests create.
HORIZON = datetime.timedelta(days=400)


class FreeRoomDirectory:
    """A Google room directory in which every room is free."""

    provider: str = CalendarProvider.GOOGLE

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
        return []


class FreeRoomResolver:
    def adapter_for(self, organization: Organization, provider: str) -> ResourceDirectoryAdapter:
        return FreeRoomDirectory()

    def is_write_enabled(self, organization: Organization, provider: str) -> bool:
        return True


@pytest.fixture
def organization(db) -> Organization:
    return Organization.objects.create(name="Booking Apply Org")


@pytest.fixture
def notifier() -> MagicMock:
    return MagicMock(spec=BookingRoomChangeNotifier)


@pytest.fixture
def service(notifier: MagicMock, di_container) -> BookingResolutionService:
    return BookingResolutionService(
        resource_directory_adapter_resolver=FreeRoomResolver(),
        booking_room_change_notifier=notifier,
        calendar_service=di_container.calendar_service(),
    )


@pytest.fixture
def organizer(organization: Organization) -> User:
    user = User.objects.create_user(email="organizer@example.com", password="pw")
    Profile.objects.create(user=user)
    return user


def _organizer_calendar(organization: Organization, owner: User, name: str) -> Calendar:
    calendar = Calendar.objects.create(
        organization=organization,
        name=name,
        external_id=f"organizer-{name}",
        provider=CalendarProvider.INTERNAL,
    )
    OrganizationMembership.objects.create(user=owner, organization=organization)
    CalendarOwnership.objects.create(
        calendar=calendar, membership_user_id=owner.id, organization=organization
    )
    return calendar


@pytest.fixture
def organizer_calendar(organization: Organization, organizer: User) -> Calendar:
    return _organizer_calendar(organization, organizer, "Organizer")


@pytest.fixture
def facade(organization: Organization, organizer: User, organizer_calendar, di_container):
    membership = OrganizationMembership.objects.get(user=organizer, organization=organization)
    system_user, _token = PublicAPIAuthService().create_system_user(
        integration_name="booking_resolution_apply",
        organization=organization,
        scoped_to_membership=membership,
    )
    facade = di_container.calendar_service()
    facade.initialize_without_provider(user_or_token=system_user, organization=organization)
    return facade


def _room(organization: Organization, name: str) -> Calendar:
    room = Calendar.objects.create(
        organization=organization,
        name=name,
        email=f"{name.lower().replace(' ', '-')}@resource.example.com",
        external_id=f"ext-{name}",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.RESOURCE,
        capacity=10,
    )
    create_resource_provider_link(calendar=room, sync_status=ResourceSyncStatus.SYNCED)
    return room


@pytest.fixture
def room_a(organization: Organization) -> Calendar:
    return _room(organization, "Room A")


@pytest.fixture
def room_b(organization: Organization) -> Calendar:
    return _room(organization, "Room B")


def _wall_clock(days: int, hours: int = 0) -> datetime.datetime:
    """A naive UTC wall-clock ``days`` and ``hours`` from now, rounded down to the hour."""
    this_hour = timezone.now().replace(minute=0, second=0, microsecond=0, tzinfo=None)
    return this_hour + datetime.timedelta(days=days, hours=hours)


def _event(
    calendar: Calendar,
    start: datetime.datetime,
    *rooms: Calendar,
    title: str = "Meeting",
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
        ResourceAllocation.objects.filter_by_organization(event.organization_id)
        .filter(event=event)
        .values_list("calendar_fk_id", flat=True)
    )


def _plan(
    service: BookingResolutionService,
    room: Calendar,
    default: BookingResolution,
    overrides: dict[int, BookingResolution] | None = None,
) -> BookingResolutionPlan:
    preview = service.preview(room)
    plan = service.validate(room, preview.fingerprint, default, overrides or {})
    assert isinstance(plan, BookingResolutionPlan), plan
    return plan


def _plan_ids(plan: BookingResolutionPlan) -> tuple[int, ...]:
    return tuple(entry.booking.event_id for entry in plan.bookings)


def _series_booking(room: Calendar) -> CalendarEvent | None:
    return (
        CalendarEvent.objects.filter_by_organization(room.organization_id)
        .booking_room(room.id)
        .exclude(recurrence_rule__isnull=True)
        .first()
    )


@pytest.mark.django_db
class TestApply:
    def test_spec_scenario_4_moves_series_from_now_on_and_cancels_m2(
        self,
        service: BookingResolutionService,
        notifier: MagicMock,
        organizer: User,
        organizer_calendar: Calendar,
        room_a: Calendar,
        room_b: Calendar,
    ):
        # M2 is on another organizer's calendar: the apply edits everyone's events.
        second_organizer = User.objects.create_user(email="second@example.com", password="pw")
        second_calendar = _organizer_calendar(room_a.organization, second_organizer, "Second")
        m1 = _event(organizer_calendar, _wall_clock(1, 2), room_a, title="M1")
        m2 = _event(second_calendar, _wall_clock(2, 4), room_a, title="M2")
        series = _event(organizer_calendar, _wall_clock(-15, 6), room_a, title="S", weekly=True)
        plan = _plan(
            service,
            room_a,
            MoveBooking(room_b.id),
            {m2.id: CancelBooking(BookingCancelMode.CANCEL_EVENT)},
        )

        result = service.apply(plan)

        assert result == ApplyResult(applied=_plan_ids(plan), pending=(), failed_at=None)
        assert _room_ids(m1) == {room_b.id}
        assert (
            not CalendarEvent.objects.filter_by_organization(m2.organization_id)
            .filter(id=m2.id)
            .exists()
        )
        # The series keeps room A for its past occurrences and has none from now on.
        series.refresh_from_db()
        now = timezone.now()
        assert _room_ids(series) == {room_a.id}
        assert series.get_occurrences_in_range(now - 3 * WEEK, now, overlap=True)
        assert series.get_occurrences_in_range(now, now + HORIZON, overlap=True) == []
        # Its continuation books room B from now on.
        continuation = _series_booking(room_b)
        assert continuation is not None
        assert continuation.title == "S"
        assert _room_ids(continuation) == {room_b.id}
        assert continuation.start_time > now - HOUR
        # Nothing from now on references room A.
        assert service.preview(room_a).bookings == ()
        expected = {
            m1.id: call(m1.id, organizer.id, BookingRoomChange.MOVED),
            m2.id: call(m2.id, second_organizer.id, BookingRoomChange.EVENT_CANCELLED),
            series.id: call(continuation.id, organizer.id, BookingRoomChange.MOVED),
        }
        assert notifier.notify_booking_room_changed.call_args_list == [
            expected[event_id] for event_id in _plan_ids(plan)
        ]

    def test_series_that_has_not_started_is_moved_as_a_whole(
        self,
        service: BookingResolutionService,
        organizer_calendar: Calendar,
        room_a: Calendar,
        room_b: Calendar,
    ):
        series = _event(organizer_calendar, _wall_clock(3), room_a, weekly=True)
        rule = series.recurrence_rule
        assert rule is not None

        service.apply(_plan(service, room_a, MoveBooking(room_b.id)))

        series.refresh_from_db()
        assert _room_ids(series) == {room_b.id}
        assert series.recurrence_rule is not None
        assert series.recurrence_rule.to_rrule_string() == rule.to_rrule_string()
        assert series.start_time == _utc(_wall_clock(3))

    def test_series_cancelled_from_now_on_keeps_its_past(
        self,
        service: BookingResolutionService,
        organizer_calendar: Calendar,
        room_a: Calendar,
    ):
        series = _event(organizer_calendar, _wall_clock(-15, 6), room_a, weekly=True)

        service.apply(_plan(service, room_a, CancelBooking(BookingCancelMode.CANCEL_EVENT)))

        series.refresh_from_db()
        now = timezone.now()
        assert _room_ids(series) == {room_a.id}
        assert series.get_occurrences_in_range(now - 3 * WEEK, now, overlap=True)
        assert series.get_occurrences_in_range(now, now + HORIZON, overlap=True) == []
        assert service.preview(room_a).bookings == ()

    def test_remove_room_keeps_the_other_rooms_and_other_events_allocations(
        self,
        service: BookingResolutionService,
        notifier: MagicMock,
        organizer: User,
        organization: Organization,
        organizer_calendar: Calendar,
        room_a: Calendar,
        room_b: Calendar,
    ):
        meeting = _event(organizer_calendar, _wall_clock(1), room_a, room_b)
        # Another event's allocation on room A, declined, so it is not a booking of A.
        other = _event(organizer_calendar, _wall_clock(2))
        declined = ResourceAllocation.objects.create(
            organization=organization, event=other, calendar=room_a, status=RSVPStatus.DECLINED
        )

        result = service.apply(_plan(service, room_a, CancelBooking(BookingCancelMode.REMOVE_ROOM)))

        assert result == ApplyResult(applied=(meeting.id,), pending=(), failed_at=None)
        assert _room_ids(meeting) == {room_b.id}
        assert (
            ResourceAllocation.objects.filter_by_organization(organization.id)
            .filter(id=declined.id)
            .exists()
        )
        notifier.notify_booking_room_changed.assert_called_once_with(
            meeting.id, organizer.id, BookingRoomChange.ROOM_REMOVED
        )

    def test_failure_stops_the_apply_and_a_rerun_applies_the_rest_only(
        self,
        service: BookingResolutionService,
        notifier: MagicMock,
        organization: Organization,
        organizer_calendar: Calendar,
        room_a: Calendar,
        room_b: Calendar,
    ):
        room_c = _room(organization, "Room C")
        first = _event(organizer_calendar, _wall_clock(1), room_a, title="First")
        second = _event(organizer_calendar, _wall_clock(2), room_a, title="Second")
        third = _event(organizer_calendar, _wall_clock(3), room_a, title="Third")
        plan = _plan(service, room_a, MoveBooking(room_b.id), {first.id: MoveBooking(room_c.id)})
        assert _plan_ids(plan) == (first.id, second.id, third.id)
        # Room B stops being bookable after the plan was validated.
        link_b = ResourceCalendarProviderLink.objects.filter_by_organization(organization.id).get(
            calendar=room_b
        )
        link_b.sync_status = ResourceSyncStatus.PENDING_DELETION
        link_b.save()

        result = service.apply(plan)

        assert result == ApplyResult(
            applied=(first.id,), pending=(second.id, third.id), failed_at=second.id
        )
        assert (_room_ids(first), _room_ids(second), _room_ids(third)) == (
            {room_c.id},
            {room_a.id},
            {room_a.id},
        )
        assert notifier.notify_booking_room_changed.call_count == 1

        link_b.sync_status = ResourceSyncStatus.SYNCED
        link_b.save()
        notifier.reset_mock()

        rerun = service.apply(plan)

        assert rerun == ApplyResult(
            applied=(first.id, second.id, third.id), pending=(), failed_at=None
        )
        assert (_room_ids(first), _room_ids(second), _room_ids(third)) == (
            {room_c.id},
            {room_b.id},
            {room_b.id},
        )
        assert [c.args[0] for c in notifier.notify_booking_room_changed.call_args_list] == [
            second.id,
            third.id,
        ]

    @pytest.mark.parametrize(
        "resolution",
        [
            MoveBooking(0),
            CancelBooking(BookingCancelMode.REMOVE_ROOM),
            CancelBooking(BookingCancelMode.CANCEL_EVENT),
        ],
        ids=["move", "remove_room", "cancel_event"],
    )
    def test_room_calendar_copy_is_deleted_whatever_the_resolution(
        self,
        service: BookingResolutionService,
        notifier: MagicMock,
        organizer_calendar: Calendar,
        room_a: Calendar,
        room_b: Calendar,
        resolution: BookingResolution,
    ):
        if isinstance(resolution, MoveBooking):
            resolution = MoveBooking(room_b.id)
        meeting = _event(organizer_calendar, _wall_clock(1), room_a, title="Meeting")
        # The room's copy of the same booking, as the room calendar's sync stores it.
        copy = _event(room_a, _wall_clock(1), title="Meeting")
        plan = _plan(service, room_a, resolution)
        assert set(_plan_ids(plan)) == {meeting.id, copy.id}

        result = service.apply(plan)

        assert result == ApplyResult(applied=_plan_ids(plan), pending=(), failed_at=None)
        assert (
            not CalendarEvent.objects.filter_by_organization(copy.organization_id)
            .filter(id=copy.id)
            .exists()
        )
        assert service.preview(room_a).bookings == ()
        # Only the organizer's own event is notified.
        assert [c.args[0] for c in notifier.notify_booking_room_changed.call_args_list] == [
            meeting.id
        ]

    def test_plan_that_cancels_the_deletion_is_refused(
        self,
        service: BookingResolutionService,
        organizer_calendar: Calendar,
        room_a: Calendar,
    ):
        meeting = _event(organizer_calendar, _wall_clock(1), room_a)

        with pytest.raises(ValueError, match="cancels the room deletion"):
            service.apply(_plan(service, room_a, AbortDeletion()))

        assert _room_ids(meeting) == {room_a.id}

    def test_apply_leaves_other_facades_under_the_normal_permission_checks(
        self,
        service: BookingResolutionService,
        organization: Organization,
        organizer_calendar: Calendar,
        room_a: Calendar,
        room_b: Calendar,
        di_container,
    ):
        meeting = _event(organizer_calendar, _wall_clock(1), room_a)
        service.apply(_plan(service, room_a, MoveBooking(room_b.id)))
        other = di_container.calendar_service()
        other.initialize_without_provider(organization=organization)

        with pytest.raises(PermissionDenied):
            other.delete_event(organizer_calendar.id, meeting.id)


@pytest.mark.django_db
class TestBulkModificationRooms:
    def test_continuation_copies_the_series_rooms_by_default(
        self, facade: CalendarService, organizer_calendar: Calendar, room_a: Calendar, room_b
    ):
        series = _event(organizer_calendar, _wall_clock(-15, 6), room_a, room_b, weekly=True)

        continuation = facade.modify_recurring_event_from_date(
            parent_event=series,
            modification_start_date=_utc(_wall_clock(6, 6)),
            modified_title="Renamed",
        )

        assert continuation is not None
        assert _room_ids(continuation) == {room_a.id, room_b.id}
        assert _room_ids(series) == {room_a.id, room_b.id}

    def test_override_replaces_the_continuation_rooms_only(
        self, facade: CalendarService, organizer_calendar: Calendar, room_a: Calendar, room_b
    ):
        series = _event(organizer_calendar, _wall_clock(-15, 6), room_a, weekly=True)

        continuation = facade.modify_recurring_event_from_date(
            parent_event=series,
            modification_start_date=_utc(_wall_clock(6, 6)),
            resource_allocations_override=[ResourceAllocationInputData(resource_id=room_b.id)],
        )

        assert continuation is not None
        assert _room_ids(continuation) == {room_b.id}
        assert _room_ids(series) == {room_a.id}


def _utc(naive: datetime.datetime) -> datetime.datetime:
    return naive.replace(tzinfo=datetime.UTC)
