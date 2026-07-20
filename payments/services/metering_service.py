"""Records event occurrences as billable usage, exactly once, ever.

This is the highest-severity code in the billing plan: occurrences of a recurring
series are computed in Postgres and never stored, so nothing exists to bill until
this service writes it — and a double-count here is silent revenue drift or an
overcharge, invisible until a customer disputes an invoice. There is no exception,
no failing test, no alert; just a wrong number.

Four properties carry that weight, in order of importance:

1. **The unique constraint is the mechanism.**
   ``MeteredOccurrence(organization, event_id, occurrence_start)`` plus
   ``bulk_create(..., ignore_conflicts=True)`` is what makes re-running a window,
   or running two overlapping windows, a no-op at the database level. The sweep
   window deliberately overlaps the previous one so a missed run self-heals;
   that is only safe because idempotence is enforced below the application, not
   remembered by it.
2. **The window bounds the expansion.** An open-ended weekly series is infinite;
   it contributes roughly four occurrences per monthly cycle because the meter
   only ever expands ``[window_start, window_end)`` and only keeps occurrences
   whose ``start_time`` falls inside it. Nothing is charged at series-creation
   time.
3. **Identity comes from the expansion, not from a second enumeration.**
   ``CalendarEventQuerySet.occurrence_bearing_masters_in_range`` is the one
   definition of which rows can yield an occurrence; a recurrence exception is
   reached only through its master (which returns the exception row itself, with
   its own pk), and a bulk-modification continuation is reached only as a master
   in its own right, its parent having been truncated. Neither is enumerated
   twice, and neither needs a special case here.
4. **Price is stamped at meter time.** ``is_within_allowance`` and ``unit_price``
   are resolved against the effective limit in force when the occurrence is
   recorded, so a later plan change or limit override cannot retroactively
   reprice usage that already happened.

``reconcile_period`` is the mitigation for the residual risk in all of the above:
it recomputes a closed cycle and reports drift both ways. It never writes.
"""

import datetime
import logging
from collections.abc import Iterable, Sequence
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from calendar_integration.models import CalendarEvent
from payments.billing_constants import LimitedResource
from payments.models import MeteredOccurrence, Subscription
from payments.services.billing_dataclasses import (
    EffectiveLimit,
    MeteringResult,
    OccurrenceIdentity,
    ReconciliationReport,
)
from payments.services.entitlement_service import EntitlementService
from payments.services.subscription_service import resolve_billing_period


logger = logging.getLogger(__name__)


#: Occurrences to expand per master per window. A window is hours wide, so this is
#: unreachable in practice for any sane series; it exists so a pathological rule
#: (``FREQ=SECONDLY``) cannot make one sweep allocate without bound.
MAX_OCCURRENCES_PER_MASTER = 10000

#: How many bulk-modification splits deep a series chain is followed before the walk
#: gives up. Each level is one query; a series split a hundred times is already
#: pathological, and the bound is what stops a cycle in mutable ``parent`` data from
#: hanging the sweep.
MAX_SERIES_CHAIN_DEPTH = 100

#: Price recorded for an occurrence that fell inside the included allowance, and
#: for one outside it when the plan carries no ``overage_unit_price``. Explicitly
#: zero rather than NULL: the column records what this occurrence was priced at,
#: and "nothing" is a price.
ZERO_PRICE = Decimal("0")


def _identity_sort_key(identity: OccurrenceIdentity) -> tuple[datetime.datetime, int, int]:
    """Stable, chronological ordering for the drift lists in a reconciliation report,
    so two runs over the same data produce byte-identical output."""
    return (identity.occurrence_start, identity.organization_id, identity.event_id)


