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


def calendar_free_for_window(
    calendar_id: int,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
    managed_ids: set[int],
    available_spans: SpansByCalendarId,
    blocking_spans: SpansByCalendarId,
) -> bool:
    """Return True if ``calendar_id`` is free for ``[window_start, window_end)``.

    - Managed calendars need an ``AvailableTime`` span that fully covers the
      window.
    - Unmanaged calendars must not overlap any blocking span.
    """
    if calendar_id in managed_ids:
        return any(
            av_start <= window_start and av_end >= window_end
            for av_start, av_end in available_spans.get(calendar_id, ())
        )
    return not any(
        intervals_overlap((bs, be), (window_start, window_end))
        for bs, be in blocking_spans.get(calendar_id, ())
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
