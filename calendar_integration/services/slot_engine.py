"""Shared, pure slot-engine primitives for bookable-slot discovery.

This module holds the reusable building blocks the calendar-group walker and the
single-calendar / bundle walker both depend on:

- :func:`intervals_overlap` — half-open overlap test.
- :func:`split_calendars_by_management` — partition calendar ids into
  managed (``manage_available_windows=True``) and unmanaged.
- :func:`fetch_available_spans` — batched ``AvailableTime`` spans for managed
  calendars.
- :func:`fetch_blocking_spans` — batched ``CalendarEvent`` + ``BlockedTime``
  spans for a set of calendars.
- :func:`calendar_free_for_window` — the per-calendar free predicate the walkers
  apply at each candidate window.
- :func:`apply_policy_filter` — drop candidate proposals that violate a resolved
  :class:`EffectivePolicy` (lead-time, max-horizon, buffer envelope).
- :func:`fetch_group_scoped_available_spans` / :func:`expand_group_scoped_available_times`
  — batched group-scoped ``AvailableTime`` windows (``CALENDAR_GROUP_SCOPED_AVAILABILITY``
  Phase 1b).
- :func:`fetch_group_scoped_blocking_spans` / :func:`expand_group_scoped_blocked_times`
  — the block analog (``CALENDAR_GROUP_SCOPED_AVAILABILITY`` Phase 2a): applied
  in :func:`calendar_free_for_window` AFTER base availability and BEFORE the
  window intersection, since a group-scoped block wins regardless of what any
  window says.

Everything here is **stateless and org-scoped through the passed organization
id**.  The functions are factored out of ``CalendarGroupService`` verbatim so the
existing group walker keeps byte-for-byte behaviour; the only addition is the
policy filter, which the group walker does not call in this phase.

Boundary semantics (decided once, applied consistently):

- **overlap** is half-open: ``[a_start, a_end)`` and ``[b_start, b_end)`` overlap
  iff ``a_start < b_end and b_start < a_end``.  Two spans that merely *touch*
  (one ends exactly where the next begins) do **not** overlap.
- **lead-time**: a candidate is kept iff ``start >= now + lead_time`` (the instant
  exactly at the lead horizon is bookable).
- **max-horizon**: a candidate is kept iff ``start <= now + max_horizon`` (the
  instant exactly at the far horizon is bookable).
- **buffer envelope (event-envelope / dead-zone-around-the-event)**: each blocking
  span ``[bs, be)`` is expanded to its dead zone ``[bs - buffer_before, be +
  buffer_after)`` and the **bare** candidate ``[start, end)`` is tested against that
  expanded zone with the same half-open overlap rule.  ``buffer_before`` protects
  the time *before* the event and ``buffer_after`` protects the time *after* it.
  Worked example: an event ``14:00-15:00`` with ``buffer_before=10m`` and
  ``buffer_after=20m`` has a dead zone of ``13:50-15:20``; a candidate overlapping
  any part of ``13:50-15:20`` is dropped, so the first post-event 30-min slot can
  only start at ``15:20`` (not ``15:10``).  Touching is still not overlap: a
  candidate ending exactly at ``13:50`` (or starting exactly at ``15:20``) is
  **allowed** (flush booking with a zero gap is permitted).
"""

import datetime
from collections.abc import Iterable, Iterator

from django.db.models import Q

from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
)
from calendar_integration.services.dataclasses import (
    BookableSlotProposal,
    EffectivePolicy,
)


Span = tuple[datetime.datetime, datetime.datetime]
SpansByCalendarId = dict[int, list[Span]]
# Group-scoped AvailableTime (Phase 1b) / BlockedTime (Phase 2a) spans, keyed
# first by CalendarGroupSlot id, then by calendar id -- a window or block
# applies only within the one slot it was configured for
# (CALENDAR_GROUP_SCOPED_AVAILABILITY).
GroupScopedSpansBySlot = dict[int, SpansByCalendarId]


def intervals_overlap(a: Span, b: Span) -> bool:
    """Return True if two half-open intervals overlap (touching is not overlap)."""
    a_start, a_end = a
    b_start, b_end = b
    return a_start < b_end and b_start < a_end


