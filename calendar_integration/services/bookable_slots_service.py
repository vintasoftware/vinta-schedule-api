"""BookableSlotsService — policy-aware single-calendar / bundle slot discovery.

This is the main read path for the booking-policy feature.  It walks a
single calendar (or a bundle calendar's children) over a search window, stepping
by ``slot_step``, and returns every ``[start, start + duration)`` window that is
free, then applies the resolved :class:`EffectivePolicy` (lead-time, max-horizon,
buffer envelope).

Design notes:

- **Personal vs bundle** is detected from ``calendar.calendar_type``.  A bundle is
  bookable for a window only when **every** ``bundle_children`` calendar is free
  (``is_primary`` gets no availability special-casing).
- **Free check** reuses :mod:`calendar_integration.services.slot_engine`'s
  management split + ``calendar_free_for_window`` so a one-calendar discovery
  yields exactly what a one-calendar group would.
- **No-policy identical-output guarantee**: when the resolved policy is
  ``EffectivePolicy.unconstrained()`` we skip ALL policy work — no managed-calendar
  blocking-span fetch, no envelope math — so the candidate set is exactly the
  pre-feature engine output.
- **Managed-calendar buffer fetch is conditional**: blocking spans for managed
  calendars are fetched only when a buffer applies (``buffer_before`` or
  ``buffer_after`` > 0).  Without a buffer the managed path is unchanged.
"""

import datetime
from typing import cast

from django.utils import timezone

from calendar_integration.constants import CalendarType
from calendar_integration.exceptions import (
    BookableSlotsValidationError,
    CalendarServiceOrganizationNotSetError,
)
from calendar_integration.models import (
    Calendar,
    ChildrenCalendarRelationship,
)
from calendar_integration.services import slot_engine
from calendar_integration.services.booking_policy_service import BookingPolicyService
from calendar_integration.services.dataclasses import (
    BookableSlotProposal,
    EffectivePolicy,
)
from organizations.models import Organization


