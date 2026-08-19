"""``OccurrenceSource`` over ``calendar_integration.CalendarEvent``.

``vinta_billing.metering.MeteringService`` bills whatever this source reports
in a window; it does not know a calendar event exists. This is that source's
project-side half, lifted from ``MeteringService.expand_occurrence_identities``
/ ``occurrence_starts_of`` / ``_resolve_series_root_ids`` -- the identity rules
those methods carried (series root, not the row that generated it; current
start time, not durable) are unchanged, only where they run from moved.

``describe()`` is the other half: batched per-page detail for the usage
ledger endpoint (``GET /billing/usage/occurrences/``), lifted from
``MeteredOccurrenceViewSet._resolve_events`` in ``payments/billing_views.py``.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Sequence
from typing import Any

from django.db.models import Prefetch

from vinta_billing.metering import Occurrence

from calendar_integration.models import CalendarEvent, CalendarOwnership


#: Occurrences to expand per master per window. A window is hours wide, so this
#: is unreachable in practice for any sane series; it exists so a pathological
#: rule (``FREQ=SECONDLY``) cannot make one sweep allocate without bound.
MAX_OCCURRENCES_PER_MASTER = 10000

#: How many bulk-modification splits deep a series chain is followed before the
#: walk gives up. Each level is one query; a series split a hundred times is
#: already pathological, and the bound is what stops a cycle in mutable
#: ``bulk_modification_parent`` data from hanging the sweep.
MAX_SERIES_CHAIN_DEPTH = 100


class CalendarEventOccurrenceSource:
    """What "a billable occurrence happened" means for this project: one
    ``CalendarEvent`` start, in or out of a recurring series."""

    def iter_occurrences(
        self,
        organization_ids: Sequence[int],
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> Iterable[Occurrence]:
        """Every billable occurrence starting in ``[window_start, window_end)``
        for the pooled subtree ``organization_ids`` names.

        **An occurrence is identified by its series root and its current start
        time** -- ``(series root pk, occurrence start)``. Exactly one half of
        that is durable, and being precise about which is the difference
        between a bound on the residual risk and a guess:

        - *The series root, not the row that generated it.* A bulk
          modification moves later occurrences onto a continuation event with
          a new pk. ``_resolve_series_root_ids`` normalises back to the
          original master, so a split does **not** re-bill the tail under the
          continuation's pk.
        - *The current start time, which is not durable.* Re-timing an
          occurrence creates a **new identity** and is billed again --
          ``calculate_recurring_events`` emits a modified exception as
          ``me.start_time`` (the moved row's own time), never
          ``re.exception_date``, so no slot distinct from ``start_time`` is
          available to key on anywhere in the expansion. This is a known,
          deferred defect; see ``payments/tests/test_metering_reconciliation.py``
          for its measured magnitude.

        ``vinta_billing.services.metering_service.MeteringService`` re-checks
        ``window_start <= occurred_at < window_end`` and drops anything
        outside the pool itself, so this method does not have to be airtight
        about either -- but it filters both anyway, matching the source this
        was lifted from, rather than relying on the caller's second check.
        """
        organization_ids = list(organization_ids)
        masters = list(
            # ``unscoped()``: this reads a subscription's *whole* reseller
            # subtree (``organization_ids``, resolved from the billing root by
            # ``EntitlementService`` before this source is ever called), which
            # no single-organization binding can express. The tenant boundary
            # is ``organization_ids`` itself, applied on the next line.
            CalendarEvent.objects.unscoped()
            .occurrence_bearing_masters_in_range(window_start, window_end)
            .filter(organization_id__in=organization_ids)
        )
        series_root_ids = self._resolve_series_root_ids(masters, organization_ids)

        seen: set[tuple[int, int, datetime.datetime]] = set()
        for master in masters:
            series_root_id = series_root_ids[master.pk]
            for occurrence_start in self._occurrence_starts_of(master, window_start, window_end):
                if not window_start <= occurrence_start < window_end:
                    continue
                identity = (master.organization_id, series_root_id, occurrence_start)
                if identity in seen:
                    continue
                seen.add(identity)
                yield Occurrence(
                    external_id=series_root_id,
                    organization_id=master.organization_id,
                    occurred_at=occurrence_start,
                )

    @staticmethod
    def _occurrence_starts_of(
        master: CalendarEvent, window_start: datetime.datetime, window_end: datetime.datetime
    ) -> list[datetime.datetime]:
        """The occurrence start times one master contributes to the window.

        Reads ``start_time`` off ``get_occurrences_in_range`` -- the ordinary
        calendar expansion, with no billing-specific variant. Deliberately
        **not** ``get_occurrences_in_range_with_bulk_modifications``: that
        follows ``bulk_modifications`` from a truncated parent into its
        continuation, and the continuation is already enumerated as a master in
        its own right by ``occurrence_bearing_masters_in_range``. Using it here
        would visit every post-split occurrence twice.

        A one-off event contributes exactly one start: its own.
        """
        if not master.is_recurring:
            return [master.start_time]
        return [
            occurrence.start_time
            for occurrence in master.get_occurrences_in_range(
                start_date=window_start,
                end_date=window_end,
                include_self=True,
                include_exceptions=True,
                max_occurrences=MAX_OCCURRENCES_PER_MASTER,
            )
        ]

    @staticmethod
    def _resolve_series_root_ids(
        masters: Sequence[CalendarEvent], organization_ids: Sequence[int]
    ) -> dict[int, int]:
        """Map each master's pk to the pk of the series it ultimately belongs to.

        A bulk modification splits a series: the parent keeps the occurrences
        before the split and a *continuation* event carries the rest, linked
        back by ``bulk_modification_parent``. Billing has to treat the whole
        chain as one series, otherwise splitting an already-metered stretch of
        time re-bills everything after the split point under the
        continuation's new pk.

        Walks the chain level by level (one query per level, not one per
        event), bounded by ``MAX_SERIES_CHAIN_DEPTH`` and guarded by a
        ``seen`` set, because ``bulk_modification_parent`` is ordinary
        mutable data and a cycle would otherwise loop forever. Hitting either
        guard falls back to the deepest ancestor reached, which over-counts at
        worst and never loses a record.
        """
        parent_of: dict[int, int | None] = {
            master.pk: master.bulk_modification_parent_fk_id for master in masters
        }
        for _depth in range(MAX_SERIES_CHAIN_DEPTH):
            unknown = {
                parent_id
                for parent_id in parent_of.values()
                if parent_id is not None and parent_id not in parent_of
            }
            if not unknown:
                break
            for pk, parent_id in (
                # ``unscoped()``: see ``iter_occurrences``.
                CalendarEvent.objects.unscoped()
                .filter(organization_id__in=organization_ids, pk__in=unknown)
                .values_list("pk", "bulk_modification_parent_fk_id")
            ):
                parent_of[pk] = parent_id
            # A parent that is not visible in the pooled subtree (deleted, or in
            # another tenant) terminates the walk rather than looping.
            for parent_id in unknown - parent_of.keys():
                parent_of[parent_id] = None

        roots: dict[int, int] = {}
        for master in masters:
            current = master.pk
            seen = {current}
            while (parent_id := parent_of.get(current)) is not None and parent_id not in seen:
                current = parent_id
                seen.add(current)
            roots[master.pk] = current
        return roots

    def describe(self, external_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        """Batched detail for the usage ledger endpoint, keyed by series-root pk.

        One query for the events (with their calendars, via
        ``select_related``) and one for the calendars' ownerships (with each
        owner's membership/user joined in that same query, via a
        ``Prefetch`` queryset) -- never a per-row lookup. Ids the project can
        no longer resolve (the underlying event was deleted) are simply
        absent from the returned mapping, and the ledger renders ``event:
        null`` for those rows -- an expected state, not an error, since a
        ``MeteredOccurrence`` outlives its event by design.

        Deliberately not organization-scoped here: ``external_ids`` only ever
        contains ids the caller already resolved from organization-scoped
        ``MeteredOccurrence`` rows (see ``MeteredOccurrenceViewSet.list``), so
        re-filtering by organization here would be a second, redundant tenant
        check rather than a real one.
        """
        external_ids = list(external_ids)
        if not external_ids:
            return {}

        events = (
            # `unscoped()`, no `organization_id__in`: the `OccurrenceSource` protocol
            # this method implements carries no organization parameter to bind one
            # from. `external_ids` arrives already scoped -- every caller resolves it
            # from an organization-scoped `MeteredOccurrence` query first, so the ids
            # themselves are the tenant boundary here. Do not copy this pattern into a
            # context that does have organization ids available; bind them there.
            CalendarEvent.objects.unscoped()
            .filter(pk__in=external_ids)
            .select_related("calendar")
            .prefetch_related(
                Prefetch(
                    "calendar__ownerships",
                    # `membership__user__profile`: `User.get_full_name()` reads
                    # `self.profile`, so this joins it in too rather than
                    # triggering a per-owner query below.
                    #
                    # `unscoped()` here for the same reason as the `CalendarEvent`
                    # query above: these ownerships are prefetched off events already
                    # narrowed to `external_ids`, so they inherit that scoping rather
                    # than needing their own `organization_id__in`.
                    queryset=CalendarOwnership.objects.unscoped().select_related(
                        "membership__user__profile"
                    ),
                )
            )
        )

        detail: dict[int, dict[str, Any]] = {}
        for event in events:
            calendar = event.calendar
            owners = (
                [
                    {
                        "user_id": ownership.membership_user_id,
                        "name": ownership.membership.user.get_full_name(),
                    }
                    for ownership in calendar.ownerships.all()
                    if ownership.membership_user_id is not None
                ]
                if calendar is not None
                else []
            )
            detail[event.pk] = {
                "title": event.title,
                "calendar": (
                    {"id": calendar.pk, "name": calendar.name} if calendar is not None else None
                ),
                "owners": owners,
            }
        return detail
