"""Preview, validate and apply how a room's future bookings are resolved before the room is deleted.

Every query names the room's own organization (``filter_by_organization``), so the
service gives the same answer from a request, a Celery task or a management command,
whether or not an organization is bound.
"""

import datetime
import hashlib
import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from calendar_integration.constants import (
    BookingCancelMode,
    BookingRejectionReason,
    BookingRoomChange,
    CalendarType,
    CalendarVisibility,
)
from calendar_integration.exceptions import StaleBookingPreviewError
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarOwnership,
    ResourceCalendarProviderLink,
)
from calendar_integration.services.dataclasses import (
    AbortDeletion,
    ApplyResult,
    BookingResolution,
    BookingResolutionPlan,
    BusyWindow,
    CalendarEventInputData,
    CancelBooking,
    MoveBooking,
    RejectedBooking,
    ResolvedBooking,
    ResourceAllocationInputData,
    RoomBooking,
    RoomBookingPreview,
)
from calendar_integration.services.protocols.booking_room_change_notifier import (
    BookingRoomChangeNotifier,
)
from calendar_integration.services.protocols.resource_directory_adapter import (
    ResourceDirectoryAdapterResolver,
)


if TYPE_CHECKING:
    from calendar_integration.services.calendar_service import CalendarService


logger = logging.getLogger(__name__)


# How far ahead a recurring series is searched for its next occurrence. Matches
# ``RecurringMixin.get_next_occurrence``.
PREVIEW_SEARCH_WINDOW = datetime.timedelta(days=10 * 365)
# Occurrences read per series while looking for the next one. More than one, because
# cancelled occurrences are dropped after the expansion is cut.
PREVIEW_MAX_OCCURRENCES = 50
# How far from its first affected occurrence a series is checked for a busy move
# target. An open-ended series has no last occurrence, so the check has to stop
# somewhere; occurrences after this are not checked.
SERIES_BUSY_CHECK_HORIZON = datetime.timedelta(days=60)


def _overlaps(window: BusyWindow, start: datetime.datetime, end: datetime.datetime) -> bool:
    return window.start < end and window.end > start


