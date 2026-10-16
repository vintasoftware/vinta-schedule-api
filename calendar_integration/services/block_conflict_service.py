"""Find and cancel the events a new blocked time overlaps.

A block made with ``BlockedTimeDeclineScope.NEW_AND_EXISTING`` cancels the events its
calendar already holds in the blocked period.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DatabaseError, transaction

from calendar_integration.exceptions import CalendarIntegrationError, TooManyBlockCancelsError
from calendar_integration.models import CalendarEvent
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
class BlockCancelResult:
    """How the cancels a block caused went."""

    cancelled_event_count: int = 0
    failed_event_count: int = 0


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
    """Creates a block and cancels the events it overlaps on the blocked calendar.

    Writes go through the injected services, so call ``initialize`` first: each cancel
    then runs under the caller's own permissions.
    """

    def __init__(
        self,
        calendar_service: CalendarService,
        appointment_type_service: AppointmentTypeService,
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
        self.calendar_service.initialize_without_provider(
            user_or_token=user_or_token, organization=organization
        )
        appointment_type_service = self.appointment_type_service
        if appointment_type_service.calendar_service is None:
            raise CalendarIntegrationError(
                "The appointment-type service needs a calendar service to cancel events."
            )
        appointment_type_service.calendar_service.initialize_without_provider(
            user_or_token=user_or_token, organization=organization
        )
        appointment_type_service.initialize(organization=organization)

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

        ``start_time`` / ``end_time`` are wall-clock times in ``timezone``. A
        NEW_AND_EXISTING block checks how many events it would cancel before anything is
        written. The cancels run after the block is written, each on its own (see
        ``cancel_block_conflicts``), so a cancel that fails leaves the block and the other
        cancels in place.

        Call ``initialize`` first, so each cancel runs under the caller's own permissions.

        :raises ValueError: When ``timezone`` is not an IANA zone.
        :raises BlockDeclineOptionsWithRecurrenceError: When ``decline_options`` is given
            with ``rrule_string`` (raised by ``create_blocked_time``, before any cancel).
        :raises TooManyBlockCancelsError: When the block would cancel more than
            ``MAX_BLOCK_CANCELS`` events.
        """
        events_to_cancel: list[BlockConflictEvent] | None = None
        if decline_options is not None and decline_options.cancels_existing:
            events_to_cancel = self.find_events_to_cancel(
                calendar,
                wall_clock_to_utc(start_time, timezone),
                wall_clock_to_utc(end_time, timezone),
            )
            if len(events_to_cancel) > MAX_BLOCK_CANCELS:
                raise TooManyBlockCancelsError(len(events_to_cancel), MAX_BLOCK_CANCELS)
        blocked_time = self.calendar_service.create_blocked_time(
            calendar=calendar,
            start_time=start_time,
            end_time=end_time,
            timezone=timezone,
            reason=reason,
            rrule_string=rrule_string,
            decline_options=decline_options,
        )
        if events_to_cancel is None:
            return blocked_time, BlockCancelResult()
        return blocked_time, self.cancel_block_conflicts(events_to_cancel)

    @staticmethod
    def find_events_to_cancel(
        calendar: Calendar, start: datetime.datetime, end: datetime.datetime
    ) -> list[BlockConflictEvent]:
        """The event occurrences on ``calendar`` that overlap the UTC instants
        ``start`` / ``end``, edges excluded. Reads only."""
        if end <= start:
            return []

        on_calendar = _overlapping_occurrences(
            CalendarEvent.objects.filter_by_organization(calendar.organization_id)
            .filter(calendar_fk_id=calendar.id)
            .overlapping_range(start, end),
            start,
            end,
        )
        # A bundle copy belongs to the bundle's event, which lives on another calendar.
        return [c for c in on_calendar if not c.event.is_bundle_representation]

    def cancel_block_conflicts(
        self, events_to_cancel: list[BlockConflictEvent]
    ) -> BlockCancelResult:
        """Cancel ``events_to_cancel``, each on its own, as far as possible.

        A single event is deleted, an appointment-type event goes through
        ``cancel_appointment_type_event``, and an occurrence of a recurring series gets
        a cancellation exception (the series stays). Deletes ask the provider to email
        every guest. A cancel that fails is rolled back alone and counted; the rest go
        on. Only the event id and the error class are logged.

        Call ``initialize`` first, so each cancel runs under the caller's own permissions.
        """
        calendar_service = self.calendar_service
        appointment_type_service = self.appointment_type_service
        cancelled = 0
        failed = 0
        for conflict in events_to_cancel:
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
        return BlockCancelResult(cancelled_event_count=cancelled, failed_event_count=failed)
