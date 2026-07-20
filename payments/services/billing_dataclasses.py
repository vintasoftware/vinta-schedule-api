"""Plain-data return types for the billing/entitlement services.

Kept separate from ``payments/services/dataclasses.py`` (which models the
*payment gateway* wire shapes) because these describe this app's own
limits/entitlements domain and are consumed by non-payments callers — the
organization, calendar, webhooks, and public-API services that ask "may I create
one more of these?".

Deferred from this phase: ``UsageSnapshot``. The plan's data-model section names it
as the periodic materialization of ``EntitlementService.get_current_usage`` (so
dashboards and invoices do not each re-run the subtree aggregate), but nothing
reads a snapshot yet — every consumer in Phases 5-6 wants a point-in-time count,
and a stale snapshot behind a *guard* would be an enforcement bug rather than an
optimization. It belongs with the phase that introduces the first reader.
"""

import datetime
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class EffectiveLimit:
    """The ceiling that actually applies to one resource for one billing root.

    ``limit_value`` is the subscription's ``SubscriptionPlanLimit.limit_value``
    plus the quantity of every active ``SubscriptionAddOn`` for the same resource.
    **``None`` means unlimited, never zero** — a resource with no
    ``SubscriptionPlanLimit`` row at all also resolves to ``None``, so a missing
    seed row cannot lock an organization out of a resource it used to be able to
    create (the fail-open rule from the plan's Phase 5 body).

    Deliberately carries no ``current_usage`` field, unlike the sketch in the
    plan's *Type plumbing* section: usage is a subtree-wide aggregate over several
    tables, and folding it in here would make every cheap ceiling lookup pay for a
    count it does not need. ``EntitlementService.get_current_usage`` returns it
    separately, and ``check_limit`` pairs the two.
    """

    resource_key: str
    limit_value: int | None
    kind: str | None
    overage_unit_price: Decimal | None

    @property
    def is_unlimited(self) -> bool:
        return self.limit_value is None


@dataclass(frozen=True)
class LimitCheckResult:
    """Outcome of ``EntitlementService.check_limit``.

    ``ceiling is None`` means unlimited, in which case ``allowed`` is always
    ``True``. ``remedy`` is populated only when ``allowed`` is ``False``; it is one
    of ``LimitRemedy`` and is what the over-limit error body surfaces to clients.

    ``current_usage is None`` means **usage was not counted**, which happens on
    exactly one path: an unlimited ceiling, where the answer cannot depend on it
    and counting would make every guarded create on the ``unlimited`` plan (i.e.
    every organization, for the whole rollout) pay for several queries nobody
    reads. It is ``None`` rather than ``0`` so a caller cannot mistake "not
    measured" for "measured, and it was zero". It is always an ``int`` on the
    branch that matters — ``allowed is False`` — which is the only branch the
    over-limit error body is built from.
    """

    allowed: bool
    resource_key: str
    current_usage: int | None
    ceiling: int | None
    remedy: str | None = None


@dataclass(frozen=True)
class OccurrenceIdentity:
    """The billing identity of a single event occurrence.

    Exactly the fields of ``MeteredOccurrence``'s unique constraint, and nothing
    else — this dataclass *is* the key, so "what the meter writes" and "what
    reconciliation recomputes" cannot drift into two different notions of identity.

    ``event_id`` is the pk of the **series root** — the original master, following
    ``bulk_modification_parent`` back through any splits — so splitting a series
    does not re-bill its tail. ``occurrence_start`` is the occurrence's **current
    start time**; there is no durable "slot" behind it, so re-timing an occurrence
    produces a different identity and a second billable row. That asymmetry is a
    known, deferred defect rather than a design property — see
    ``MeteringService.expand_occurrence_identities``.

    Times are always the timezone-aware ``start_time`` generated column, never the
    ``*_tz_unaware`` field, which is not comparable across timezones.
    """

    organization_id: int
    event_id: int
    occurrence_start: datetime.datetime


@dataclass(frozen=True)
class MeteringResult:
    """What one call to ``MeteringService.meter_occurrences_for_period`` did.

    ``occurrences_seen`` counts occurrences the window expanded to;
    ``occurrences_recorded`` counts rows the database actually gained. On a
    re-run of an already-metered window the first is unchanged and the second is
    **zero** — that difference is the observable proof that the sweep is
    idempotent, and it is measured by counting rows before and after rather than
    by trusting ``bulk_create``'s return value (which cannot report conflicts).
    """

    subscription_id: int
    window_start: datetime.datetime
    window_end: datetime.datetime
    occurrences_seen: int
    occurrences_recorded: int


@dataclass(frozen=True)
class ReconciliationReport:
    """Drift between what a closed period *should* have metered and what it did.

    The plan's named mitigation for this feature's highest-severity risk: metering
    depends on a scheduled task, occurrences are computed rather than stored, and a
    miscount is invisible until a customer disputes a bill. Nothing here writes —
    reconciliation reports, it does not repair, so a surprising number is escalated
    rather than silently absorbed.

    ``unmetered`` are occurrences the period still expands to but that were never
    recorded (under-billing — a sweep that never ran). ``orphaned`` are recorded
    rows the period no longer expands to (over-billing *or* a legitimately deleted
    event; ``MeteredOccurrence`` deliberately outlives its event, so a non-zero
    ``orphaned`` is not automatically a defect).
    """

    subscription_id: int
    billing_period_start: datetime.datetime
    billing_period_end: datetime.datetime
    expected_count: int
    metered_count: int
    unmetered: tuple[OccurrenceIdentity, ...]
    orphaned: tuple[OccurrenceIdentity, ...]

    @property
    def drift(self) -> int:
        """Total number of occurrences the two sides disagree about; 0 == clean."""
        return len(self.unmetered) + len(self.orphaned)

    @property
    def is_clean(self) -> bool:
        return self.drift == 0
