"""Available-times, blocked-times, and availability/unavailability window reads.

``AvailabilityService`` owns the availability concern extracted from the
``CalendarService`` facade. It is a plain class (not a DI-container provider):
the facade constructs it, after authentication, feeding it the shared
:class:`CalendarServiceContext` so it never re-authenticates or re-builds a
calendar adapter (the perf guardrail). Everything it needs arrives via the
constructor:

- ``context`` — the immutable auth snapshot (organization, user_or_token,
  account, calendar_adapter, permission_service, side_effects_service). Read
  through ``self._context``; the auth guards in ``type_guards.py`` inspect the
  same ``organization`` / ``account`` / ``calendar_adapter`` attributes the
  context exposes, so behavior is byte-for-byte identical to the former methods.
- ``recurrence_manager`` — the stateless :class:`RecurrenceManager` the facade
  also holds; the recurring blocked/available methods delegate to it exactly as
  the former facade methods did.
- ``host`` — the :class:`AvailabilityServiceHost` (in Phase 4 the facade
  itself). The availability concern routes three things back through it:

  - **event reads** (``get_calendar_events_expanded``) — the event concern,
    extracted in Phase 2; reaching it via the host keeps a single
    implementation and the call graph the existing test suite asserts on.
  - **blocked-time creation** (``bulk_create_manual_blocked_times``) — stays
    resident on the facade (other facade flows reference it and it is not part
    of the availability concern's extracted surface), so ``create_blocked_time``
    routes through the host.
  - **recurrence-rule creation** (``_create_recurrence_rule_if_needed``) — a
    shared facade helper used by both availability and (facade-resident)
    blocked-time bulk creation; routed through the host so it has one
    implementation and stays byte-for-byte.

Routing through the host keeps single implementations and behavior
byte-for-byte; later phases swap concerns in without touching this service.

The interval-subtraction math (``_subtract_busy_intervals``) and the
recurrence-expansion reads (``get_available_times_expanded`` /
``get_blocked_times_expanded``) are moved verbatim — no added queries inside
loops, no changed query structure, no algorithmic-complexity change.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

from django.db import transaction
from django.db.models import Q

from calendar_integration.constants import CalendarType
from calendar_integration.models import (
    AvailableTime,
    AvailableTimeBulkModification,
    AvailableTimeRecurrenceException,
    BlockedTime,
    BlockedTimeBulkModification,
    BlockedTimeRecurrenceException,
    Calendar,
    CalendarEvent,
    RecurrenceRule,
    RecurringMixin,
)
from calendar_integration.services.calendar_service_utils import (
    serialize_event as _serialize_event_util,
)
from calendar_integration.services.dataclasses import (
    AvailableTimeWindow,
    BlockedTimeData,
    UnavailableTimeWindow,
)
from calendar_integration.services.protocols.base_calendar_service import BaseCalendarService
from calendar_integration.services.protocols.initializer_or_authenticated_calendar_service import (
    InitializedOrAuthenticatedCalendarService,
)
from calendar_integration.services.type_guards import (
    is_initialized_or_authenticated_calendar_service,
)


if TYPE_CHECKING:
    from collections.abc import Iterable

    from calendar_integration.services.calendar_service_context import CalendarServiceContext
    from calendar_integration.services.dataclasses import CalendarEventData
    from calendar_integration.services.recurrence_manager import RecurrenceManager


class AvailabilityServiceHost(Protocol):
    """The collaborator surface the availability concern routes back to the facade for.

    Three concerns are not part of the availability concern's extracted surface and
    stay on the facade:

    - **event reads** (``get_calendar_events_expanded``) — the event concern
      (Phase 2); reached through the host to keep one implementation and the call
      graph the existing test suite patches via the facade;
    - **blocked-time bulk creation** (``bulk_create_manual_blocked_times``) — a
      facade-resident helper (not part of the availability surface); ``create_blocked_time``
      routes through it;
    - **recurrence-rule creation** (``_create_recurrence_rule_if_needed``) — a shared
      facade helper used by availability writes and facade-resident blocked-time bulk
      creation alike; routed through the host for a single implementation.

    In Phase 4 the facade supplies *itself*. Later phases may swap individual
    concerns without changing this service's call sites.
    """

    def get_calendar_events_expanded(
        self,
        calendar: Calendar,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[CalendarEvent]: ...

    def bulk_create_manual_blocked_times(
        self,
        calendar: Calendar,
        blocked_times: Iterable[tuple[datetime.datetime, datetime.datetime, str, str, str | None]],
    ) -> Iterable[BlockedTime]: ...

    def _create_recurrence_rule_if_needed(
        self, rrule_string: str | None
    ) -> RecurrenceRule | None: ...


class AvailabilityService:
    """Owns available-times, blocked-times, and availability/unavailability windows."""

    def __init__(
        self,
        context: CalendarServiceContext,
        recurrence_manager: RecurrenceManager,
        host: AvailabilityServiceHost,
    ) -> None:
        self._context = context
        self._recurrence_manager = recurrence_manager
        # Phase 4 seam: event reads, blocked-time bulk creation, and the shared
        # recurrence-rule helper are reached through the host (the facade).
        # See ``AvailabilityServiceHost``.
        self._host = host

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _serialize_event(self, event: CalendarEvent) -> CalendarEventData:
        """Build webhook payload for calendar event."""
        return _serialize_event_util(event)

    def _remove_available_time_windows_that_overlap_with_blocked_times_and_events(
        self,
        calendar_id: int,
        blocked_times: Iterable[BlockedTime],
        events: Iterable[CalendarEvent],
        start_time: datetime.datetime,
        end_time: datetime.datetime,
    ):
        """
        Removes AvailableTime windows that overlap with BlockedTime or CalendarEvent instances.
        """
        context = cast("InitializedOrAuthenticatedCalendarService", self._context)
        if not context.organization:
            return

        blocked_times = list(blocked_times)
        events = list(events)

        available_time_windows = AvailableTime.objects.filter(
            calendar_fk_id=calendar_id,
            start_time__gte=start_time,
            end_time__lte=end_time,
            organization_id=context.organization.id,
        )

        available_time_windows_to_delete: list[int] = []

        for available_time in available_time_windows:
            # Check if the available time overlaps with any blocked time
            overlaps_with_blocked = any(
                bt.start_time < available_time.end_time and bt.end_time > available_time.start_time
                for bt in blocked_times
            )
            # Check if the available time overlaps with any event
            overlaps_with_event = any(
                event.start_time < available_time.end_time
                and event.end_time > available_time.start_time
                for event in events
            )

            if overlaps_with_blocked or overlaps_with_event:
                # If it overlaps, remove it from the list of blocked times
                available_time_windows_to_delete.append(available_time.id)

        AvailableTime.objects.filter(
            id__in=available_time_windows_to_delete,
            organization_id=context.organization.id,
            calendar_fk_id=calendar_id,
        ).delete()

    @staticmethod
    def _subtract_busy_intervals(
        window_start: datetime.datetime,
        window_end: datetime.datetime,
        busy_intervals: Iterable[tuple[datetime.datetime, datetime.datetime]],
    ) -> list[tuple[datetime.datetime, datetime.datetime]]:
        """Return the parts of [window_start, window_end] not covered by any busy interval.

        Busy intervals may be unsorted, overlapping, or extend beyond the window; they
        are clipped to the window and merged on the fly. A window fully covered by busy
        time yields an empty list.
        """
        clipped = sorted(
            (max(start, window_start), min(end, window_end))
            for start, end in busy_intervals
            if end > window_start and start < window_end
        )

        free: list[tuple[datetime.datetime, datetime.datetime]] = []
        cursor = window_start
        for busy_start, busy_end in clipped:
            if busy_start > cursor:
                free.append((cursor, busy_start))
            cursor = max(cursor, busy_end)
        if cursor < window_end:
            free.append((cursor, window_end))
        return free

    # ------------------------------------------------------------------
    # Availability / unavailability window reads
    # ------------------------------------------------------------------

    def get_unavailable_time_windows_in_range(
        self,
        calendar: Calendar,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
    ) -> list[UnavailableTimeWindow]:
        """
        Retrieve unavailable time windows for a calendar within a specified date range.
        This includes both calendar events (with recurring instances) and blocked times
        that overlap with the given time range.

        :param calendar: The calendar to retrieve unavailable time windows for.
        :param start_datetime: Start date for the availability search.
        :param end_datetime: End date for the availability search.
        :return: List of UnavailableTimeWindow instances.
        """
        if not is_initialized_or_authenticated_calendar_service(
            cast("BaseCalendarService", self._context)
        ):
            raise

        # Get expanded calendar events (including recurring instances)
        # This handles both master events and their generated instances
        calendar_events = self._host.get_calendar_events_expanded(
            calendar=calendar,
            start_date=start_datetime,
            end_date=end_datetime,
        )

        # Get expanded blocked times (including recurring instances)
        # Replace the current blocked_times query with:
        blocked_times = self.get_blocked_times_expanded(
            calendar=calendar,
            start_date=start_datetime,
            end_date=end_datetime,
        )

        # If this calendar is part of any bundles, include bundle events
        bundle_calendars = Calendar.objects.filter(
            calendar_type=CalendarType.BUNDLE,
            bundle_children=calendar,
            organization_id=calendar.organization_id,
        )

        bundle_events: list[CalendarEvent] = []
        for bundle_calendar in bundle_calendars:
            # Get bundle events from the bundle calendar directly
            bundle_calendar_events = CalendarEvent.objects.filter(
                bundle_calendar=bundle_calendar,
                start_time__lt=end_datetime,
                end_time__gt=start_datetime,
                organization_id=bundle_calendar.organization_id,
            )
            # Only include bundle events that aren't already in our calendar_events
            # (to avoid counting the same event twice)
            bundle_events.extend(
                bundle_event
                for bundle_event in bundle_calendar_events
                if all(ce.id != bundle_event.id for ce in calendar_events)
            )

        # Combine regular events with bundle events
        all_events = calendar_events + bundle_events

        return sorted(
            [
                UnavailableTimeWindow(
                    start_time=event.start_time,
                    end_time=event.end_time,
                    reason="calendar_event",
                    id=event.id,
                    data=self._serialize_event(event),
                )
                for event in all_events
            ]
            + [
                UnavailableTimeWindow(
                    start_time=blocked_time.start_time,
                    end_time=blocked_time.end_time,
                    reason="blocked_time",
                    id=blocked_time.id,
                    data=BlockedTimeData(
                        id=blocked_time.id,
                        calendar_external_id=blocked_time.calendar.external_id,
                        start_time=blocked_time.start_time,
                        end_time=blocked_time.end_time,
                        timezone=blocked_time.timezone,
                        reason=blocked_time.reason,
                        external_id=blocked_time.external_id,
                        meta=blocked_time.meta or {},
                    ),
                )
                for blocked_time in blocked_times
            ],
            key=lambda x: x.start_time,
        )

    def get_availability_windows_in_range(
        self, calendar: Calendar, start_datetime: datetime.datetime, end_datetime: datetime.datetime
    ) -> Iterable[AvailableTimeWindow]:
        """
        Retrieve availability windows for a calendar within a specified date range.
        :param calendar_id: ID of the calendar to retrieve availability for.
        :param start_datetime: Start date for the availability search.
        :param end_datetime: End date for the availability search.
        :return: Iterable of AvalableTimeWindow instances.
        """
        if not is_initialized_or_authenticated_calendar_service(
            cast("BaseCalendarService", self._context)
        ):
            raise

        if calendar.manage_available_windows:
            # Declared availability windows (recurring instances expanded).
            available_times = self.get_available_times_expanded(
                calendar=calendar,
                start_date=start_datetime,
                end_date=end_datetime,
            )

            # Net availability = declared windows minus busy (events + blocked times).
            # Subtract the unavailable windows so callers get true bookable time and
            # don't have to reconcile two overlapping lists client-side.
            unavailable_windows = self.get_unavailable_time_windows_in_range(
                calendar, start_datetime, end_datetime
            )
            busy_intervals = [(uw.start_time, uw.end_time) for uw in unavailable_windows]

            return [
                AvailableTimeWindow(
                    start_time=free_start,
                    end_time=free_end,
                    id=available_time.id,
                    can_book_partially=False,
                    timezone=available_time.timezone,
                )
                for available_time in available_times
                for free_start, free_end in AvailabilityService._subtract_busy_intervals(
                    available_time.start_time, available_time.end_time, busy_intervals
                )
            ]

        unavailable_windows_sorted_by_start_datetime = self.get_unavailable_time_windows_in_range(
            calendar, start_datetime, end_datetime
        )
        available_windows = []

        if not unavailable_windows_sorted_by_start_datetime:
            # If there are no unavailable windows, the entire range is available
            return [
                AvailableTimeWindow(
                    start_time=start_datetime,
                    end_time=end_datetime,
                    id=None,  # ID will be set when saving to the database
                    can_book_partially=True,
                )
            ]

        if start_datetime < unavailable_windows_sorted_by_start_datetime[0].start_time:
            available_windows.append(
                (start_datetime, unavailable_windows_sorted_by_start_datetime[0].start_time)
            )
        for i in range(len(unavailable_windows_sorted_by_start_datetime) - 1):
            current_end = unavailable_windows_sorted_by_start_datetime[i].end_time
            next_start = unavailable_windows_sorted_by_start_datetime[i + 1].start_time
            if current_end < next_start:
                available_windows.append((current_end, next_start))
        if end_datetime > unavailable_windows_sorted_by_start_datetime[-1].end_time:
            available_windows.append(
                (unavailable_windows_sorted_by_start_datetime[-1].end_time, end_datetime)
            )

        return [
            AvailableTimeWindow(
                start_time=start,
                end_time=end,
                can_book_partially=True,
                # this calendar doesn't manage available windows, so there is no
                # AvailableTime record in the database
                id=None,
            )
            for start, end in available_windows
        ]

    # ------------------------------------------------------------------
    # Available-time writes
    # ------------------------------------------------------------------

    def bulk_create_availability_windows(
        self,
        calendar: Calendar,
        availability_windows: Iterable[
            tuple[datetime.datetime, datetime.datetime, str, str | None]
        ],
    ) -> Iterable[AvailableTime]:
        """
        Create availability windows for a calendar (with optional recurrence support).
        :param calendar: The calendar to create the availability windows for.
        :param availability_windows: Iterable of tuples containing (start_time, end_time, rrule_string).
        :return: List of created AvailableTime instances.
        """
        if not is_initialized_or_authenticated_calendar_service(
            cast("BaseCalendarService", self._context)
        ):
            raise

        if not calendar.manage_available_windows:
            raise ValueError("This calendar does not manage available windows.")

        availability_windows_to_create = []

        for start_time, end_time, timezone, rrule_string in availability_windows:
            # Create recurrence rule if provided
            recurrence_rule = self._host._create_recurrence_rule_if_needed(rrule_string)

            available_time = AvailableTime(
                calendar=calendar,
                start_time_tz_unaware=start_time,
                end_time_tz_unaware=end_time,
                timezone=timezone,
                organization_id=calendar.organization_id,
                recurrence_rule=recurrence_rule,
            )
            availability_windows_to_create.append(available_time)

        return AvailableTime.objects.bulk_create(availability_windows_to_create)

    @transaction.atomic()
    def batch_modify_available_times(
        self,
        calendar: Calendar,
        operations: Iterable[dict],
    ) -> list[AvailableTime]:
        """Apply a batch of create/update/delete operations to a calendar's available times.

        Row-atomic: each operation acts on a whole AvailableTime row. Runs in a single
        transaction — any failure rolls the whole batch back. Update/delete operations
        are scoped to this calendar (and organization); a missing id raises ValueError.

        :param calendar: The calendar whose available times are being modified.
        :param operations: Iterable of dicts, each with an ``action`` of
            ``create`` / ``update`` / ``delete`` plus the relevant fields
            (``id``, ``start_time``, ``end_time``, ``timezone``, ``rrule_string``).
        :return: The calendar's available times after the batch is applied.
        """
        context = cast("BaseCalendarService", self._context)
        if not is_initialized_or_authenticated_calendar_service(context):
            raise

        if not calendar.manage_available_windows:
            raise ValueError("This calendar does not manage available windows.")

        scoped = AvailableTime.objects.filter_by_organization(context.organization.id).filter(
            calendar_fk=calendar
        )

        for operation in operations:
            action = operation["action"]

            if action == "create":
                recurrence_rule = self._host._create_recurrence_rule_if_needed(
                    operation.get("rrule_string")
                )
                AvailableTime.objects.create(
                    calendar=calendar,
                    organization_id=calendar.organization_id,
                    start_time_tz_unaware=operation["start_time"],
                    end_time_tz_unaware=operation["end_time"],
                    timezone=operation["timezone"],
                    recurrence_rule=recurrence_rule,
                )
            elif action == "update":
                try:
                    available_time = scoped.get(id=operation["id"])
                except AvailableTime.DoesNotExist as e:
                    raise ValueError(
                        f"Available time {operation['id']} not found in this calendar."
                    ) from e
                if "start_time" in operation:
                    available_time.start_time_tz_unaware = operation["start_time"]
                if "end_time" in operation:
                    available_time.end_time_tz_unaware = operation["end_time"]
                if "timezone" in operation:
                    available_time.timezone = operation["timezone"]
                if "rrule_string" in operation:
                    available_time.recurrence_rule = self._host._create_recurrence_rule_if_needed(
                        operation["rrule_string"]
                    )
                available_time.save()
            elif action == "delete":
                try:
                    scoped.get(id=operation["id"]).delete()
                except AvailableTime.DoesNotExist as e:
                    raise ValueError(
                        f"Available time {operation['id']} not found in this calendar."
                    ) from e

        return list(
            AvailableTime.objects.filter_by_organization(context.organization.id).filter(
                calendar_fk=calendar
            )
        )

    @transaction.atomic()
    def create_available_time(
        self,
        calendar: Calendar,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        timezone: str,
        rrule_string: str | None = None,
    ) -> AvailableTime:
        """Create a single available time (optionally recurring)."""
        result = self.bulk_create_availability_windows(
            calendar=calendar, availability_windows=[(start_time, end_time, timezone, rrule_string)]
        )
        return next(iter(result))

    def get_available_times_expanded(
        self,
        calendar: Calendar,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[AvailableTime]:
        """Get all available times in a date range with recurring available times expanded to instances."""
        if not is_initialized_or_authenticated_calendar_service(
            cast("BaseCalendarService", self._context)
        ):
            raise

        base_qs = (
            AvailableTime.objects.annotate_recurring_occurrences_on_date_range(
                start_date, end_date, overlap=True
            )
            .select_related("recurrence_rule")
            .filter(
                organization_id=calendar.organization_id,
                calendar=calendar,
                parent_recurring_object__isnull=True,  # Master times only
            )
        )

        # Get non-recurring times overlapping the date range. Interval overlap is
        # start < range_end AND end > range_start — this also catches windows that
        # fully contain the range, which a start-or-end-inside filter would drop.
        non_recurring_times = base_qs.filter(
            start_time__lt=end_date,
            end_time__gt=start_date,
            recurrence_rule__isnull=True,  # Non-recurring only
            is_recurring_exception=False,  # Exclude exception objects
        )

        # Get recurring master times and generate their instances
        recurring_times = base_qs.filter(
            recurrence_rule__isnull=False,  # Recurring only
        ).filter(
            Q(recurrence_rule__until__isnull=True) | Q(recurrence_rule__until__gte=start_date),
            start_time__lte=end_date,
        )

        times: list[AvailableTime] = list(non_recurring_times)

        for master_time in recurring_times:
            instances = master_time.get_occurrences_in_range(
                start_date, end_date, include_self=False, include_exceptions=True, overlap=True
            )
            times.extend(instances)

        # Sort by start time
        times.sort(key=lambda x: x.start_time)
        return times

    # ------------------------------------------------------------------
    # Blocked-time writes / reads
    # ------------------------------------------------------------------

    @transaction.atomic()
    def create_blocked_time(
        self,
        calendar: Calendar,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        timezone: str,
        reason: str = "",
        rrule_string: str | None = None,
    ) -> BlockedTime:
        """Create a single blocked time (optionally recurring)."""
        result = self._host.bulk_create_manual_blocked_times(
            calendar=calendar,
            blocked_times=[(start_time, end_time, timezone, reason, rrule_string)],
        )
        return next(iter(result))

    @transaction.atomic()
    def update_blocked_time(
        self,
        calendar: Calendar,
        blocked_time_id: int,
        start_time: datetime.datetime | None = None,
        end_time: datetime.datetime | None = None,
        timezone: str | None = None,
        reason: str | None = None,
        rrule_string: str | None = None,
    ) -> BlockedTime:
        """Update an existing blocked time's fields (partial update — only provided fields change).

        :param calendar: The calendar the blocked time belongs to.
        :param blocked_time_id: The id of the blocked time to update.
        :param start_time: New start time (replaces start_time_tz_unaware), or None to leave unchanged.
        :param end_time: New end time (replaces end_time_tz_unaware), or None to leave unchanged.
        :param timezone: New timezone string, or None to leave unchanged.
        :param reason: New reason string, or None to leave unchanged.
        :param rrule_string: New recurrence rule string, or None to leave unchanged.
        :return: The updated BlockedTime instance.
        :raises ValueError: If blocked_time_id is not found in this calendar.
        """
        context = cast("BaseCalendarService", self._context)
        if not is_initialized_or_authenticated_calendar_service(context):
            raise

        scoped = BlockedTime.objects.filter_by_organization(context.organization.id).filter(
            calendar_fk=calendar
        )

        try:
            blocked_time = scoped.get(id=blocked_time_id)
        except BlockedTime.DoesNotExist as e:
            raise ValueError(f"Blocked time {blocked_time_id} not found in this calendar.") from e

        if start_time is not None:
            blocked_time.start_time_tz_unaware = start_time
        if end_time is not None:
            blocked_time.end_time_tz_unaware = end_time
        if timezone is not None:
            blocked_time.timezone = timezone
        if reason is not None:
            blocked_time.reason = reason
        if rrule_string is not None:
            blocked_time.recurrence_rule = self._host._create_recurrence_rule_if_needed(
                rrule_string
            )

        blocked_time.save()
        return blocked_time

    @transaction.atomic()
    def delete_blocked_time(
        self,
        calendar: Calendar,
        blocked_time_id: int,
    ) -> None:
        """Delete an existing blocked time (single-row delete).

        A recurring blocked time is stored as one row (with an rrule on its RecurrenceRule).
        Deleting it removes the whole recurrence series; materialized exception rows are not
        separately handled by this method. If granular per-occurrence deletion is required,
        use ``create_recurring_blocked_time_exception`` with ``is_cancelled=True``.

        :param calendar: The calendar the blocked time belongs to.
        :param blocked_time_id: The id of the blocked time to delete.
        :raises ValueError: If blocked_time_id is not found in this calendar.
        """
        context = cast("BaseCalendarService", self._context)
        if not is_initialized_or_authenticated_calendar_service(context):
            raise

        scoped = BlockedTime.objects.filter_by_organization(context.organization.id).filter(
            calendar_fk=calendar
        )

        try:
            scoped.get(id=blocked_time_id).delete()
        except BlockedTime.DoesNotExist as e:
            raise ValueError(f"Blocked time {blocked_time_id} not found in this calendar.") from e

    def get_blocked_times_expanded(
        self,
        calendar: Calendar,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
    ) -> list[BlockedTime]:
        """Get all blocked times in a date range with recurring blocked times expanded to instances."""
        if not is_initialized_or_authenticated_calendar_service(
            cast("BaseCalendarService", self._context)
        ):
            raise

        # Get calendars to query - includes the main calendar and bundle children if applicable
        calendars_to_query = [calendar]
        if calendar.calendar_type == CalendarType.BUNDLE:
            # Add all bundle children calendars
            bundle_children = calendar.bundle_children.all()
            calendars_to_query.extend(bundle_children)

        base_qs = (
            BlockedTime.objects.annotate_recurring_occurrences_on_date_range(
                start_date, end_date, overlap=True
            )
            .select_related("recurrence_rule")
            .filter(
                organization_id=calendar.organization_id,
                calendar__in=calendars_to_query,
                parent_recurring_object__isnull=True,  # Master times only
            )
        )

        # Get non-recurring times overlapping the date range. Interval overlap is
        # start < range_end AND end > range_start — this also catches blocks that
        # fully contain the range, which a start-or-end-inside filter would drop
        # (and miss a block covering the whole booking, allowing a double-booking).
        non_recurring_times = base_qs.filter(
            start_time__lt=end_date,
            end_time__gt=start_date,
            recurrence_rule__isnull=True,  # Non-recurring only
            is_recurring_exception=False,  # Exclude exception objects
        )

        # Get recurring master times and generate their instances
        recurring_times = base_qs.filter(
            recurrence_rule__isnull=False,  # Recurring only
        ).filter(
            Q(recurrence_rule__until__isnull=True) | Q(recurrence_rule__until__gte=start_date),
            start_time__lte=end_date,
        )

        times: list[BlockedTime] = list(non_recurring_times)

        for master_time in recurring_times:
            instances = master_time.get_occurrences_in_range(
                start_date, end_date, include_self=False, include_exceptions=True, overlap=True
            )
            times.extend(instances)

        # Sort by start time
        times.sort(key=lambda x: x.start_time)
        return times

    # ------------------------------------------------------------------
    # Recurring blocked-time / available-time exceptions + bulk-modifications
    # ------------------------------------------------------------------

    def create_recurring_blocked_time_exception(
        self,
        parent_blocked_time: BlockedTime,
        exception_date: datetime.date,
        modified_reason: str | None = None,
        modified_start_time: datetime.datetime | None = None,
        modified_end_time: datetime.datetime | None = None,
        modified_timezone: str | None = None,
        is_cancelled: bool = False,
    ) -> BlockedTime | None:
        """
        Create an exception for a recurring blocked time (either cancelled or modified).

        :param parent_blocked_time: The recurring blocked time to create an exception for
        :param exception_date: The date of the occurrence to modify/cancel
        :param modified_reason: New reason for the modified occurrence (if not cancelled)
        :param modified_start_time: New start time for the modified occurrence (if not cancelled)
        :param modified_end_time: New end time for the modified occurrence (if not cancelled)
        :param modified_timezone: New timezone for the modified occurrence (if not cancelled)
        :param is_cancelled: True if cancelling the occurrence, False if modifying
        :return: Created modified blocked time or None if cancelled
        """

        def create_new_recurring_blocked_time(
            parent_obj: RecurringMixin,
            second_occurrence: RecurringMixin,
            new_recurrence_rule: RecurrenceRule,
        ) -> RecurringMixin:
            parent_blocked_time = cast(BlockedTime, parent_obj)
            second_blocked_time = cast(BlockedTime, second_occurrence)
            return self.create_blocked_time(
                calendar=parent_blocked_time.calendar,
                start_time=second_blocked_time.start_time,
                end_time=second_blocked_time.end_time,
                timezone=second_blocked_time.timezone,
                reason=second_blocked_time.reason,
                rrule_string=new_recurrence_rule.to_rrule_string(),
            )

        def create_modified_blocked_time(
            parent_obj: RecurringMixin,
            exception_datetime: datetime.datetime,
            modification_data: dict[str, Any],
        ) -> RecurringMixin:
            parent_blocked_time = cast(BlockedTime, parent_obj)
            return self.create_blocked_time(
                calendar=parent_blocked_time.calendar,
                start_time=modification_data.get("start_time") or exception_datetime,
                end_time=(
                    modification_data.get("end_time")
                    or (exception_datetime + parent_blocked_time.duration)
                ),
                timezone=modification_data.get("timezone") or parent_blocked_time.timezone,
                reason=modification_data.get("reason") or parent_blocked_time.reason,
            )

        def update_exception_manager(
            parent_obj: RecurringMixin, new_recurring_obj: RecurringMixin
        ) -> None:
            BlockedTimeRecurrenceException.objects.filter(parent_blocked_time=parent_obj).update(
                parent_blocked_time_fk=new_recurring_obj
            )

        def delete_exception_manager(parent_obj: RecurringMixin) -> None:
            BlockedTimeRecurrenceException.objects.filter(parent_blocked_time=parent_obj).delete()

        modification_data = {
            "reason": modified_reason,
            "start_time": modified_start_time,
            "end_time": modified_end_time,
            "timezone": modified_timezone,
        }

        result = self._recurrence_manager.create_recurring_exception_generic(
            self._context,
            object_type_name="blocked time",
            parent_object=parent_blocked_time,
            exception_date=datetime.datetime.combine(
                exception_date,
                parent_blocked_time.start_time.time(),
                tzinfo=parent_blocked_time.start_time.tzinfo,
            ),
            is_cancelled=is_cancelled,
            modification_data=modification_data,
            create_new_recurring_callback=create_new_recurring_blocked_time,
            create_modified_object_callback=create_modified_blocked_time,
            exception_manager_update_callback=update_exception_manager,
            exception_manager_delete_callback=delete_exception_manager,
        )
        return cast(BlockedTime, result) if result else None

    def create_recurring_available_time_exception(
        self,
        parent_available_time: AvailableTime,
        exception_date: datetime.date,
        modified_start_time: datetime.datetime | None = None,
        modified_end_time: datetime.datetime | None = None,
        modified_timezone: str | None = None,
        is_cancelled: bool = False,
    ) -> AvailableTime | None:
        """
        Create an exception for a recurring available time (either cancelled or modified).

        :param parent_available_time: The recurring available time to create an exception for
        :param exception_date: The date of the occurrence to modify/cancel
        :param modified_start_time: New start time for the modified occurrence (if not cancelled)
        :param modified_end_time: New end time for the modified occurrence (if not cancelled)
        :param modified_timezone: New timezone for the modified occurrence (if not cancelled)
        :param is_cancelled: True if cancelling the occurrence, False if modifying
        :return: Created modified available time or None if cancelled
        """

        def create_new_recurring_available_time(
            parent_obj: RecurringMixin,
            second_occurrence: RecurringMixin,
            new_recurrence_rule: RecurrenceRule,
        ) -> RecurringMixin:
            parent_available_time = cast(AvailableTime, parent_obj)
            second_available_time = cast(AvailableTime, second_occurrence)
            return self.create_available_time(
                calendar=parent_available_time.calendar,
                start_time=second_available_time.start_time,
                end_time=second_available_time.end_time,
                timezone=second_available_time.timezone,
                rrule_string=new_recurrence_rule.to_rrule_string(),
            )

        def create_modified_available_time(
            parent_obj: RecurringMixin,
            exception_datetime: datetime.datetime,
            modification_data: dict[str, Any],
        ) -> RecurringMixin:
            parent_available_time = cast(AvailableTime, parent_obj)
            return self.create_available_time(
                calendar=parent_available_time.calendar,
                start_time=modification_data.get("start_time") or exception_datetime,
                end_time=(
                    modification_data.get("end_time")
                    or (exception_datetime + parent_available_time.duration)
                ),
                timezone=modification_data.get("timezone") or parent_available_time.timezone,
            )

        def update_exception_manager(
            parent_obj: RecurringMixin, new_recurring_obj: RecurringMixin
        ) -> None:
            AvailableTimeRecurrenceException.objects.filter(
                parent_available_time=parent_obj
            ).update(parent_available_time_fk=new_recurring_obj)

        def delete_exception_manager(parent_obj: RecurringMixin) -> None:
            AvailableTimeRecurrenceException.objects.filter(
                parent_available_time=parent_obj
            ).delete()

        modification_data = {
            "start_time": modified_start_time,
            "end_time": modified_end_time,
            "timezone": modified_timezone,
        }

        result = self._recurrence_manager.create_recurring_exception_generic(
            self._context,
            object_type_name="available time",
            parent_object=parent_available_time,
            exception_date=datetime.datetime.combine(
                exception_date,
                parent_available_time.start_time.time(),
                tzinfo=parent_available_time.start_time.tzinfo,
            ),
            is_cancelled=is_cancelled,
            modification_data=modification_data,
            create_new_recurring_callback=create_new_recurring_available_time,
            create_modified_object_callback=create_modified_available_time,
            exception_manager_update_callback=update_exception_manager,
            exception_manager_delete_callback=delete_exception_manager,
        )
        return cast(AvailableTime, result) if result else None

    def create_recurring_blocked_time_bulk_modification(
        self,
        parent_blocked_time: BlockedTime,
        modification_start_date: datetime.datetime,
        modified_reason: str | None = None,
        modified_start_time_offset: datetime.timedelta | None = None,
        modified_end_time_offset: datetime.timedelta | None = None,
        is_bulk_cancelled: bool = False,
        modification_rrule_string: str | None = None,
    ) -> BlockedTime | None:
        """Create a bulk modification for a recurring blocked time from the specified date onwards."""

        def truncate_parent(
            parent_obj: RecurringMixin,
            new_recurrence_rule: RecurrenceRule | None,
        ):
            parent = cast(BlockedTime, parent_obj)
            parent.recurrence_rule_fk = new_recurrence_rule  # type: ignore
            parent.save()
            return parent

        def create_continuation(
            parent_obj: RecurringMixin,
            start_dt: datetime.datetime,
            recurrence_rule: RecurrenceRule | None,
            modification_data: dict[str, Any],
        ) -> RecurringMixin:
            parent = cast(BlockedTime, parent_obj)
            new_start = (
                (start_dt + modification_data["start_time_offset"])
                if modification_data.get("start_time_offset")
                else start_dt
            )
            duration = parent.duration
            new_end = (
                new_start + modification_data["end_time_offset"]
                if modification_data.get("end_time_offset")
                else new_start + duration
            )
            return self.create_blocked_time(
                calendar=parent.calendar,
                start_time=new_start,
                end_time=new_end,
                timezone=parent.timezone,
                reason=modification_data.get("reason") or parent.reason,
                rrule_string=recurrence_rule.to_rrule_string() if recurrence_rule else None,
            )

        def record_bulk(
            parent_obj: RecurringMixin,
            start_dt: datetime.datetime,
            continuation_obj: RecurringMixin | None,
            cancelled: bool,
        ):
            BlockedTimeBulkModification.objects.create(
                organization=parent_obj.organization,
                parent_blocked_time=parent_obj,
                modification_start_date=start_dt,
                modified_continuation=None,
                is_bulk_cancelled=cancelled,
            )

        modification_data = {
            "reason": modified_reason,
            "start_time_offset": modified_start_time_offset,
            "end_time_offset": modified_end_time_offset,
        }

        result = self._recurrence_manager.create_recurring_bulk_modification_generic(
            self._context,
            object_type_name="blocked time",
            parent_object=parent_blocked_time,
            modification_start_date=modification_start_date,
            is_bulk_cancelled=is_bulk_cancelled,
            modification_data=modification_data,
            truncate_parent_callback=truncate_parent,
            create_continuation_callback=create_continuation,
            bulk_modification_record_callback=record_bulk,
            modification_rrule_string=modification_rrule_string,
        )
        return cast(BlockedTime, result) if result else None

    def create_recurring_available_time_bulk_modification(
        self,
        parent_available_time: AvailableTime,
        modification_start_date: datetime.datetime,
        modified_start_time_offset: datetime.timedelta | None = None,
        modified_end_time_offset: datetime.timedelta | None = None,
        is_bulk_cancelled: bool = False,
        modification_rrule_string: str | None = None,
    ) -> AvailableTime | None:
        """Create a bulk modification for a recurring available time from the specified date onwards."""

        def truncate_parent(
            parent_obj: RecurringMixin,
            new_recurrence_rule: RecurrenceRule | None,
        ):
            parent = cast(AvailableTime, parent_obj)
            parent.recurrence_rule_fk = new_recurrence_rule  # type: ignore
            parent.save()
            return parent

        def create_continuation(
            parent_obj: RecurringMixin,
            start_dt: datetime.datetime,
            recurrence_rule: RecurrenceRule | None,
            modification_data: dict[str, Any],
        ) -> RecurringMixin:
            parent = cast(AvailableTime, parent_obj)
            new_start = (
                (start_dt + modification_data["start_time_offset"])
                if modification_data.get("start_time_offset")
                else start_dt
            )
            duration = parent.duration
            new_end = (
                new_start + modification_data["end_time_offset"]
                if modification_data.get("end_time_offset")
                else new_start + duration
            )
            return self.create_available_time(
                calendar=parent.calendar,
                start_time=new_start,
                end_time=new_end,
                timezone=parent.timezone,
                rrule_string=recurrence_rule.to_rrule_string() if recurrence_rule else None,
            )

        def record_bulk(
            parent_obj: RecurringMixin,
            start_dt: datetime.datetime,
            continuation_obj: RecurringMixin | None,
            cancelled: bool,
        ):
            AvailableTimeBulkModification.objects.create(
                organization=parent_obj.organization,
                parent_available_time=parent_obj,
                modification_start_date=start_dt,
                modified_continuation=None,
                is_bulk_cancelled=cancelled,
            )

        modification_data = {
            "start_time_offset": modified_start_time_offset,
            "end_time_offset": modified_end_time_offset,
        }

        result = self._recurrence_manager.create_recurring_bulk_modification_generic(
            self._context,
            object_type_name="available time",
            parent_object=parent_available_time,
            modification_start_date=modification_start_date,
            is_bulk_cancelled=is_bulk_cancelled,
            modification_data=modification_data,
            truncate_parent_callback=truncate_parent,
            create_continuation_callback=create_continuation,
            bulk_modification_record_callback=record_bulk,
            modification_rrule_string=modification_rrule_string,
        )
        return cast(AvailableTime, result) if result else None

    def modify_recurring_blocked_time_from_date(
        self,
        parent_blocked_time: BlockedTime,
        modification_start_date: datetime.datetime,
        modified_reason: str | None = None,
        modified_start_time_offset: datetime.timedelta | None = None,
        modified_end_time_offset: datetime.timedelta | None = None,
        modification_rrule_string: str | None = None,
    ) -> BlockedTime | None:
        continuation = self.create_recurring_blocked_time_bulk_modification(
            parent_blocked_time=parent_blocked_time,
            modification_start_date=modification_start_date,
            modified_reason=modified_reason,
            modified_start_time_offset=modified_start_time_offset,
            modified_end_time_offset=modified_end_time_offset,
            is_bulk_cancelled=False,
            modification_rrule_string=modification_rrule_string,
        )

        return continuation

    def cancel_recurring_blocked_time_from_date(
        self,
        parent_blocked_time: BlockedTime,
        modification_start_date: datetime.datetime,
        modification_rrule_string: str | None = None,
    ) -> None:
        self.create_recurring_blocked_time_bulk_modification(
            parent_blocked_time=parent_blocked_time,
            modification_start_date=modification_start_date,
            is_bulk_cancelled=True,
            modification_rrule_string=modification_rrule_string,
        )

    def modify_recurring_available_time_from_date(
        self,
        parent_available_time: AvailableTime,
        modification_start_date: datetime.datetime,
        modified_start_time_offset: datetime.timedelta | None = None,
        modified_end_time_offset: datetime.timedelta | None = None,
        modification_rrule_string: str | None = None,
    ) -> AvailableTime | None:
        continuation = self.create_recurring_available_time_bulk_modification(
            parent_available_time=parent_available_time,
            modification_start_date=modification_start_date,
            modified_start_time_offset=modified_start_time_offset,
            modified_end_time_offset=modified_end_time_offset,
            is_bulk_cancelled=False,
            modification_rrule_string=modification_rrule_string,
        )

        return continuation

    def cancel_recurring_available_time_from_date(
        self,
        parent_available_time: AvailableTime,
        modification_start_date: datetime.datetime,
        modification_rrule_string: str | None = None,
    ) -> None:
        self.create_recurring_available_time_bulk_modification(
            parent_available_time=parent_available_time,
            modification_start_date=modification_start_date,
            is_bulk_cancelled=True,
            modification_rrule_string=modification_rrule_string,
        )