class BookingResolutionService:
    """Lists a room's future bookings, validates the plan for resolving them, and applies it."""

    def __init__(
        self,
        resource_directory_adapter_resolver: ResourceDirectoryAdapterResolver,
        booking_room_change_notifier: BookingRoomChangeNotifier,
        calendar_service: "CalendarService",
    ):
        self.resource_directory_adapter_resolver = resource_directory_adapter_resolver
        self.booking_room_change_notifier = booking_room_change_notifier
        # Only ``apply`` uses it, after binding it with ``initialize_for_room_resolution``.
        self.calendar_service = calendar_service

    def preview(self, room: Calendar) -> RoomBookingPreview:
        """The room's future bookings, and a fingerprint of them.

        A booking is an event that books the room (``CalendarEventQuerySet.booking_room``)
        and has an occurrence that has not ended yet. A recurring series is one entry:
        "from now on" when it started before now, as a whole otherwise. An exception
        row of a series is its own entry only when its series is not one, since
        resolving the series already covers it.

        The fingerprint is a sha256 over the sorted ``(event id, modified, recurrence
        rule)`` of the entries, so adding a booking, removing one, or editing one
        changes it.
        """
        now = timezone.now()
        rows = list(
            CalendarEvent.objects.filter_by_organization(room.organization_id)
            .booking_room(room.id)
            .with_occurrences_overlapping(
                now, now + PREVIEW_SEARCH_WINDOW, max_occurrences=PREVIEW_MAX_OCCURRENCES
            )
        )

        entries: list[tuple[CalendarEvent, RoomBooking]] = []
        series_ids: set[int] = set()
        exception_rows: list[CalendarEvent] = []
        for row in rows:
            if row.recurrence_rule is None:
                if row.parent_recurring_object_fk_id is None:
                    entries.append((row, self._one_off_booking(row)))
                else:
                    exception_rows.append(row)
                continue
            occurrences = row.get_occurrences_in_range(
                now,
                now + PREVIEW_SEARCH_WINDOW,
                max_occurrences=PREVIEW_MAX_OCCURRENCES,
                overlap=True,
            )
            if not occurrences:
                continue
            first = occurrences[0]
            entries.append(
                (
                    row,
                    RoomBooking(
                        event_id=row.id,
                        calendar_id=row.calendar_fk_id,
                        title=row.title,
                        start=first.start_time,
                        end=first.end_time,
                        is_series=True,
                        series_from=first.start_time if row.start_time < now else None,
                    ),
                )
            )
            series_ids.add(row.id)
        entries.extend(
            (row, self._one_off_booking(row))
            for row in exception_rows
            if row.parent_recurring_object_fk_id not in series_ids
        )

        entries.sort(key=lambda entry: (entry[1].start, entry[1].event_id))
        return RoomBookingPreview(
            fingerprint=self._fingerprint(row for row, _ in entries),
            bookings=tuple(booking for _, booking in entries),
        )

    def room_busy_windows(
        self, room: Calendar, start: datetime.datetime, end: datetime.datetime
    ) -> list[BusyWindow]:
        """The windows in which ``room`` is busy between ``start`` and ``end``, by start.

        Three sources, any of which makes the room busy:

        - events on the room's own calendar;
        - events on any calendar that allocate the room (a declined allocation does
          not count), with recurring series expanded. Every occurrence of a series
          that books the room counts, a modified one included: an occurrence edit
          keeps the series' rooms, and its exception row carries no allocations of
          its own;
        - the provider's free/busy, for a room whose provider link says it exists on
          the provider (``is_bookable``). That covers bookings Vinta Schedule has not
          synced. A provider error is raised as the ``ResourceDirectoryError`` family.

        Windows may overlap one another; they are not merged.
        """
        windows: list[BusyWindow] = []
        rows = (
            CalendarEvent.objects.filter_by_organization(room.organization_id)
            .booking_room(room.id)
            .with_occurrences_overlapping(start, end)
        )
        for row in rows:
            if row.recurrence_rule is None:
                windows.append(BusyWindow(row.start_time, row.end_time))
                continue
            windows.extend(
                BusyWindow(occurrence.start_time, occurrence.end_time)
                for occurrence in row.get_occurrences_in_range(start, end, overlap=True)
            )

        link = (
            ResourceCalendarProviderLink.objects.filter_by_organization(room.organization_id)
            .filter(calendar=room)
            .first()
        )
        if link is not None and link.is_bookable:
            adapter = self.resource_directory_adapter_resolver.adapter_for(
                room.organization, link.provider
            )
            windows.extend(adapter.get_free_busy(room.email, start, end))

        return sorted(windows, key=lambda window: (window.start, window.end))

    def validate(
        self,
        room: Calendar,
        fingerprint: str,
        default_resolution: BookingResolution,
        overrides: Mapping[int, BookingResolution],
    ) -> BookingResolutionPlan | list[RejectedBooking]:
        """Check a resolution for every future booking of ``room``, all or nothing.

        ``overrides`` maps an event id from the preview to the resolution for that
        booking; every other booking takes ``default_resolution``.

        Raises ``StaleBookingPreviewError`` when ``fingerprint`` is not the current
        preview's. Otherwise returns the plan when every resolution is valid, or every
        rejected booking with its reason:

        - an override for an event that is not in the preview;
        - a move whose target is missing or not a room, is the room itself, is not
          bookable (archived, inactive, or its provider link is not bookable), is on
          another provider, is smaller than the event's attendee count, or is busy at
          any affected occurrence. A series is checked up to
          ``SERIES_BUSY_CHECK_HORIZON`` after its first affected occurrence. Two
          bookings moved to the same target must not overlap each other either.

        - a move or a remove-room of a booking on the room's own calendar: it holds the
          room by being on it, so it can only be cancelled.

        A booking whose event already books the target is not checked for capacity
        or busy: moving it only takes the deleted room off it.

        A provider error while reading a target's free/busy is raised, so an
        unchecked move is never accepted.
        """
        preview = self.preview(room)
        if fingerprint != preview.fingerprint:
            raise StaleBookingPreviewError()

        preview_ids = {booking.event_id for booking in preview.bookings}
        rejected = [
            RejectedBooking(event_id=event_id, reason=BookingRejectionReason.NOT_IN_PREVIEW)
            for event_id in sorted(set(overrides) - preview_ids)
        ]

        resolved = [
            ResolvedBooking(booking, overrides.get(booking.event_id, default_resolution))
            for booking in preview.bookings
        ]
        # A booking on the room's own calendar holds the room by being on it, not by
        # an allocation, so there is no allocation to swap or drop: only cancelling
        # the event (or the deletion) applies to it.
        on_room_calendar = {
            entry.booking.event_id
            for entry in resolved
            if entry.booking.calendar_id == room.id
            and (
                isinstance(entry.resolution, MoveBooking)
                or (
                    isinstance(entry.resolution, CancelBooking)
                    and entry.resolution.mode == BookingCancelMode.REMOVE_ROOM
                )
            )
        }
        rejected.extend(
            RejectedBooking(event_id=event_id, reason=BookingRejectionReason.ON_ROOM_CALENDAR)
            for event_id in sorted(on_room_calendar)
        )
        moves = [
            (entry.booking, entry.resolution)
            for entry in resolved
            if isinstance(entry.resolution, MoveBooking)
            and entry.booking.event_id not in on_room_calendar
        ]
        rejected.extend(self._validate_moves(room, moves))

        if rejected:
            return rejected
        return BookingResolutionPlan(
            room_id=room.id,
            organization_id=room.organization_id,
            fingerprint=preview.fingerprint,
            bookings=tuple(resolved),
        )

    def apply(self, plan: BookingResolutionPlan) -> ApplyResult:
        """Apply a validated plan, one booking at a time, in plan order.

        The event edits go through the injected ``CalendarService``, initialized with
        ``initialize_for_room_resolution``: it acts as the system, so it may edit every
        organizer's events, while provider writes, the room bookability guard, billing
        checks and the audit trail work as for any other edit. Authorizing the room
        deletion is the caller's job.

        Each booking is applied in its own transaction, because each step calls the
        provider and a provider call cannot be rolled back:

        - move: the room is swapped for the target in the event's rooms. A series that
          started before now is split at ``series_from`` with
          ``modify_recurring_event_from_date``, so the occurrences before it keep the
          room and the continuation books the target;
        - cancel, remove the room: the same, with the room dropped instead;
        - cancel the event: ``delete_event`` (a whole series is deleted), or
          ``cancel_recurring_event_from_date`` for a series that started before now.

        The organizer (the default owner of the event's calendar) is notified of each
        change. A cancelled event is notified before it is deleted, since the notifier
        reads it; the email itself goes out only when the step commits.

        A booking that no longer books the room is skipped and counted as applied, so
        running a plan again after a partial apply finishes the rest. The apply stops at
        the first booking that fails: that booking and every one after it are returned
        as pending, and nothing about them changed.

        Raises ``ValueError``, changing nothing, when the plan cancels the deletion for
        any booking.
        """
        if any(isinstance(entry.resolution, AbortDeletion) for entry in plan.bookings):
            raise ValueError("A plan that cancels the room deletion cannot be applied.")
        room = Calendar.objects.filter_by_organization(plan.organization_id).get(id=plan.room_id)
        self.calendar_service.initialize_for_room_resolution(room.organization)

        applied: list[int] = []
        for index, entry in enumerate(plan.bookings):
            event_id = entry.booking.event_id
            try:
                with transaction.atomic():
                    self._apply_booking(room, entry)
            except Exception:  # noqa: BLE001 -- any failure stops the apply; see the docstring
                logger.exception(
                    "Applying the resolution of booking %s for room %s failed.", event_id, room.id
                )
                return ApplyResult(
                    applied=tuple(applied),
                    pending=tuple(e.booking.event_id for e in plan.bookings[index:]),
                    failed_at=event_id,
                )
            applied.append(event_id)
        return ApplyResult(applied=tuple(applied), pending=(), failed_at=None)

    def _apply_booking(self, room: Calendar, entry: ResolvedBooking) -> None:
        booking, resolution = entry.booking, entry.resolution
        event = (
            CalendarEvent.objects.filter_by_organization(room.organization_id)
            .booking_room(room.id)
            .filter(id=booking.event_id)
            .first()
        )
        if event is None:
            return
        series_from = booking.series_from
        if series_from is not None and not event.get_occurrences_in_range(
            series_from, series_from + PREVIEW_SEARCH_WINDOW, max_occurrences=1, overlap=True
        ):
            # Already split at ``series_from`` by an earlier run: only the past
            # occurrences, which keep the room, are left on this row.
            return

        organizer_user_id = self._organizer_user_id(event)
        if (
            isinstance(resolution, CancelBooking)
            and resolution.mode == BookingCancelMode.CANCEL_EVENT
        ):
            if organizer_user_id is not None:
                self.booking_room_change_notifier.notify_booking_room_changed(
                    event.id, organizer_user_id, BookingRoomChange.EVENT_CANCELLED
                )
            self._cancel(event, series_from)
            return

        room_ids = [
            room_id
            for room_id in dict.fromkeys(
                allocation.calendar_fk_id for allocation in event.resource_allocations.all()
            )
            if room_id is not None and room_id != room.id
        ]
        if isinstance(resolution, MoveBooking):
            change = BookingRoomChange.MOVED
            if resolution.target_calendar_id not in room_ids:
                room_ids.append(resolution.target_calendar_id)
        else:
            change = BookingRoomChange.ROOM_REMOVED
        allocations = [ResourceAllocationInputData(resource_id=room_id) for room_id in room_ids]

        if series_from is not None:
            continuation = self.calendar_service.modify_recurring_event_from_date(
                parent_event=event,
                modification_start_date=series_from,
                resource_allocations_override=allocations,
            )
            notified_event_id = continuation.id if continuation is not None else event.id
        else:
            self.calendar_service.update_event(
                event.calendar_fk_id,  # type: ignore[arg-type]
                event.id,
                CalendarEventInputData(
                    # Omitted (``None``) title, description and attendees are kept.
                    title=None,
                    description=None,
                    start_time=event.start_time,
                    end_time=event.end_time,
                    timezone=event.timezone,
                    resource_allocations=allocations,
                    # Repeated, or the update turns a series into a single event.
                    recurrence_rule=(
                        event.recurrence_rule.to_rrule_string() if event.recurrence_rule else None
                    ),
                    parent_event_id=event.parent_recurring_object_fk_id,
                    is_recurring_exception=event.is_recurring_exception,
                ),
            )
            notified_event_id = event.id
        if organizer_user_id is not None:
            self.booking_room_change_notifier.notify_booking_room_changed(
                notified_event_id, organizer_user_id, change
            )

    def _cancel(self, event: CalendarEvent, series_from: datetime.datetime | None) -> None:
        """Delete ``event``, or only its occurrences from ``series_from`` on."""
        if series_from is not None:
            self.calendar_service.cancel_recurring_event_from_date(event, series_from)
        else:
            self.calendar_service.delete_event(
                event.calendar_fk_id,  # type: ignore[arg-type]
                event.id,
                delete_series=event.is_recurring,
            )

    @staticmethod
    def _organizer_user_id(event: CalendarEvent) -> int | None:
        """The user id of the event's organizer: ``CalendarOwnership.default_of`` its calendar."""
        ownership = CalendarOwnership.default_of(
            CalendarOwnership.objects.filter_by_organization(event.organization_id).filter(
                calendar__id=event.calendar_fk_id
            )
        )
        return ownership.membership_user_id if ownership is not None else None

    def _validate_moves(
        self, room: Calendar, moves: list[tuple[RoomBooking, MoveBooking]]
    ) -> list[RejectedBooking]:
        if not moves:
            return []
        organization_id = room.organization_id
        targets = (
            Calendar.objects.filter_by_organization(organization_id)
            .filter(
                id__in={move.target_calendar_id for _, move in moves},
                calendar_type=CalendarType.RESOURCE,
            )
            .in_bulk()
        )
        links = {
            link.calendar_fk_id: link
            for link in ResourceCalendarProviderLink.objects.filter_by_organization(
                organization_id
            ).filter(calendar__id__in=targets.keys())
        }
        events = (
            CalendarEvent.objects.filter_by_organization(organization_id)
            .filter(id__in=[booking.event_id for booking, _ in moves])
            .annotate_attendee_count()
            .in_bulk()
        )
        already_booking_target = {
            target_id: set(
                CalendarEvent.objects.filter_by_organization(organization_id)
                .booking_room(target_id)
                .filter(id__in=events.keys())
                .values_list("id", flat=True)
            )
            for target_id in targets
        }

        reasons: dict[int, BookingRejectionReason] = {}
        # Moves that passed every other check, by target, in preview order.
        busy_checks: dict[int, list[tuple[RoomBooking, list[BusyWindow]]]] = defaultdict(list)
        for booking, move in moves:
            event = events[booking.event_id]
            target = targets.get(move.target_calendar_id)
            link = links.get(move.target_calendar_id)
            if target is None:
                reasons[booking.event_id] = BookingRejectionReason.TARGET_NOT_FOUND
            elif target.id == room.id:
                reasons[booking.event_id] = BookingRejectionReason.TARGET_IS_SAME_ROOM
            elif target.visibility == CalendarVisibility.INACTIVE or (
                link is not None and not link.is_bookable
            ):
                reasons[booking.event_id] = BookingRejectionReason.TARGET_NOT_BOOKABLE
            elif target.provider != room.provider:
                reasons[booking.event_id] = BookingRejectionReason.TARGET_ON_DIFFERENT_PROVIDER
            elif event.id in already_booking_target[target.id]:
                continue
            elif target.capacity is not None and event.attendee_count > target.capacity:  # type: ignore[attr-defined]
                reasons[booking.event_id] = BookingRejectionReason.TARGET_TOO_SMALL
            else:
                busy_checks[target.id].append((booking, self._affected_occurrences(event, booking)))

        # One busy read per target, so one provider free/busy call however many
        # bookings move there. Each booking is checked against that, and against the
        # occurrences of the bookings moved there before it in this plan.
        for target_id, checks in busy_checks.items():
            all_occurrences = [
                occurrence for _, occurrences in checks for occurrence in occurrences
            ]
            if not all_occurrences:
                continue
            windows = self.room_busy_windows(
                targets[target_id],
                min(occurrence.start for occurrence in all_occurrences),
                max(occurrence.end for occurrence in all_occurrences),
            )
            for booking, occurrences in checks:
                if any(
                    _overlaps(window, occurrence.start, occurrence.end)
                    for occurrence in occurrences
                    for window in windows
                ):
                    reasons[booking.event_id] = BookingRejectionReason.TARGET_BUSY
                else:
                    windows.extend(occurrences)

        return [
            RejectedBooking(event_id=booking.event_id, reason=reasons[booking.event_id])
            for booking, _ in moves
            if booking.event_id in reasons
        ]

    def _affected_occurrences(self, event: CalendarEvent, booking: RoomBooking) -> list[BusyWindow]:
        """The occurrences a resolution of ``booking`` changes.

        A one-off booking is its own time. A series is every occurrence from its
        first affected one, up to ``SERIES_BUSY_CHECK_HORIZON`` later.
        """
        if not booking.is_series:
            return [BusyWindow(booking.start, booking.end)]
        occurrences = event.get_occurrences_in_range(
            booking.start, booking.start + SERIES_BUSY_CHECK_HORIZON, overlap=True
        )
        return [
            BusyWindow(occurrence.start_time, occurrence.end_time) for occurrence in occurrences
        ]

    @staticmethod
    def _one_off_booking(row: CalendarEvent) -> RoomBooking:
        return RoomBooking(
            event_id=row.id,
            calendar_id=row.calendar_fk_id,
            title=row.title,
            start=row.start_time,
            end=row.end_time,
            is_series=False,
            series_from=None,
        )

    @staticmethod
    def _fingerprint(rows: Iterable[CalendarEvent]) -> str:
        parts = sorted(
            (
                row.id,
                row.modified.isoformat(),
                row.recurrence_rule.to_rrule_string() if row.recurrence_rule else "",
            )
            for row in rows
        )
        payload = "\n".join(f"{event_id}|{modified}|{rule}" for event_id, modified, rule in parts)
        return hashlib.sha256(payload.encode()).hexdigest()