def split_calendars_by_management(
    organization_id: int, calendar_ids: set[int]
) -> tuple[set[int], set[int]]:
    """Partition ``calendar_ids`` into (managed, unmanaged) for the given org.

    Managed calendars are those with ``manage_available_windows=True`` — they are
    checked against ``AvailableTime`` coverage; unmanaged calendars are checked
    against blocking (``CalendarEvent`` / ``BlockedTime``) spans.
    """
    managed_ids: set[int] = set()
    unmanaged_ids: set[int] = set()
    for cid, managed in (
        Calendar.objects.filter_by_organization(organization_id)
        .filter(id__in=calendar_ids)
        .values_list("id", "manage_available_windows")
    ):
        if managed:
            managed_ids.add(cid)
        else:
            unmanaged_ids.add(cid)
    return managed_ids, unmanaged_ids


def fetch_available_spans(
    organization_id: int,
    managed_ids: set[int],
    search_window_start: datetime.datetime,
    search_window_end: datetime.datetime,
) -> SpansByCalendarId:
    """Batched ``AvailableTime`` spans for the managed calendars in one query."""
    spans: SpansByCalendarId = {}
    if not managed_ids:
        return spans
    for row in (
        AvailableTime.objects.filter_by_organization(organization_id)
        .filter(
            calendar_fk_id__in=managed_ids,
            start_time__lte=search_window_end,
            end_time__gte=search_window_start,
        )
        .values("calendar_fk_id", "start_time", "end_time")
    ):
        spans.setdefault(row["calendar_fk_id"], []).append((row["start_time"], row["end_time"]))
    return spans


def fetch_blocking_spans(
    organization_id: int,
    calendar_ids: set[int],
    search_window_start: datetime.datetime,
    search_window_end: datetime.datetime,
    *,
    with_bulk_modifications: bool,
) -> SpansByCalendarId:
    """Batched blocking (``CalendarEvent`` + ``BlockedTime``) spans for the calendars.

    One query per type for the whole window, then walked in Python.  Recurring
    occurrences are expanded through the queryset annotation (optionally through
    the bulk-modification continuation series).
    """
    spans: SpansByCalendarId = {}
    if not calendar_ids:
        return spans

    if with_bulk_modifications:
        events_qs = CalendarEvent.objects.filter_by_organization(
            organization_id
        ).annotate_recurring_occurrences_with_bulk_modifications_on_date_range(
            search_window_start, search_window_end
        )
    else:
        events_qs = CalendarEvent.objects.filter_by_organization(
            organization_id
        ).annotate_recurring_occurrences_on_date_range(search_window_start, search_window_end)

    overlap_filter = (
        Q(start_time__range=(search_window_start, search_window_end))
        | Q(end_time__range=(search_window_start, search_window_end))
        | Q(start_time__lte=search_window_start, end_time__gte=search_window_end)
        | Q(recurring_occurrences__len__gt=0)
    )

    for ev in events_qs.filter(overlap_filter, calendar_fk_id__in=calendar_ids).values(
        "calendar_fk_id", "start_time", "end_time", "recurring_occurrences"
    ):
        bucket = spans.setdefault(ev["calendar_fk_id"], [])
        if ev["start_time"] and ev["end_time"]:
            bucket.append((ev["start_time"], ev["end_time"]))
        for occ in ev["recurring_occurrences"] or ():
            occ_start = datetime.datetime.fromisoformat(occ["start_time"])
            occ_end = datetime.datetime.fromisoformat(occ["end_time"])
            bucket.append((occ_start, occ_end))

    for bt in (
        BlockedTime.objects.filter_by_organization(organization_id)
        .filter(
            Q(start_time__range=(search_window_start, search_window_end))
            | Q(end_time__range=(search_window_start, search_window_end))
            | Q(
                start_time__lte=search_window_start,
                end_time__gte=search_window_end,
            ),
            calendar_fk_id__in=calendar_ids,
        )
        .values("calendar_fk_id", "start_time", "end_time")
    ):
        spans.setdefault(bt["calendar_fk_id"], []).append((bt["start_time"], bt["end_time"]))
    return spans