class BookableSlotsService:
    """Discover policy-compliant bookable slots for a single calendar or bundle.

    Must be initialized with ``initialize(organization)`` before use.  All query
    paths are organization-scoped through the model managers and the
    ``slot_engine`` helpers — no raw or unscoped queries are made.
    """

    organization: Organization | None
    booking_policy_service: BookingPolicyService | None

    def __init__(self, booking_policy_service: BookingPolicyService | None = None) -> None:
        self.organization = None
        self.booking_policy_service = booking_policy_service

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self, organization: Organization) -> None:
        """Bind this service (and its booking_policy_service) to a tenant org."""
        self.organization = organization
        if self.booking_policy_service is not None:
            self.booking_policy_service.initialize(organization)

    def _assert_initialized(self) -> None:
        if self.organization is None or self.booking_policy_service is None:
            raise CalendarServiceOrganizationNotSetError(
                "BookableSlotsService requires an organization and a booking_policy_service. "
                "Call initialize()."
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_bookable_slots_for_calendar(
        self,
        calendar_id: int,
        search_window_start: datetime.datetime,
        search_window_end: datetime.datetime,
        duration: datetime.timedelta,
        slot_step: datetime.timedelta = datetime.timedelta(minutes=15),
        *,
        now: datetime.datetime | None = None,
        with_bulk_modifications: bool = False,
    ) -> list[BookableSlotProposal]:
        """Return policy-compliant ``[start, start + duration)`` windows.

        Detects bundle vs personal from ``calendar.calendar_type``; for bundles
        requires ALL ``bundle_children`` free; resolves the ``EffectivePolicy``
        through ``BookingPolicyService``; runs the engine + policy filter.

        ``now`` defaults to ``timezone.now()`` (the request time) and is used for
        the lead-time / max-horizon cutoffs.
        """
        self._assert_initialized()
        # _assert_initialized guarantees both are bound; capture narrowed locals.
        organization = cast(Organization, self.organization)
        booking_policy_service = cast(BookingPolicyService, self.booking_policy_service)

        if slot_step <= datetime.timedelta(0):
            raise BookableSlotsValidationError("slot_step must be a positive timedelta.")
        if duration <= datetime.timedelta(0):
            raise BookableSlotsValidationError("duration must be a positive timedelta.")

        if now is None:
            now = timezone.now()

        calendar = Calendar.objects.filter_by_organization(organization.id).get(id=calendar_id)

        if calendar.calendar_type == CalendarType.BUNDLE:
            target_calendar_ids = self._bundle_child_ids(organization.id, calendar)
            if not target_calendar_ids:
                # An empty bundle has no participants to satisfy → no slots.
                return []
            policy = booking_policy_service.resolve_for_bundle(calendar)
        else:
            target_calendar_ids = {calendar.id}
            policy = booking_policy_service.resolve_for_calendar(calendar)

        proposals = self._walk_candidates(
            organization.id,
            target_calendar_ids,
            search_window_start,
            search_window_end,
            duration,
            slot_step,
            with_bulk_modifications=with_bulk_modifications,
        )

        if policy == EffectivePolicy.unconstrained():
            # No policy anywhere → skip ALL policy work so the output is
            # byte-for-byte the pre-feature engine result.
            return proposals

        buffer_blocking_spans = self._buffer_blocking_spans(
            organization.id,
            policy,
            target_calendar_ids,
            search_window_start,
            search_window_end,
            with_bulk_modifications=with_bulk_modifications,
        )
        return slot_engine.apply_policy_filter(proposals, policy, now, buffer_blocking_spans)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _bundle_child_ids(self, org_id: int, bundle_calendar: Calendar) -> set[int]:
        """Return the set of child calendar ids that make up the bundle."""
        return set(
            ChildrenCalendarRelationship.objects.filter_by_organization(org_id)
            .filter(bundle_calendar_fk_id=bundle_calendar.pk)
            .values_list("child_calendar_fk_id", flat=True)
        )

    def _walk_candidates(
        self,
        org_id: int,
        target_calendar_ids: set[int],
        search_window_start: datetime.datetime,
        search_window_end: datetime.datetime,
        duration: datetime.timedelta,
        slot_step: datetime.timedelta,
        *,
        with_bulk_modifications: bool,
    ) -> list[BookableSlotProposal]:
        """Walk candidate windows; a window is offered only when EVERY target
        calendar is free for it (the bundle all-free predicate, which reduces to
        a single-calendar free check for a personal calendar)."""
        managed_ids, unmanaged_ids = slot_engine.split_calendars_by_management(
            org_id, target_calendar_ids
        )
        available_spans = slot_engine.fetch_available_spans(
            org_id, managed_ids, search_window_start, search_window_end
        )
        blocking_spans = slot_engine.fetch_blocking_spans(
            org_id,
            unmanaged_ids,
            search_window_start,
            search_window_end,
            with_bulk_modifications=with_bulk_modifications,
        )

        proposals: list[BookableSlotProposal] = []
        cursor = search_window_start
        while cursor + duration <= search_window_end:
            window_start = cursor
            window_end = cursor + duration
            all_free = all(
                slot_engine.calendar_free_for_window(
                    cid,
                    window_start,
                    window_end,
                    managed_ids,
                    available_spans,
                    blocking_spans,
                )
                for cid in target_calendar_ids
            )
            if all_free:
                proposals.append(BookableSlotProposal(start_time=window_start, end_time=window_end))
            cursor = cursor + slot_step
        return proposals

    def _buffer_blocking_spans(
        self,
        org_id: int,
        policy: EffectivePolicy,
        target_calendar_ids: set[int],
        search_window_start: datetime.datetime,
        search_window_end: datetime.datetime,
        *,
        with_bulk_modifications: bool,
    ) -> slot_engine.SpansByCalendarId:
        """Fetch blocking spans for the buffer-envelope check.

        Only fetched when a buffer applies.  Crucially, the buffer rule is defined
        against existing ``CalendarEvent`` / ``BlockedTime`` for **all** target
        calendars — managed included — so this fetches blocking spans for the full
        target set (not just unmanaged ones).  The fetch window is widened by the
        buffers so a blocking span just outside the search window can still clip a
        candidate via its dead zone.
        """
        no_buffer = policy.buffer_before <= datetime.timedelta(0) and policy.buffer_after <= (
            datetime.timedelta(0)
        )
        if no_buffer or not target_calendar_ids:
            return {}
        # Event-envelope widening: a span before the window matters when its
        # be + buffer_after reaches back to search_window_start (so widen START by
        # buffer_after); a span after the window matters when its bs - buffer_before
        # reaches forward to search_window_end (so widen END by buffer_before).
        return slot_engine.fetch_blocking_spans(
            org_id,
            target_calendar_ids,
            search_window_start - policy.buffer_after,
            search_window_end + policy.buffer_before,
            with_bulk_modifications=with_bulk_modifications,
        )
