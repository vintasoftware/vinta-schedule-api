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
- :func:`fetch_group_scoped_quota_rules` / :func:`fetch_group_scoped_quota_period_counts`
  / :func:`quota_period_start_utc` — the quota analog
  (``CALENDAR_GROUP_SCOPED_AVAILABILITY`` Phase 3b): applied in
  :func:`calendar_free_for_window` LAST, after base availability, block, and
  window all pass. The counting call (``GetCalendarGroupQuotaPeriodCountsJSON``,
  Phase 3a) is issued ONCE per ``(group_slot, period)`` combination actually
  configured, covering the WHOLE search window in one shot; each candidate
  then only does an in-memory dict lookup keyed by the period its start time
  falls into (:func:`quota_period_start_utc`, which mirrors the SQL function's
  UTC bucketing exactly) -- no query inside the per-candidate loop.

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
from typing import NamedTuple

from django.db.models import Q

from calendar_integration.constants import QuotaPeriod
from calendar_integration.database_functions import GetCalendarGroupQuotaPeriodCountsJSON
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarGroupSlotQuotaRule,
)
from calendar_integration.services.dataclasses import (
    BookableSlotProposal,
    EffectivePolicy,
)
from organizations.models import WeekStart


Span = tuple[datetime.datetime, datetime.datetime]
SpansByCalendarId = dict[int, list[Span]]
# Group-scoped AvailableTime (Phase 1b) / BlockedTime (Phase 2a) spans, keyed
# first by CalendarGroupSlot id, then by calendar id -- a window or block
# applies only within the one slot it was configured for
# (CALENDAR_GROUP_SCOPED_AVAILABILITY).
GroupScopedSpansBySlot = dict[int, SpansByCalendarId]


class GroupScopedQuotaRule(NamedTuple):
    """One ``CalendarGroupSlotQuotaRule`` row, flattened for the discovery /
    booking-validation lookup (``CALENDAR_GROUP_SCOPED_AVAILABILITY`` Phase 3b).
    A calendar may have several of these for the same ``(slot_id, calendar_id)``
    -- one per period -- and ALL of them must have headroom (spec: "at most 1 a
    day AND 3 a week" is two rules, both enforced).
    """

    slot_id: int
    calendar_id: int
    period: str
    cap: int