def window_fully_covered_by_spans(
    spans: Iterable[Span],
    window_start: datetime.datetime,
    window_end: datetime.datetime,
) -> bool:
    """Return True if ``[window_start, window_end)`` is fully inside AT LEAST
    ONE of ``spans`` -- not their union. This is the "one span must cover it
    whole" rule both base ``AvailableTime`` coverage and group-scoped
    availability windows use (``CALENDAR_GROUP_SCOPED_AVAILABILITY`` spec
    resolution order: "T fully inside one of them?").
    """
    return any(sp_start <= window_start and sp_end >= window_end for sp_start, sp_end in spans)


def calendar_free_for_window(
    calendar_id: int,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
    managed_ids: set[int],
    available_spans: SpansByCalendarId,
    blocking_spans: SpansByCalendarId,
    group_scoped_calendar_ids: set[int] | None = None,
    group_scoped_spans: SpansByCalendarId | None = None,
    group_scoped_block_calendar_ids: set[int] | None = None,
    group_scoped_block_spans: SpansByCalendarId | None = None,
) -> bool:
    """Return True if ``calendar_id`` is free for ``[window_start, window_end)``.

    - Managed calendars need an ``AvailableTime`` span that fully covers the
      window.
    - Unmanaged calendars must not overlap any blocking span.

    ``group_scoped_calendar_ids`` / ``group_scoped_spans`` add the Phase 1b
    window intersection and ``group_scoped_block_calendar_ids`` /
    ``group_scoped_block_spans`` add the Phase 2a block exclusion
    (``CALENDAR_GROUP_SCOPED_AVAILABILITY``): all four default to ``None``,
    which reproduces the exact pre-Phase-1b behavior byte-for-byte -- this is
    the single-calendar / bundle walker's call shape
    (``BookableSlotsService._walk_candidates``), which passes none of them and
    must stay untouched (spec non-goal: single-calendar booking).

    Resolution order (spec "State transitions & edge cases" flowchart): base
    availability, then block, then window. A calendar with a configured
    group-scoped block that OVERLAPS ``[window_start, window_end)`` is
    excluded immediately -- "blocks beat everything" -- regardless of what any
    group-scoped window says; the window check below never even runs for it.

    When ``calendar_id`` is NOT in ``group_scoped_calendar_ids`` (or that set
    is falsy), this calendar has no group-scoped window configured for the
    slot being evaluated and the result is exactly the base-availability
    (and, if applicable, block) check above (fall-through default). When it
    IS in that set, the window must additionally be fully covered by at least
    one of ``group_scoped_spans``' entries for this calendar -- narrowing
    only, never widening base availability (spec: "Intersect, never widen"; a
    calendar configured with a window that does not overlap
    ``[window_start, window_end)`` at all contributes an empty span list
    here, which correctly yields ``False``).
    """
    if calendar_id in managed_ids:
        base_free = window_fully_covered_by_spans(
            available_spans.get(calendar_id, ()), window_start, window_end
        )
    else:
        base_free = not any(
            intervals_overlap((bs, be), (window_start, window_end))
            for bs, be in blocking_spans.get(calendar_id, ())
        )
    if not base_free:
        return False

    if group_scoped_block_calendar_ids and calendar_id in group_scoped_block_calendar_ids:
        blocked = any(
            intervals_overlap((bs, be), (window_start, window_end))
            for bs, be in (group_scoped_block_spans or {}).get(calendar_id, ())
        )
        if blocked:
            return False

    if not group_scoped_calendar_ids or calendar_id not in group_scoped_calendar_ids:
        return True

    return window_fully_covered_by_spans(
        (group_scoped_spans or {}).get(calendar_id, ()), window_start, window_end
    )