class MeteringService:
    """Writes and audits ``MeteredOccurrence`` rows. Stateless; injected via DI."""

    def __init__(self, entitlement_service: EntitlementService) -> None:
        self._entitlement_service = entitlement_service

    # ------------------------------------------------------------------
    # Metering
    # ------------------------------------------------------------------

    def meter_occurrences_for_period(
        self,
        subscription: Subscription,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> MeteringResult:
        """Record every occurrence starting in ``[window_start, window_end)``.

        Safe to call repeatedly with the same window, and safe to call with a
        window overlapping one already swept — both are how a missed run heals.

        The whole call runs inside one transaction holding the billing root's
        subscription row lock (``EntitlementService.lock_billing_root``). The lock
        is not there for the inserts — the unique constraint already makes those
        idempotent — it is there for the **allowance stamping**, which reads how
        much of the included allowance the period has already consumed and then
        writes rows that depend on that count. Two concurrent sweeps without the
        lock would each read the same "0 used" and each stamp the first N
        occurrences as within-allowance, giving away the allowance twice.

        :param window_start: Inclusive lower bound on ``occurrence_start``.
        :param window_end: Exclusive upper bound. Callers sweeping live usage pass
            a value at or before "now"; passing a future bound meters occurrences
            that have not happened yet.
        """
        if window_end <= window_start:
            logger.warning(
                "Refusing to meter subscription %s: window end %s is not after window start %s.",
                subscription.pk,
                window_end,
                window_start,
            )
            return MeteringResult(
                subscription_id=subscription.pk,
                window_start=window_start,
                window_end=window_end,
                occurrences_seen=0,
                occurrences_recorded=0,
            )

        with transaction.atomic():
            self._entitlement_service.lock_billing_root(subscription.organization)
            identities = self.expand_occurrence_identities(subscription, window_start, window_end)
            recorded = self._record(subscription, identities)

        return MeteringResult(
            subscription_id=subscription.pk,
            window_start=window_start,
            window_end=window_end,
            occurrences_seen=len(identities),
            occurrences_recorded=recorded,
        )

    def _record(self, subscription: Subscription, identities: Sequence[OccurrenceIdentity]) -> int:
        """Insert the identities that are not already recorded; return rows gained.

        Already-recorded identities are filtered out *before* the allowance is
        assigned. That is not a substitute for the unique constraint — the
        ``ignore_conflicts=True`` below is still what guarantees idempotence, and
        it still fires on a genuine race. It is needed because the allowance is
        positional: without it, an occurrence recorded by an earlier overlapping
        window would consume an allowance slot here *as well*, pushing genuinely
        new occurrences into overage that should have been included.

        The row count is measured before and after rather than taken from
        ``bulk_create``'s return value, which cannot report which rows conflicted.
        """
        if not identities:
            return 0

        effective_limit = self._entitlement_service.get_effective_limit(
            subscription.organization, LimitedResource.EVENT_OCCURRENCES
        )
        already_recorded = self._existing_identities(subscription, identities)
        new_identities = sorted(
            (identity for identity in identities if identity not in already_recorded),
            key=lambda identity: (identity.occurrence_start, identity.event_id),
        )
        if not new_identities:
            return 0

        rows: list[MeteredOccurrence] = []
        # Allowance is consumed chronologically and per billing period, so a window
        # that straddles a cycle boundary starts the next cycle's allowance fresh.
        consumed_by_period: dict[datetime.datetime, int] = {}
        for identity in new_identities:
            period_start, _period_end = resolve_billing_period(
                subscription, identity.occurrence_start
            )
            if period_start not in consumed_by_period:
                consumed_by_period[period_start] = self._recorded_count_for_period(
                    subscription, period_start
                )
            position = consumed_by_period[period_start]
            consumed_by_period[period_start] = position + 1
            is_within_allowance, unit_price = self._price_for(effective_limit, position)
            rows.append(
                MeteredOccurrence(
                    organization_id=identity.organization_id,
                    subscription=subscription,
                    event_id=identity.event_id,
                    occurrence_start=identity.occurrence_start,
                    billing_period_start=period_start,
                    is_within_allowance=is_within_allowance,
                    unit_price=unit_price,
                )
            )

        before = self._recorded_total(subscription, consumed_by_period)
        MeteredOccurrence.objects.bulk_create(rows, ignore_conflicts=True)
        after = self._recorded_total(subscription, consumed_by_period)
        return after - before

    @staticmethod
    def _price_for(effective_limit: EffectiveLimit, position: int) -> tuple[bool, Decimal]:
        """Stamp allowance membership and price for the ``position``-th occurrence
        of a billing period (zero-based).

        ``limit_value is None`` is unlimited — the whole rollout runs there, since
        every organization sits on the ``unlimited`` plan — and everything is
        inside the allowance at no cost.
        """
        if effective_limit.limit_value is None or position < effective_limit.limit_value:
            return True, ZERO_PRICE
        return False, effective_limit.overage_unit_price or ZERO_PRICE

    @staticmethod
    def _recorded_count_for_period(
        subscription: Subscription, period_start: datetime.datetime
    ) -> int:
        return MeteredOccurrence.objects.for_billing_period(subscription.pk, period_start).count()

    @classmethod
    def _recorded_total(
        cls, subscription: Subscription, period_starts: Iterable[datetime.datetime]
    ) -> int:
        return sum(
            cls._recorded_count_for_period(subscription, period_start)
            for period_start in period_starts
        )

    @staticmethod
    def _existing_identities(
        subscription: Subscription, identities: Sequence[OccurrenceIdentity]
    ) -> set[OccurrenceIdentity]:
        """Which of ``identities`` the ledger already holds.

        Queried by the unique-constraint tuple itself so this cannot disagree with
        what an insert would conflict on. Scoped by ``occurrence_start`` range and
        the pooled organization ids rather than by an ``OR`` over every tuple, so
        the query stays one indexable predicate regardless of window size.
        """
        organization_ids = {identity.organization_id for identity in identities}
        starts = [identity.occurrence_start for identity in identities]
        existing = MeteredOccurrence.objects.for_organizations(sorted(organization_ids)).filter(
            Q(subscription_id=subscription.pk),
            occurrence_start__gte=min(starts),
            occurrence_start__lte=max(starts),
        )
        return {
            OccurrenceIdentity(
                organization_id=organization_id,
                event_id=event_id,
                occurrence_start=occurrence_start,
            )
            for organization_id, event_id, occurrence_start in existing.values_list(
                "organization_id", "event_id", "occurrence_start"
            )
        }

    # ------------------------------------------------------------------
    # Expansion
    # ------------------------------------------------------------------

    def expand_occurrence_identities(
        self,
        subscription: Subscription,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> list[OccurrenceIdentity]:
        """Every billable occurrence starting in ``[window_start, window_end)`` for
        the whole pooled subtree ``subscription`` pays for.

        The one place an occurrence's billing identity is derived. ``reconcile_period``
        calls it too, so reconciliation compares the meter against the same expression
        the meter used rather than against a second opinion.

        **An occurrence is identified by its series root and the recurrence slot it
        occupies**, not by whichever row happens to represent it right now. Both
        halves of that matter, and each is the fix for a real double-bill:

        - *The slot, not the row's own time.* Editing one occurrence of a series
          writes a separate ``CalendarEvent``. Identifying the occurrence by that
          row's pk would make an already-metered occurrence look brand new the next
          time the window is swept, and the ledger's unique constraint could not
          catch it because the two rows genuinely differ.
        - *The series root, not the row that generated it.* A bulk modification
          truncates the parent series and moves the remaining occurrences onto a
          continuation event with a new pk. Without normalising to the root, applying
          one to a stretch of time that has already been metered would re-bill every
          occurrence after the split point.

        The result is deduplicated on the identity tuple. That is belt-and-braces —
        the enumeration is designed not to produce a duplicate — but the alternative
        is relying on ``ON CONFLICT DO NOTHING`` to absorb duplicates *within a single
        statement*, and this way ``occurrences_seen`` is a count of distinct
        occurrences rather than of expansion outputs.
        """
        organization_ids = self._entitlement_service.get_pooled_organization_ids(
            subscription.organization
        )
        masters = list(
            CalendarEvent.objects.occurrence_bearing_masters_in_range(
                window_start, window_end
            ).filter(organization_id__in=organization_ids)
        )
        series_root_ids = self._resolve_series_root_ids(masters, organization_ids)

        identities: dict[OccurrenceIdentity, None] = {}
        for master in masters:
            series_root_id = series_root_ids[master.pk]
            for slot_start in self._occurrence_slots_of(master, window_start, window_end):
                if not window_start <= slot_start < window_end:
                    continue
                identities[
                    OccurrenceIdentity(
                        organization_id=master.organization_id,
                        event_id=series_root_id,
                        occurrence_start=slot_start,
                    )
                ] = None
        return list(identities)

    @staticmethod
    def _occurrence_slots_of(
        master: CalendarEvent, window_start: datetime.datetime, window_end: datetime.datetime
    ) -> list[datetime.datetime]:
        """The recurrence slots one master contributes to the window.

        Expands through ``get_occurrence_slots_in_range``, which is
        ``get_occurrences_in_range`` with each occurrence's originating slot kept
        rather than discarded — the same single expansion, not a second one.

        Deliberately **not**
        ``get_occurrences_in_range_with_bulk_modifications``: that follows
        ``bulk_modifications`` from a truncated parent into its continuation, and the
        continuation is already enumerated as a master in its own right by
        ``occurrence_bearing_masters_in_range``. Using it here would visit every
        post-split occurrence twice.

        A one-off event occupies exactly one slot: its own start time.
        """
        if not master.is_recurring:
            return [master.start_time]
        return [
            slot_start
            for slot_start, _instance in master.get_occurrence_slots_in_range(
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

        A bulk modification splits a series: the parent keeps the occurrences before
        the split and a *continuation* event carries the rest, linked back by
        ``bulk_modification_parent``. Billing has to treat the whole chain as one
        series, otherwise splitting an already-metered stretch of time re-bills
        everything after the split point under the continuation's new pk.

        Walks the chain level by level (one query per level, not one per event),
        bounded by ``MAX_SERIES_CHAIN_DEPTH`` and guarded by a ``seen`` set, because
        ``bulk_modification_parent`` is ordinary mutable data and a cycle would
        otherwise loop forever. Hitting either guard falls back to the deepest
        ancestor reached, which over-counts at worst and never loses a record.
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
            for pk, parent_id in CalendarEvent.objects.filter(
                organization_id__in=organization_ids, pk__in=unknown
            ).values_list("pk", "bulk_modification_parent_fk_id"):
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

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def reconcile_period(
        self, subscription: Subscription, period: datetime.datetime
    ) -> ReconciliationReport:
        """Recompute a billing cycle and report drift against what was metered.

        ``period`` is any moment inside the cycle of interest; the cycle's exact
        bounds come from ``resolve_billing_period``, the same function the meter
        stamped ``billing_period_start`` with.

        Read-only by design. A repair that ran automatically would hide the
        condition it was repairing, and the two drift directions do not have the
        same remedy: ``unmetered`` rows are usage that was never billed (re-run
        the sweep), while ``orphaned`` rows may be perfectly correct — an event
        deleted after its occurrences happened leaves rows behind on purpose,
        because an occurrence that was billed stays billed.
        """
        period_start, period_end = resolve_billing_period(subscription, period)
        expected = set(self.expand_occurrence_identities(subscription, period_start, period_end))
        metered = {
            OccurrenceIdentity(
                organization_id=organization_id,
                event_id=event_id,
                occurrence_start=occurrence_start,
            )
            for organization_id, event_id, occurrence_start in MeteredOccurrence.objects.for_billing_period(
                subscription.pk, period_start
            ).values_list("organization_id", "event_id", "occurrence_start")
        }
        return ReconciliationReport(
            subscription_id=subscription.pk,
            billing_period_start=period_start,
            billing_period_end=period_end,
            expected_count=len(expected),
            metered_count=len(metered),
            unmetered=tuple(sorted(expected - metered, key=_identity_sort_key)),
            orphaned=tuple(sorted(metered - expected, key=_identity_sort_key)),
        )

    # ------------------------------------------------------------------
    # Sweep helpers
    # ------------------------------------------------------------------

    @staticmethod
    def subscriptions_to_sweep() -> Iterable[int]:
        """Ids of every subscription the periodic sweep should meter.

        Every ``Subscription`` row: a subscription exists only on a billing root
        (``SubscriptionService.create_subscription_for_organization`` skips
        reseller children), and ``expand_occurrence_identities`` pools the root's
        whole subtree, so iterating subscriptions covers every organization
        exactly once.
        """
        return Subscription.objects.values_list("pk", flat=True).order_by("pk")
