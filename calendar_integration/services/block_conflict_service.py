"""Find and cancel the events a new blocked time overlaps.

A block made with ``BlockedTimeDeclineScope.NEW_AND_EXISTING`` cancels the events its
calendar already holds in the blocked period. ``find_block_conflicts`` is the read both
the cancel and the ``blockedTimeImpact`` preview run, so the preview counts exactly what
a save would cancel at that moment.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DatabaseError, transaction

from calendar_integration.exceptions import (
    BlockDeclineOptionsWithRecurrenceError,
    CalendarIntegrationError,
    TooManyBlockCancelsError,
)
from calendar_integration.models import CalendarEvent, CalendarOwnership
from calendar_integration.services.calendar_service_utils import wall_clock_to_utc
from calendar_integration.services.calendar_webhook_service import PROVIDER_CALL_ERRORS


if TYPE_CHECKING:
    from calendar_integration.models import BlockedTime, Calendar
    from calendar_integration.querysets import CalendarEventQuerySet
    from calendar_integration.services.appointment_type_service import AppointmentTypeService
    from calendar_integration.services.calendar_service import CalendarService
    from calendar_integration.services.dataclasses import BlockDeclineOptions
    from organizations.models import Organization
    from public_api.models import SystemUser
    from users.models import User


logger = logging.getLogger(__name__)

#: Google's ``sendUpdates`` for a cancel a block causes: every guest is emailed.
BLOCK_CANCEL_SEND_UPDATES = "all"

#: Most events one block may cancel. The cancels run inside the request that creates the
#: block, one provider call each, so a block that would cancel more is refused.
MAX_BLOCK_CANCELS = 25

#: Errors one cancel can raise without stopping the others. Each cancel runs in its own
#: savepoint, so a database error rolls back only that cancel.
_CANCEL_ERRORS: tuple[type[Exception], ...] = (
    *PROVIDER_CALL_ERRORS,
    PermissionDenied,
    ObjectDoesNotExist,
    DjangoValidationError,
    DatabaseError,
    NotImplementedError,
)


@dataclass(frozen=True)
class BlockConflictEvent:
    """One event occurrence a block overlaps.

    ``event`` is the stored row to act on. For an occurrence of a recurring series that
    has no row of its own, ``event`` is the series' master and ``occurrence_start`` is
    the occurrence's original start; cancelling it cancels only that occurrence.
    """

    event: CalendarEvent
    occurrence_start: datetime.datetime | None = None


@dataclass(frozen=True)
class BlockConflicts:
    """What a block over a period would do to existing events.

    ``events_to_cancel``: occurrences held on the blocked calendar.
    ``conflict_events``: occurrences held on other calendars where the blocked
    calendar's owner attends, and bundle copies on the blocked calendar. Vinta Schedule
    never cancels these. But when the block is written to Google as an Out of office
    event with NEW_AND_EXISTING, Google declines the owner's invitations to them. The
    events themselves stay in place for everyone else.
    """

    events_to_cancel: list[BlockConflictEvent]
    conflict_events: list[BlockConflictEvent]


@dataclass(frozen=True)
class BlockCancelResult:
    """How the cancels a block caused went."""

    cancelled_event_count: int = 0
    failed_event_count: int = 0
    conflict_event_count: int = 0


def _overlapping_occurrences(
    events: CalendarEventQuerySet, start: datetime.datetime, end: datetime.datetime
) -> list[BlockConflictEvent]:
    """The occurrences in ``events`` that overlap ``(start, end)``, edges excluded.

    Recurring masters are expanded; a stored row found both on its own and through its
    master's expansion is returned once.
    """
    found: list[BlockConflictEvent] = []
    seen_row_ids: set[int] = set()

    def add(conflict: BlockConflictEvent, occurrence: CalendarEvent) -> None:
        if not (occurrence.start_time < end and occurrence.end_time > start):
            return
        if conflict.occurrence_start is None:
            if conflict.event.pk in seen_row_ids:
                return
            seen_row_ids.add(conflict.event.pk)
        found.append(conflict)

    for event in events:
        if event.recurrence_rule_fk_id is None:
            add(BlockConflictEvent(event=event), event)
            continue
        for occurrence in event.get_occurrences_in_range(
            start, end, include_self=False, include_exceptions=True, overlap=True
        ):
            if occurrence.pk is not None:
                # A modified occurrence: a row of its own.
                add(BlockConflictEvent(event=occurrence), occurrence)
            else:
                add(
                    BlockConflictEvent(event=event, occurrence_start=occurrence.start_time),
                    occurrence,
                )
    return found


class BlockConflictService:
    """Finds the events a block overlaps, and cancels the ones on the blocked calendar.

    ``find_block_conflicts`` only reads. ``create_block`` and ``cancel_block_conflicts``
    write through the injected services, so call ``initialize`` first: each cancel then
    runs under the caller's own permissions.
    """

    def __init__(
        self,
        calendar_service: CalendarService | None = None,
        appointment_type_service: AppointmentTypeService | None = None,
    ) -> None:
        self.calendar_service = calendar_service
        self.appointment_type_service = appointment_type_service

    def initialize(
        self, user_or_token: User | str | SystemUser | None, organization: Organization
    ) -> None:
        """Bind both services to the caller and the organization.

        The appointment-type service cancels through its own calendar service, so that
        one is bound to the same caller too.
        """
        calendar_service, appointment_type_service = self._services()
        calendar_service.initialize_without_provider(
            user_or_token=user_or_token, organization=organization
        )
        if appointment_type_service.calendar_service is None:
            raise CalendarIntegrationError(
                "The appointment-type service needs a calendar service to cancel events."
            )
        appointment_type_service.calendar_service.initialize_without_provider(
            user_or_token=user_or_token, organization=organization
        )
        appointment_type_service.initialize(organization=organization)

    def _services(self) -> tuple[CalendarService, AppointmentTypeService]:
        if self.calendar_service is None or self.appointment_type_service is None:
            raise CalendarIntegrationError(
                "BlockConflictService needs a calendar and an appointment-type service."
            )
        return self.calendar_service, self.appointment_type_service

    def find_block_conflicts(
        self,
        calendar: Calendar,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        timezone: str,
    ) -> BlockConflicts:
        """Find the events a block on ``calendar`` over the given period overlaps.

        ``start_time`` / ``end_time`` are wall-clock times in ``timezone``, as
        ``createBlockedTime`` takes them. An event that only touches the period at an
        edge is not overlapped. Reads only.

        :raises ValueError: When ``timezone`` is not an IANA zone.
        """
        return self._find_conflicts(
            calendar, wall_clock_to_utc(start_time, timezone), wall_clock_to_utc(end_time, timezone)
        )

    def create_block(
        self,
        *,
        calendar: Calendar,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        timezone: str,
        reason: str = "",
        rrule_string: str | None = None,
        decline_options: BlockDeclineOptions | None = None,
    ) -> tuple[BlockedTime, BlockCancelResult]:
        """Create a blocked time and, for a NEW_AND_EXISTING block, cancel what it overlaps.

        ``start_time`` / ``end_time`` are wall-clock times in ``timezone``. A block with
        options checks its timezone, its recurrence and how many events it would cancel
        before anything is written. The cancels run after the block is written, each on
        its own (see ``cancel_block_conflicts``), so a cancel that fails leaves the block
        and the other cancels in place.

        Call ``initialize`` first, so each cancel runs under the caller's own permissions.

        :raises ValueError: When ``timezone`` is not an IANA zone.
        :raises BlockDeclineOptionsWithRecurrenceError: When ``decline_options`` is given
            with ``rrule_string``.
        :raises TooManyBlockCancelsError: When the block would cancel more than
            ``MAX_BLOCK_CANCELS`` events.
        """
        calendar_service, _appointment_type_service = self._services()
        conflicts: BlockConflicts | None = None
        if decline_options is not None:
            if rrule_string:
                raise BlockDeclineOptionsWithRecurrenceError()
            start = wall_clock_to_utc(start_time, timezone)
            end = wall_clock_to_utc(end_time, timezone)
            if decline_options.cancels_existing:
                conflicts = self._find_conflicts(calendar, start, end)
                if len(conflicts.events_to_cancel) > MAX_BLOCK_CANCELS:
                    raise TooManyBlockCancelsError(
                        len(conflicts.events_to_cancel), MAX_BLOCK_CANCELS
                    )
        blocked_time = calendar_service.create_blocked_time(
            calendar=calendar,
            start_time=start_time,
            end_time=end_time,
            timezone=timezone,
            reason=reason,
            rrule_string=rrule_string,
            decline_options=decline_options,
        )
        if conflicts is None:
            return blocked_time, BlockCancelResult()
        return blocked_time, self.cancel_block_conflicts(conflicts)

    def _find_conflicts(
        self, calendar: Calendar, start: datetime.datetime, end: datetime.datetime
    ) -> BlockConflicts:
        """``find_block_conflicts`` over the UTC instants ``start`` / ``end``."""
        if end <= start:
            return BlockConflicts(events_to_cancel=[], conflict_events=[])

        on_calendar = _overlapping_occurrences(
            CalendarEvent.objects.filter_by_organization(calendar.organization_id)
            .filter(calendar_fk_id=calendar.id)
            .overlapping_range(start, end),
            start,
            end,
        )
        # A bundle copy belongs to the bundle's event, which lives on another calendar.
        events_to_cancel = [c for c in on_calendar if not c.event.is_bundle_representation]
        bundle_copies = [c for c in on_calendar if c.event.is_bundle_representation]

        owner_ids = CalendarOwnership.objects.filter_by_organization(
            calendar.organization_id
        ).filter(calendar_fk_id=calendar.id, membership_user_id__isnull=False)
        elsewhere = _overlapping_occurrences(
            CalendarEvent.objects.filter_by_organization(calendar.organization_id)
            .filter(attendances__membership_user_id__in=owner_ids.values("membership_user_id"))
            .exclude(calendar_fk_id=calendar.id)
            .distinct()
            .overlapping_range(start, end),
            start,
            end,
        )
        return BlockConflicts(
            events_to_cancel=events_to_cancel, conflict_events=bundle_copies + elsewhere
        )

    def cancel_block_conflicts(self, conflicts: BlockConflicts) -> BlockCancelResult:
        """Cancel ``conflicts.events_to_cancel``, each on its own, as far as possible.

        A single event is deleted, an appointment-type event goes through
        ``cancel_appointment_type_event``, and an occurrence of a recurring series gets
        a cancellation exception (the series stays). Deletes ask the provider to email
        every guest. A cancel that fails is rolled back alone and counted; the rest go
        on. Only the event id and the error class are logged.

        Call ``initialize`` first, so each cancel runs under the caller's own permissions.
        """
        calendar_service, appointment_type_service = self._services()
        cancelled = 0
        failed = 0
        for conflict in conflicts.events_to_cancel:
            event = conflict.event
            try:
                with transaction.atomic():
                    if conflict.occurrence_start is not None:
                        calendar_service.cancel_event_occurrence(
                            calendar_id=event.calendar_fk_id,  # type: ignore[arg-type]
                            master_event_id=event.id,
                            recurrence_id=conflict.occurrence_start,
                        )
                    elif event.appointment_type_fk_id is not None:
                        appointment_type_service.cancel_appointment_type_event(
                            event_id=event.id, send_updates=BLOCK_CANCEL_SEND_UPDATES
                        )
                    else:
                        calendar_service.delete_event(
                            calendar_id=event.calendar_fk_id,  # type: ignore[arg-type]
                            event_id=event.id,
                            send_updates=BLOCK_CANCEL_SEND_UPDATES,
                        )
            except _CANCEL_ERRORS as exc:
                failed += 1
                logger.warning(
                    "Could not cancel event %s overlapped by a new block: %s",
                    event.id,
                    type(exc).__name__,
                )
            else:
                cancelled += 1
        return BlockCancelResult(
            cancelled_event_count=cancelled,
            failed_event_count=failed,
            conflict_event_count=len(conflicts.conflict_events),
        )