def apply_policy_filter(
    proposals: list[BookableSlotProposal],
    policy: EffectivePolicy,
    now: datetime.datetime,
    buffer_blocking_spans: SpansByCalendarId,
) -> list[BookableSlotProposal]:
    """Drop proposals that violate ``policy`` relative to ``now``.

    Three rules (see module docstring for the inclusive/exclusive boundary
    decisions):

    - **lead-time**: drop a proposal whose ``start < now + lead_time``.
    - **max-horizon**: drop a proposal whose ``start > now + max_horizon`` (only
      when ``max_horizon`` is not ``None``).
    - **buffer envelope (event-envelope)**: when a buffer applies, drop a proposal
      whose **bare** window ``[start, end)`` overlaps the dead zone of **any**
      blocking span across **all** target calendars — the dead zone being the span
      expanded to ``[bs - buffer_before, be + buffer_after)`` (``buffer_blocking_spans``
      is the union of managed + unmanaged blocking spans gathered by the caller).

    ``buffer_blocking_spans`` is consulted only when a buffer is in effect; the
    caller passes an empty mapping when no buffer applies (and should skip the
    managed-calendar blocking-span fetch entirely in that case).
    """
    lead_cutoff = now + policy.lead_time
    horizon_cutoff = (now + policy.max_horizon) if policy.max_horizon is not None else None
    has_buffer = policy.buffer_before > datetime.timedelta(0) or policy.buffer_after > (
        datetime.timedelta(0)
    )

    # Flatten the per-calendar blocking spans into a single list once; a candidate
    # is rejected if its bare window overlaps any blocking span's dead zone on ANY
    # target calendar.
    all_blocking_spans: list[Span] = []
    if has_buffer:
        for cal_spans in buffer_blocking_spans.values():
            all_blocking_spans.extend(cal_spans)

    filtered: list[BookableSlotProposal] = []
    for proposal in proposals:
        if proposal.start_time < lead_cutoff:
            continue
        if horizon_cutoff is not None and proposal.start_time > horizon_cutoff:
            continue
        if has_buffer:
            # Event-envelope: expand each blocking span to its dead zone
            # [bs - buffer_before, be + buffer_after) and test the BARE candidate.
            candidate = (proposal.start_time, proposal.end_time)
            if any(
                intervals_overlap(
                    candidate,
                    (bs - policy.buffer_before, be + policy.buffer_after),
                )
                for bs, be in all_blocking_spans
            ):
                continue
        filtered.append(proposal)
    return filtered


# ---------------------------------------------------------------------------
# Group-scoped availability windows (CALENDAR_GROUP_SCOPED_AVAILABILITY
# Phase 1b -- discovery + booking-validation intersection).
# ---------------------------------------------------------------------------


def _iter_group_scoped_available_time_occurrences(
    organization_id: int,
    slot_ids: Iterable[int],
    calendar_ids: Iterable[int],
    start_date: datetime.datetime,
    end_date: datetime.datetime,
) -> Iterator[tuple[int, int, AvailableTime]]:
    """Yield ``(group_slot_id, calendar_id, occurrence)`` for every group-scoped
    ``AvailableTime`` occurrence overlapping ``[start_date, end_date)`` across
    the given (slot, calendar) universe -- one query for non-recurring rows and
    one for recurring masters, fixed regardless of how many pairs are
    configured.

    Reads through ``AvailableTime.objects.unscoped()`` -- group-scoped rows are
    invisible to the default manager. Occurrence expansion for group-scoped
    masters is safe because (a) no write path creates a group-scoped recurrence
    exception yet, and (b) ``RecurringMixin._get_occurrences_in_range`` now
    routes the exception-instance lookup through ``_base_manager`` when the
    master is group-scoped, ensuring group-scoped exception rows are found if
    one ever becomes reachable.

    ``occurrence`` may be a persisted master/exception row or a synthetic
    in-memory instance (``AvailableTime.create_instance_from_occurrence``,
    used for recurring occurrences) -- the latter does not carry its own
    ``group_slot`` or ``organization``. Callers must attribute spans using the
    yielded ``group_slot_id`` / ``calendar_id``, not the occurrence's own
    fields.
    """
    slot_ids = list(slot_ids)
    calendar_ids = list(calendar_ids)
    if not slot_ids or not calendar_ids:
        return

    base_qs = (
        AvailableTime.objects.unscoped()
        .filter_by_organization(organization_id)
        .filter(
            group_slot_fk_id__in=slot_ids,
            calendar_fk_id__in=calendar_ids,
            parent_recurring_object__isnull=True,
        )
        .annotate_recurring_occurrences_on_date_range(start_date, end_date, overlap=True)
        .select_related("recurrence_rule")
    )

    non_recurring_times = base_qs.filter(
        start_time__lt=end_date,
        end_time__gt=start_date,
        recurrence_rule__isnull=True,
        is_recurring_exception=False,
    )
    for at in non_recurring_times:
        yield at.group_slot_fk_id, at.calendar_fk_id, at  # type: ignore[misc]

    recurring_times = base_qs.filter(recurrence_rule__isnull=False).filter(
        Q(recurrence_rule__until__isnull=True) | Q(recurrence_rule__until__gte=start_date),
        start_time__lte=end_date,
    )
    for master_time in recurring_times:
        instances = master_time.get_occurrences_in_range(
            start_date, end_date, include_self=False, include_exceptions=True, overlap=True
        )
        for instance in instances:
            yield master_time.group_slot_fk_id, master_time.calendar_fk_id, instance  # type: ignore[misc]