# Quota rules for a (slot, calendar) pair -- there can be more than one (e.g.
# a daily rule AND a weekly rule), all of which must pass.
QuotaRulesByCalendar = dict[int, list[GroupScopedQuotaRule]]
# Quota rules, keyed first by CalendarGroupSlot id, then by calendar id --
# mirrors GroupScopedSpansBySlot's shape.
GroupScopedQuotaRulesBySlot = dict[int, QuotaRulesByCalendar]
# Live-booking counts per period bucket for one (calendar, period) pair within
# a slot: {period_start (UTC) -> booking_count}. Only periods with >= 1 live
# booking appear (the SQL function only returns non-empty buckets); a period
# absent from this dict has a count of 0.
QuotaPeriodBucketCounts = dict[datetime.datetime, int]
# Counts keyed first by CalendarGroupSlot id, then by (calendar_id, period).
GroupScopedQuotaCountsBySlot = dict[int, dict[tuple[int, str], QuotaPeriodBucketCounts]]


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
    group_scoped_quota_calendar_ids: set[int] | None = None,
    group_scoped_quota_rules: QuotaRulesByCalendar | None = None,
    group_scoped_quota_counts: dict[tuple[int, str], QuotaPeriodBucketCounts] | None = None,
    week_start: str = WeekStart.MONDAY,
) -> bool:
    """Return True if ``calendar_id`` is free for ``[window_start, window_end)``.

    - Managed calendars need an ``AvailableTime`` span that fully covers the
      window.
    - Unmanaged calendars must not overlap any blocking span.

    ``group_scoped_calendar_ids`` / ``group_scoped_spans`` add the Phase 1b
    window intersection, ``group_scoped_block_calendar_ids`` /
    ``group_scoped_block_spans`` add the Phase 2a block exclusion, and
    ``group_scoped_quota_calendar_ids`` / ``group_scoped_quota_rules`` /
    ``group_scoped_quota_counts`` add the Phase 3b quota cap
    (``CALENDAR_GROUP_SCOPED_AVAILABILITY``): all default to ``None``, which
    reproduces the exact pre-Phase-1b behavior byte-for-byte -- this is the
    single-calendar / bundle walker's call shape
    (``BookableSlotsService._walk_candidates``), which passes none of them and
    must stay untouched (spec non-goal: single-calendar booking).

    Resolution order (spec "State transitions & edge cases" flowchart): base
    availability, then block, then window, then quota -- QUOTA IS CHECKED
    LAST, after everything else passes. A calendar with a configured
    group-scoped block that OVERLAPS ``[window_start, window_end)`` is
    excluded immediately -- "blocks beat everything" -- regardless of what any
    group-scoped window or quota rule says; the window and quota checks below
    never even run for it.

    When ``calendar_id`` is NOT in ``group_scoped_calendar_ids`` (or that set
    is falsy), this calendar has no group-scoped window configured for the
    slot being evaluated and the window step is a no-op (fall-through
    default). When it IS in that set, the window must additionally be fully
    covered by at least one of ``group_scoped_spans``' entries for this
    calendar -- narrowing only, never widening base availability (spec:
    "Intersect, never widen"; a calendar configured with a window that does
    not overlap ``[window_start, window_end)`` at all contributes an empty
    span list here, which correctly yields ``False``).

    Quota (Phase 3b): when ``calendar_id`` is NOT in
    ``group_scoped_quota_calendar_ids`` (or that set is falsy), the quota step
    is a no-op -- same self-gating fall-through as windows and blocks. When it
    IS in that set, EVERY rule in ``group_scoped_quota_rules[calendar_id]``
    must have headroom (spec: "at most 1 a day AND 3 a week" is two rules,
    both enforced) -- one rule at/over its cap for the period
    ``window_start`` falls into rejects the candidate. The period a candidate
    falls into is computed by :func:`quota_period_start_utc`, which mirrors
    the counting SQL function's UTC bucketing exactly so the candidate side
    and the counted side always agree on which bucket a time belongs to. No
    query happens here -- ``group_scoped_quota_counts`` was already fetched
    once for the whole search window by the caller
    (:func:`fetch_group_scoped_quota_period_counts`); this is a pure
    in-memory dict lookup.
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

    if group_scoped_calendar_ids and calendar_id in group_scoped_calendar_ids:
        if not window_fully_covered_by_spans(
            (group_scoped_spans or {}).get(calendar_id, ()), window_start, window_end
        ):
            return False

    if group_scoped_quota_calendar_ids and calendar_id in group_scoped_quota_calendar_ids:
        for rule in (group_scoped_quota_rules or {}).get(calendar_id, ()):
            period_start = quota_period_start_utc(window_start, rule.period, week_start)
            count = (
                (group_scoped_quota_counts or {})
                .get((calendar_id, rule.period), {})
                .get(period_start, 0)
            )
            if count >= rule.cap:
                return False

    return True


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


# ---------------------------------------------------------------------------
# Group-scoped quota rules (CALENDAR_GROUP_SCOPED_AVAILABILITY Phase 3b --
# discovery + booking-validation enforcement). The rule model and the
# per-period counting SQL function/wrapper were added in Phase 3a; nothing
# read them until now.
# ---------------------------------------------------------------------------


def fetch_group_scoped_quota_rules(
    organization_id: int,
    slot_ids: Iterable[int],
    calendar_ids: Iterable[int],
) -> list[GroupScopedQuotaRule]:
    """Every ``CalendarGroupSlotQuotaRule`` row for the given (slot, calendar)
    universe -- ONE query, fixed regardless of how many candidate windows the
    caller will check the result against. Callers should only invoke this
    once at least one (slot, calendar) pair is known to have a quota rule
    configured; see ``CalendarGroupService._slot_pools_with_group_scoped_flags``
    for the zero-extra-query existence check that gates this fetch.
    """
    slot_ids = list(slot_ids)
    calendar_ids = list(calendar_ids)
    if not slot_ids or not calendar_ids:
        return []
    return [
        GroupScopedQuotaRule(
            slot_id=row["group_slot_fk_id"],
            calendar_id=row["calendar_fk_id"],
            period=row["period"],
            cap=row["cap"],
        )
        for row in (
            CalendarGroupSlotQuotaRule.objects.filter_by_organization(organization_id)
            .filter(group_slot_fk_id__in=slot_ids, calendar_fk_id__in=calendar_ids)
            .values("group_slot_fk_id", "calendar_fk_id", "period", "cap")
        )
    ]


def group_quota_rules_by_slot(
    rules: Iterable[GroupScopedQuotaRule],
) -> GroupScopedQuotaRulesBySlot:
    """Reshape a flat list of :class:`GroupScopedQuotaRule` into
    ``{slot_id: {calendar_id: [rules]}}`` -- the shape
    :func:`calendar_free_for_window` consumes per slot, mirroring
    ``GroupScopedSpansBySlot``.
    """
    by_slot: GroupScopedQuotaRulesBySlot = {}
    for rule in rules:
        by_slot.setdefault(rule.slot_id, {}).setdefault(rule.calendar_id, []).append(rule)
    return by_slot


def quota_period_start_utc(
    instant: datetime.datetime, period: str, week_start: str
) -> datetime.datetime:
    """Return the UTC start of the fixed calendar period (day / week / month)
    that ``instant`` falls into -- the Python-side mirror of
    ``calculate_calendar_group_quota_period_counts``'s bucketing (Phase 3a
    SQL), so a candidate time and a counted booking with the same real instant
    always land in the SAME bucket.

    Bucketing happens in ONE consistent frame -- UTC -- regardless of
    ``instant``'s own tzinfo (naive instants are treated as already being in
    UTC, matching how ``CalendarEvent.start_time`` -- a true UTC instant -- is
    truncated ``AT TIME ZONE 'UTC'`` on the SQL side). This is the same
    documented v1 decision the counting function carries: no canonical
    calendar-level timezone exists in this schema, so per-event/per-candidate
    timezones must never be allowed to split or bypass a quota bucket.

    Week buckets honor ``week_start`` (``organizations.models.WeekStart``):
    a Monday start truncates to the ISO Monday on/before ``instant``; a Sunday
    start truncates to the Sunday on/before ``instant`` (mirrors the SQL
    function's ``date_trunc('week', ts + 1 day) - 1 day`` shift).
    """
    if instant.tzinfo is None:
        instant_utc = instant.replace(tzinfo=datetime.UTC)
    else:
        instant_utc = instant.astimezone(datetime.UTC)
    midnight = instant_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == QuotaPeriod.DAY:
        return midnight
    if period == QuotaPeriod.MONTH:
        return midnight.replace(day=1)

    # Week period: `datetime.weekday()` is Monday=0 .. Sunday=6.
    if week_start == WeekStart.SUNDAY:
        days_since_period_start = (midnight.weekday() + 1) % 7
    else:
        days_since_period_start = midnight.weekday()
    return midnight - datetime.timedelta(days=days_since_period_start)


def quota_period_end_utc(period_start: datetime.datetime, period: str) -> datetime.datetime:
    """Return the (exclusive) end of the fixed calendar period that starts at
    ``period_start`` (itself expected to already be a value returned by
    :func:`quota_period_start_utc`). Companion to that function -- together
    they give the ``[period_start, period_end)`` bounds one period bucket
    covers, used by callers that need to fetch counts for a specific booking
    time rather than a whole discovery search window (see
    :func:`quota_covering_range`).
    """
    if period == QuotaPeriod.DAY:
        return period_start + datetime.timedelta(days=1)
    if period == QuotaPeriod.WEEK:
        return period_start + datetime.timedelta(days=7)
    # Month: `period_start` always has day=1 (from `quota_period_start_utc`).
    if period_start.month == 12:
        return period_start.replace(year=period_start.year + 1, month=1)
    return period_start.replace(month=period_start.month + 1)


def quota_covering_range(
    instants: Iterable[datetime.datetime],
    periods: Iterable[str],
    week_start: str,
) -> tuple[datetime.datetime, datetime.datetime] | None:
    """Return a ``[range_start, range_end)`` that fully covers, for EVERY
    ``instant`` and EVERY ``period`` type, the whole quota-period bucket that
    instant falls into -- not just the instant itself.

    This matters for any caller whose "search window" is narrower than a
    quota period (e.g. booking/reschedule validation checks a single
    candidate ``[start, end)``, and ``check_group_availability`` checks
    discrete, possibly far-apart ranges): if the counting query's range were
    just ``[instant, instant)``-ish, an EARLIER live booking in the SAME
    period (e.g. one made at 9am when the candidate being validated is at
    2pm the same day) would fall outside the fetched range and be silently
    undercounted, letting a calendar already at its cap slip through. Widening
    to the full period boundary is what makes the count agree with what
    discovery would have shown for the same period, regardless of which
    single instant a caller happens to be asking about.

    Returns ``None`` when either iterable is empty (nothing to cover).
    """
    starts: list[datetime.datetime] = []
    ends: list[datetime.datetime] = []
    periods = list(periods)
    for instant in instants:
        for period in periods:
            period_start = quota_period_start_utc(instant, period, week_start)
            starts.append(period_start)
            ends.append(quota_period_end_utc(period_start, period))
    if not starts:
        return None
    return min(starts), max(ends)


def fetch_group_scoped_quota_period_counts(
    organization_id: int,
    rules: Iterable[GroupScopedQuotaRule],
    week_start: str,
    search_window_start: datetime.datetime,
    search_window_end: datetime.datetime,
) -> GroupScopedQuotaCountsBySlot:
    """Live-booking counts, bucketed by period, for every ``(slot, calendar,
    period)`` combination present in ``rules``, covering the WHOLE
    ``[search_window_start, search_window_end)`` range in one shot per
    combination.

    **Callers MUST pass a range that fully covers every quota-period bucket
    they intend to look up afterward** -- not just the literal candidate
    time(s) under evaluation. A range narrower than a period bucket silently
    undercounts (an earlier live booking in the same period, outside the
    passed range, is invisible to the SQL function's ``WHERE ce.start_time >=
    p_range_start AND ce.start_time < p_range_end`` filter). Discovery
    (``find_bookable_slots``) naturally satisfies this because its search
    window spans many candidates already; callers validating a single instant
    or a handful of scattered ranges (booking/reschedule validation,
    ``check_group_availability``) MUST widen first via
    :func:`quota_covering_range`.

    **Query-count discipline (the headline risk of this phase):** issues
    exactly ONE query per distinct ``(slot_id, period)`` pair present in
    ``rules`` -- NOT one per calendar and NOT one per candidate time. Calendar
    ids sharing a ``(slot_id, period)`` are folded into a single annotated
    ``Calendar`` queryset (``GetCalendarGroupQuotaPeriodCountsJSON`` evaluates
    once per matched row, inside ONE SQL round trip -- the Phase 3a function
    only accepts one calendar id as a positional argument, but that argument
    becomes a per-row column reference when annotating a multi-row queryset,
    so a whole roster shares one query). In the common case (one slot, one or
    two period types configured) this is 1-2 queries total for the entire
    discovery call, independent of how many candidate slots the caller will
    check the result against afterward -- that check is a pure in-memory dict
    lookup (see :func:`quota_period_start_utc` /
    :func:`calendar_free_for_window`).
    """
    calendar_ids_by_slot_period: dict[tuple[int, str], set[int]] = {}
    for rule in rules:
        calendar_ids_by_slot_period.setdefault((rule.slot_id, rule.period), set()).add(
            rule.calendar_id
        )

    counts_by_slot: GroupScopedQuotaCountsBySlot = {}
    for (slot_id, period), calendar_ids in calendar_ids_by_slot_period.items():
        rows = (
            Calendar.objects.filter_by_organization(organization_id)
            .filter(id__in=calendar_ids)
            .annotate(
                quota_period_counts=GetCalendarGroupQuotaPeriodCountsJSON(
                    "id",
                    slot_id,
                    organization_id,
                    period,
                    week_start,
                    search_window_start,
                    search_window_end,
                )
            )
            .values_list("id", "quota_period_counts")
        )
        for calendar_id, buckets in rows:
            bucket_counts: QuotaPeriodBucketCounts = {}
            for bucket in buckets or ():
                period_start = datetime.datetime.fromisoformat(bucket["period_start"])
                # ``bucket["period_end"]`` is intentionally unused here -- only
                # ``period_start`` (the lookup key) and ``booking_count`` are
                # needed on the Python side; callers derive any end boundary
                # they need via `quota_period_end_utc`.
                bucket_counts[period_start] = bucket["booking_count"]
            counts_by_slot.setdefault(slot_id, {})[(calendar_id, period)] = bucket_counts
    return counts_by_slot