def expand_group_scoped_available_times(
    organization_id: int,
    slot_ids: Iterable[int],
    calendar_ids: Iterable[int],
    start_date: datetime.datetime,
    end_date: datetime.datetime,
) -> list[AvailableTime]:
    """Expand every group-scoped ``AvailableTime`` for the given (slot,
    calendar) universe that overlaps ``[start_date, end_date)``, recurrence
    included, sorted by start time.

    Shared by ``CalendarGroupService._group_scoped_available_times_expanded``
    (single-pair write-path use: orphaned-booking detection, Phase 1a) and the
    batched discovery-side fetch below -- one implementation, so the two paths
    cannot drift apart on the annotate-first exception-trap avoidance the
    write path already relies on.
    """
    times = [
        occurrence
        for _, _, occurrence in _iter_group_scoped_available_time_occurrences(
            organization_id, slot_ids, calendar_ids, start_date, end_date
        )
    ]
    times.sort(key=lambda t: t.start_time)
    return times


def fetch_group_scoped_available_spans(
    organization_id: int,
    slot_ids: Iterable[int],
    calendar_ids: Iterable[int],
    search_window_start: datetime.datetime,
    search_window_end: datetime.datetime,
) -> GroupScopedSpansBySlot:
    """Batched group-scoped ``AvailableTime`` spans for the given (slot,
    calendar) universe -- one query for non-recurring rows and one for
    recurring masters, fixed regardless of how many pairs are configured or
    how many candidate windows the caller will check the result against
    (mirrors :func:`fetch_available_spans`'s one-query-per-type batching).

    Returns spans keyed first by ``CalendarGroupSlot`` id, then by calendar id
    -- a window applies only within the one slot it was configured for.
    Callers should only invoke this once at least one (slot, calendar) pair is
    known to have a group-scoped window configured; see
    ``CalendarGroupService._slot_pools_with_group_scoped_flags`` for the
    zero-extra-query existence check that gates this fetch.
    """
    spans_by_slot: GroupScopedSpansBySlot = {}
    for slot_id, calendar_id, occurrence in _iter_group_scoped_available_time_occurrences(
        organization_id, slot_ids, calendar_ids, search_window_start, search_window_end
    ):
        spans_by_slot.setdefault(slot_id, {}).setdefault(calendar_id, []).append(
            (occurrence.start_time, occurrence.end_time)
        )
    return spans_by_slot


# ---------------------------------------------------------------------------
# Group-scoped blocked time (CALENDAR_GROUP_SCOPED_AVAILABILITY Phase 2a --
# writes, discovery, and booking-validation enforcement).
# ---------------------------------------------------------------------------


def _iter_group_scoped_blocked_time_occurrences(
    organization_id: int,
    slot_ids: Iterable[int],
    calendar_ids: Iterable[int],
    start_date: datetime.datetime,
    end_date: datetime.datetime,
) -> Iterator[tuple[int, int, BlockedTime]]:
    """Yield ``(group_slot_id, calendar_id, occurrence)`` for every group-scoped
    ``BlockedTime`` occurrence overlapping ``[start_date, end_date)`` across
    the given (slot, calendar) universe -- the block analog of
    :func:`_iter_group_scoped_available_time_occurrences`: one query for
    non-recurring rows and one for recurring masters, fixed regardless of how
    many pairs are configured.

    Reads through ``BlockedTime.objects.unscoped()`` -- group-scoped rows are
    invisible to the default manager. Occurrence expansion for group-scoped
    masters is safe for the same reason it is for windows: no write path
    creates a group-scoped ``BlockedTimeRecurrenceException`` yet, and
    ``RecurringMixin._get_occurrences_in_range`` routes the exception-instance
    lookup through ``_base_manager`` for any group-scoped master --
    ``AvailableTime`` and ``BlockedTime`` share that mixin, so the fix applies
    to both without further changes.

    ``occurrence`` may be a persisted master/exception row or a synthetic
    in-memory instance (``BlockedTime.create_instance_from_occurrence``, used
    for recurring occurrences) -- the latter does not carry its own
    ``group_slot`` or ``organization``. Callers must attribute spans using the
    yielded ``group_slot_id`` / ``calendar_id``, not the occurrence's own
    fields.
    """
    slot_ids = list(slot_ids)
    calendar_ids = list(calendar_ids)
    if not slot_ids or not calendar_ids:
        return

    base_qs = (
        BlockedTime.objects.unscoped()
        .filter_by_organization(organization_id)
        .filter(
            group_slot_fk_id__in=slot_ids,
            calendar_fk_id__in=calendar_ids,
            parent_recurring_object__isnull=True,
        )
        .annotate_recurring_occurrences_on_date_range(start_date, end_date, overlap=True)
        .select_related("recurrence_rule")
    )

    non_recurring_times = base_qs.filter(
        start_time__lt=end_date,
        end_time__gt=start_date,
        recurrence_rule__isnull=True,
        is_recurring_exception=False,
    )
    for bt in non_recurring_times:
        yield bt.group_slot_fk_id, bt.calendar_fk_id, bt  # type: ignore[misc]

    recurring_times = base_qs.filter(recurrence_rule__isnull=False).filter(
        Q(recurrence_rule__until__isnull=True) | Q(recurrence_rule__until__gte=start_date),
        start_time__lte=end_date,
    )
    for master_time in recurring_times:
        instances = master_time.get_occurrences_in_range(
            start_date, end_date, include_self=False, include_exceptions=True, overlap=True
        )
        for instance in instances:
            yield master_time.group_slot_fk_id, master_time.calendar_fk_id, instance  # type: ignore[misc]


def expand_group_scoped_blocked_times(
    organization_id: int,
    slot_ids: Iterable[int],
    calendar_ids: Iterable[int],
    start_date: datetime.datetime,
    end_date: datetime.datetime,
) -> list[BlockedTime]:
    """Expand every group-scoped ``BlockedTime`` for the given (slot,
    calendar) universe that overlaps ``[start_date, end_date)``, recurrence
    included, sorted by start time.

    Shared by ``CalendarGroupService._group_scoped_blocked_times_expanded``
    (single-pair write-path use: orphaned-booking detection, Phase 2a) and the
    batched discovery-side fetch below -- one implementation, so the two paths
    cannot drift apart on the annotate-first exception-trap avoidance the
    write path already relies on. Mirrors
    :func:`expand_group_scoped_available_times`.
    """
    times = [
        occurrence
        for _, _, occurrence in _iter_group_scoped_blocked_time_occurrences(
            organization_id, slot_ids, calendar_ids, start_date, end_date
        )
    ]
    times.sort(key=lambda t: t.start_time)
    return times


def fetch_group_scoped_blocking_spans(
    organization_id: int,
    slot_ids: Iterable[int],
    calendar_ids: Iterable[int],
    search_window_start: datetime.datetime,
    search_window_end: datetime.datetime,
) -> GroupScopedSpansBySlot:
    """Batched group-scoped ``BlockedTime`` spans for the given (slot,
    calendar) universe -- one query for non-recurring rows and one for
    recurring masters, fixed regardless of how many pairs are configured or
    how many candidate windows the caller will check the result against
    (mirrors :func:`fetch_group_scoped_available_spans`'s one-query-per-type
    batching).

    Returns spans keyed first by ``CalendarGroupSlot`` id, then by calendar id
    -- a block applies only within the one slot it was configured for.
    Callers should only invoke this once at least one (slot, calendar) pair is
    known to have a group-scoped block configured; see
    ``CalendarGroupService._slot_pools_with_group_scoped_flags`` for the
    zero-extra-query existence check that gates this fetch.
    """
    spans_by_slot: GroupScopedSpansBySlot = {}
    for slot_id, calendar_id, occurrence in _iter_group_scoped_blocked_time_occurrences(
        organization_id, slot_ids, calendar_ids, search_window_start, search_window_end
    ):
        spans_by_slot.setdefault(slot_id, {}).setdefault(calendar_id, []).append(
            (occurrence.start_time, occurrence.end_time)
        )
    return spans_by_slot
