"""Cycle close: settle a billing period's accrued overage and roll it forward.

The last and highest-stakes piece of the billing plan — **money leaves the
building here.** A double-charge or an unexplained drift is exactly the
high-severity failure the spec rates worst, because it is silent: no exception,
no red test, just a wrong number on an invoice a customer has to dispute.

Five properties carry that weight:

1. **One derivation of "overage owed", shared with reconciliation.** The amount
   charged is ``MeteredOccurrence.objects.for_billing_period(...).overage_total()``
   — ``sum(unit_price) where not is_within_allowance`` over the stamped rows — and
   ``reconcile_period`` recomputes its identity set from the *same*
   ``for_billing_period`` rows. What is charged and what is audited can never be
   two different numbers. The allowance boundary is **not** recomputed at close
   time; it was stamped at meter time on purpose, so a later limit change cannot
   retroactively reprice a closed period.

2. **One period boundary, the meter's own.** The closing period is the stored
   ``current_period_start``/``current_period_end`` — exactly the values
   ``resolve_billing_period`` returns for any occurrence inside the current cycle,
   which is what the meter stamped ``billing_period_start`` with. Close never
   derives a second period; a second derivation would repeat the recurring
   two-period-derivation defect, here charging real money.

3. **Idempotent on ``(subscription, period_start)``.** The overage charge carries
   an idempotency key derived from the subscription and the closing period start,
   forwarded to the provider, so a crash between charge and period-roll cannot
   double-charge on retry — the provider itself refuses the
   second charge. The **durable marker** that a period is already closed is the
   rolled ``current_period_start`` itself: once rolled, ``current_period_end`` is in
   the future, so the sweep's ``current_period_end <= now`` guard makes a re-run a
   no-op. No period-close record model is needed. Concurrent sweeps serialise on a
   ``SELECT ... FOR UPDATE`` of the subscription row.

4. **Real-money overage is not activated yet.** Every organization is on
   ``unlimited`` (NULL ``event_occurrences`` limit) for the whole rollout, so the
   overage sum is always zero and no charge is ever issued today. That is
   deliberate: the recurrence pk-aliasing defect can inflate the metered
   occurrence count, and turning an inflated count into money before that upstream
   calendar bug is fixed would bill for occurrences that never happened.
   ``_charge_overage`` therefore short-circuits on a NULL/unlimited limit — close
   still rolls the period and reconciles, but charges nothing. **Do not activate
   this before the recurrence fix ships.**

5. **Best-effort across subscriptions.** One subscription's close failing (a
   declined charge, a provider error) must not abort the sweep for the rest — the
   beat task fans out one Celery task per subscription, each catching and logging
   its own failure (``payments.tasks.close_subscription_billing_period``).

6. **Persisting the closed-period statement can never undo a committed charge or
   block the roll.** ``_close_one_period`` writes a durable ``BillingPeriodSummary``
   (+ its ``BillingPeriodResourceUsage`` children) after ``reconcile_period`` and
   ``_charge_overage`` and before ``_roll_period`` — the only point at which the
   plan in force, the prepaid counts, and the outcome of the charge are all
   simultaneously in hand for the period being closed, not the one it is about to
   roll into. That write runs inside its own nested ``transaction.atomic()``
   (a savepoint) and any failure there is logged and swallowed rather than
   propagated, so a bug in statement-writing can degrade history, never revenue.
   See ``_persist_statement``.
"""

import datetime
import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from payments.billing_constants import BillingState, LimitedResource
from payments.exceptions import IllegalBillingStateTransitionError
from payments.models import (
    BillingPeriodResourceUsage,
    BillingPeriodSummary,
    MeteredOccurrence,
    Payment,
    Subscription,
)
from payments.services.billing_dataclasses import ClosedPeriod, ReconciliationReport
from payments.services.billing_state_machine import transition_billing_state
from payments.services.entitlement_service import EntitlementService
from payments.services.metering_service import MeteringService
from payments.services.payment_service import PaymentService
from payments.services.subscription_service import SubscriptionService, overage_settlement_step


logger = logging.getLogger(__name__)


#: Ceiling on how many elapsed periods one ``close_subscription`` call will settle
#: in a single run before yielding. A subscription whose sweep never ran for months
#: is caught up one period at a time; the bound stops a corrupt period pair (an
#: end at or before its start, which the roll would never advance past) from
#: spinning the task forever. 24 months is two years of missed monthly closes — far
#: past any real outage, and the leftover is picked up on the next beat tick.
MAX_CLOSE_PERIODS_PER_RUN = 24


def overage_idempotency_key(subscription: Subscription, period_start: datetime.datetime) -> str:
    """The overage charge's idempotency key, derived from ``(subscription,
    period_start)``.

    **The single most important line here.** It is forwarded to the provider's own
    idempotency header (via ``PaymentService.create_payment`` ->
    ``BasePaymentAdapter.process``), so two
    attempts to close the *same* period — a Celery redelivery, or a retry after a
    crash between the charge and the period-roll — resolve to **one** charge at the
    provider even when the local ``Payment`` row from the first attempt was rolled
    back. Derived from ``period_start`` (not "now" and not a fresh uuid) precisely
    so a re-run of the same closing period produces a byte-identical key; a per-run
    key would double-charge on every retry.
    """
    return f"overage-{subscription.pk}-{period_start.isoformat()}"


class CycleCloseService:
    """Closes elapsed billing periods: settles accrued overage, rolls the period
    forward, and applies the period-boundary actions deferred to close time.

    Stateless; injected via ``di_core.containers``. Runs from a Celery task, never a
    request, so it opens its own ``transaction.atomic`` blocks (there is no
    ``ATOMIC_REQUESTS`` wrapper around a task).
    """

    def __init__(
        self,
        metering_service: MeteringService,
        subscription_service: SubscriptionService,
        payment_service: PaymentService,
        entitlement_service: EntitlementService,
    ) -> None:
        self._metering_service = metering_service
        self._subscription_service = subscription_service
        self._payment_service = payment_service
        self._entitlement_service = entitlement_service

    # ------------------------------------------------------------------
    # Sweep selection
    # ------------------------------------------------------------------

    @staticmethod
    def subscriptions_to_close(now: datetime.datetime | None = None) -> list[int]:
        """Ids of the subscriptions whose current period has ended and should close.

        The intersection of ``MeteringService.subscriptions_to_sweep()`` — the
        billing-root subscriptions the meter also sweeps, so "which subscription
        owns this usage" has one definition, not a second copy of
        ``billing_root_filter`` — with ``current_period_end <= now``. A demoted root
        (re-parented under a reseller) is excluded for the same reason the meter
        excludes it: its usage now pools against an ancestor, and closing it would
        settle the ancestor's subtree a second time under the wrong subscription.
        """
        now = now or timezone.now()
        root_ids = MeteringService.subscriptions_to_sweep()
        return list(
            Subscription.objects.filter(pk__in=root_ids, current_period_end__lte=now).values_list(
                "pk", flat=True
            )
        )

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def close_subscription(
        self, subscription: Subscription, now: datetime.datetime | None = None
    ) -> list[ClosedPeriod]:
        """Close every elapsed billing period for ``subscription`` and apply the
        period-boundary actions that land here.

        Serialised under ``SELECT ... FOR UPDATE`` on the subscription row: a
        concurrent sweep blocks until this one commits, then re-reads the rolled
        period and finds nothing left to close — so a charge happens at most once
        even under concurrency, without relying on the provider dedup (which is the
        *crash*-safety net, a different failure mode).

        Idempotent: a second call after a completed close is a no-op, because the
        rolled ``current_period_start`` moved ``current_period_end`` into the future
        and the ``while`` guard below exits immediately.

        Returns one ``ClosedPeriod`` per period actually rolled — empty when the
        current period has not ended yet (nothing to do).
        """
        now = now or timezone.now()
        closed: list[ClosedPeriod] = []
        with transaction.atomic():
            locked = Subscription.objects.select_for_update().get(pk=subscription.pk)
            steps = 0
            while locked.current_period_end <= now and steps < MAX_CLOSE_PERIODS_PER_RUN:
                closed.append(self._close_one_period(locked))
                steps += 1
            if closed:
                self._apply_pending_plan_change_if_due(locked, now)
                self._apply_cancelled_to_free_if_due(locked)
        return closed

    def _close_one_period(self, subscription: Subscription) -> ClosedPeriod:
        """Settle and roll exactly one period. Runs inside ``close_subscription``'s
        locked transaction.

        Order is deliberate: reconcile (read-only) and charge happen against the
        period being closed *before* the roll mutates the stored period, so
        ``reconcile_period`` and the meter's ``billing_period_start`` stamp resolve
        to the same bounds. The roll is last, so if the charge raises the whole
        transaction unwinds and the period is retried (unrolled) on the next run —
        with the same idempotency key, so the provider does not double-charge.
        """
        period_start = subscription.current_period_start
        period_end = subscription.current_period_end

        # Reconcile the closing period against a recomputation of the calendar. Run
        # before the roll so `resolve_billing_period(subscription, period_start)`
        # returns the still-current stored bounds — the same bounds the meter
        # stamped. Read-only; it reports drift, it does not repair.
        report = self._metering_service.reconcile_period(subscription, period_start)
        self._log_reconciliation(subscription, report)

        overage_total, payment = self._charge_overage(subscription, period_start)

        # Ordered after the charge and before the roll: `subscription.plan` is
        # still the outgoing plan (`_apply_pending_plan_change_if_due` only runs
        # once, after every period in this pass has closed — see
        # `close_subscription`) and `subscription.current_period_start` still
        # names the period this statement is about. See `_persist_statement`.
        self._persist_statement(
            subscription, period_start, period_end, overage_total, payment, report
        )

        self._roll_period(subscription, period_end)

        return ClosedPeriod(
            subscription_id=subscription.pk,
            billing_period_start=period_start,
            billing_period_end=period_end,
            overage_total=overage_total,
            charged=payment is not None,
            payment_id=payment.pk if payment is not None else None,
            reconciliation=report,
        )

    def _charge_overage(
        self, subscription: Subscription, period_start: datetime.datetime
    ) -> tuple[Decimal, Payment | None]:
        """Charge the accrued overage for the period starting at ``period_start``.

        Returns ``(overage_total, payment_or_None)``.

        **Real-money charge — do not activate before the recurrence fix ships.**
        If the effective ``event_occurrences`` limit is NULL (unlimited), this
        charges nothing and returns immediately. Every organization is on
        ``unlimited`` for the whole rollout, so this is the branch taken today: the
        machinery is exercised and reconciled, but no money moves. Turning the
        metered count into a charge before the upstream recurrence pk-aliasing
        defect (an open-ended series can duplicate indefinitely, inflating the
        count) is fixed would bill for occurrences that never happened. Activation
        waits for that fix.

        The stamped ``is_within_allowance`` columns make this doubly safe: under an
        unlimited plan the meter stamps every occurrence as within-allowance at zero
        price, so ``overage_total`` is zero even if this NULL short-circuit were
        removed. Both belt and braces are intentional.
        """
        effective_limit = self._entitlement_service.get_effective_limit(
            subscription.organization, LimitedResource.EVENT_OCCURRENCES
        )
        if effective_limit.limit_value is None:
            logger.debug(
                "Cycle close: subscription %s has an unlimited event_occurrences allowance; "
                "rolling the period and reconciling but charging no overage (real-money overage "
                "is not activated yet).",
                subscription.pk,
            )
            return Decimal("0"), None

        overage_total = MeteredOccurrence.objects.for_billing_period(
            subscription.pk, period_start
        ).overage_total()
        if overage_total <= 0:
            return overage_total, None

        idempotency_key = overage_idempotency_key(subscription, period_start)
        payment = self._payment_service.create_payment(
            organization=subscription.organization,
            currency=subscription.plan.currency,
            amount=overage_total,
            description=(
                f"Event occurrence overage for period starting {period_start.isoformat()}"
            ),
            payment_method="overage",
            payment_token="",
            idempotency_key=idempotency_key,
        )
        logger.info(
            "Cycle close: charged subscription %s overage of %s for period starting %s "
            "(idempotency key %s, payment %s).",
            subscription.pk,
            overage_total,
            period_start.isoformat(),
            idempotency_key,
            payment.pk,
        )
        return overage_total, payment

    # ------------------------------------------------------------------
    # Statement persistence
    # ------------------------------------------------------------------

    def _persist_statement(
        self,
        subscription: Subscription,
        period_start: datetime.datetime,
        period_end: datetime.datetime,
        overage_total: Decimal,
        payment: Payment | None,
        report: ReconciliationReport,
    ) -> None:
        """Write the durable ``BillingPeriodSummary`` (+ one ``BillingPeriodResourceUsage``
        per ``LimitedResource`` member) for the period just settled.

        **Ordering is the whole point of where this is called from**
        (``_close_one_period``, after ``reconcile_period``/``_charge_overage`` and
        before ``_roll_period``):

        - ``EntitlementService.get_usage_breakdown`` snapshots each **prepaid**
          resource's count as of *right now*, so this must run before the roll
          purely for the plan-snapshot reason below — those seven resources have
          no period of their own (see ``BillingPeriodResourceUsage``'s "Detail =
          post-paid ledger only" decision), so "now" is the only meaningful
          instant to record them at, and it is also the earliest instant at which
          the charge, the plan, and the reconciliation report for *this* period
          are all simultaneously in hand.
        - ``event_occurrences`` — the one **postpaid**, period-scoped resource —
          is deliberately **not** read through ``get_usage_breakdown``:
          ``EntitlementService``'s counter for it answers for the cycle in
          progress *right now* (``current_billing_period_start``, derived from
          ``timezone.now()`` — built for live enforcement, not history), which by
          the time cycle close runs is never the period being closed (that
          mismatch is *why* the period needs closing). Its breakdown is grouped
          straight off ``MeteredOccurrence.objects.for_billing_period(subscription.pk,
          period_start)`` instead — the same rows ``_charge_overage`` and
          ``reconcile_period`` already read from, so this is a fourth reader of
          the one definition of "this period's occurrences", not a second one.
        - The plan snapshot (``plan_slug``/``plan_name``/``billing_interval``/
          ``currency``) is read from ``subscription.plan``/``subscription.billing_interval``
          here, which is still the *outgoing* plan: ``_apply_pending_plan_change_if_due``
          (the deferred flip onto a scheduled downgrade) is only called once, in
          ``close_subscription``, after every period in the current catch-up pass has
          already closed — never from inside this method's caller. A pending change
          effective at this exact boundary therefore cannot leak onto the period it
          did not apply to.

        ``subscription.organization`` is used directly as the billing root rather
        than re-resolving it through ``resolve_billing_root``: every ``Subscription``
        belongs to a billing root by construction
        (``SubscriptionService.create_subscription_for_organization`` refuses to
        create one for a reseller child) and ``subscriptions_to_close`` only ever
        selects from ``MeteringService.subscriptions_to_sweep()``'s root ids — so
        that walk would terminate on its first step here regardless. The pooled
        organization ids are resolved **once** for the whole set
        (``get_pooled_organization_ids``), not per resource — used directly for
        the ``event_occurrences`` grouping above, and implicitly re-derived (from
        the same already-root organization, so still a no-query walk) inside each
        ``get_usage_breakdown``/``get_effective_limit`` call for the other seven.

        **Idempotency.** ``get_or_create`` on ``(subscription, billing_period_start)``
        — the same durability pattern ``MeteredOccurrence`` and
        ``ProviderWebhookEvent`` use elsewhere in this app: the unique constraint is
        the correctness mechanism, not the code path. In practice
        ``_close_one_period`` is never invoked twice for the same period (the
        ``current_period_end <= now`` sweep guard in ``close_subscription`` already
        prevents that once a period has rolled), so the ``created is False`` branch
        below is belt-and-braces rather than the primary defense — exactly like the
        two models above.

        **Failure isolation.** The whole write is wrapped in its own
        ``transaction.atomic()`` — a *savepoint*, since ``close_subscription``
        already holds the outer transaction open — and any exception raised inside
        is logged at ``ERROR`` and swallowed rather than re-raised. The overage
        charge has already happened by the time this runs; a bug here must degrade
        history, never roll back money that already moved or stop the period from
        rolling. A missing statement is recoverable by re-running close, which the
        unique constraint makes safe.

        This method's failure-isolation behavior is exercised under **test**
        settings only. ``ATOMIC_REQUESTS`` itself does not gate this specific path
        — ``close_subscription`` opens its own explicit ``transaction.atomic()``
        regardless of that setting, because it runs from a Celery task, never a
        request — but the covering test still only proves Django's nested-atomic /
        savepoint rollback semantics under pytest-django's own transaction
        wrapping, not against a live worker process under production settings. Do
        not read that test as independent proof of production charge/commit
        ordering; see the project's ``ATOMIC_REQUESTS`` trap note.
        """
        try:
            with transaction.atomic():
                summary, created = BillingPeriodSummary.objects.get_or_create(
                    subscription=subscription,
                    billing_period_start=period_start,
                    defaults={
                        "organization": subscription.organization,
                        "billing_period_end": period_end,
                        "plan_slug": subscription.plan.slug,
                        "plan_name": subscription.plan.name,
                        "billing_interval": subscription.billing_interval,
                        "currency": subscription.plan.currency,
                        "overage_total": overage_total,
                        "charged": payment is not None,
                        # `payment_id` (the raw FK column), not `payment` (the
                        # forward descriptor): the latter validates its assigned
                        # value is a `Payment` *instance*, which would reject the
                        # duck-typed payment stand-in ``_payment_service`` may
                        # legitimately return (see ``ClosedPeriod.payment_id``,
                        # which reads the same `.pk` for the identical reason).
                        "payment_id": payment.pk if payment is not None else None,
                        "reconciliation_unmetered": len(report.unmetered),
                        "reconciliation_orphaned": len(report.orphaned),
                        "closed_at": timezone.now(),
                    },
                )
                if not created:
                    logger.info(
                        "Cycle close: statement for subscription %s period %s already exists "
                        "(id=%s); re-run is a no-op.",
                        subscription.pk,
                        period_start.isoformat(),
                        summary.pk,
                    )
                    return

                pooled_organization_ids = self._entitlement_service.get_pooled_organization_ids(
                    subscription.organization
                )
                resource_rows: list[BillingPeriodResourceUsage] = []
                for resource_key in LimitedResource.values:
                    effective_limit = self._entitlement_service.get_effective_limit(
                        subscription.organization, resource_key
                    )
                    if resource_key == LimitedResource.EVENT_OCCURRENCES:
                        usage_breakdown = {
                            row["organization_id"]: row["usage_count"]
                            for row in (
                                MeteredOccurrence.objects.for_billing_period(
                                    subscription.pk, period_start
                                )
                                .for_organizations(pooled_organization_ids)
                                .order_by()
                                .values("organization_id")
                                .annotate(usage_count=Count("pk"))
                            )
                        }
                    else:
                        usage_breakdown = self._entitlement_service.get_usage_breakdown(
                            subscription.organization, resource_key
                        )
                    resource_rows.append(
                        BillingPeriodResourceUsage(
                            summary=summary,
                            resource_key=resource_key,
                            kind=effective_limit.kind,
                            total=sum(usage_breakdown.values()),
                            limit_value=effective_limit.limit_value,
                            overage_unit_price=effective_limit.overage_unit_price,
                            by_organization=usage_breakdown,
                        )
                    )
                BillingPeriodResourceUsage.objects.bulk_create(resource_rows)
        except Exception:
            logger.exception(
                "Cycle close: failed to persist the billing period statement for "
                "subscription %s period %s. The overage charge already committed and the "
                "period will still roll; this failure is swallowed so it cannot undo "
                "either. History for this period is lost unless close is re-run (safe: the "
                "(subscription, billing_period_start) unique constraint makes a retry a "
                "no-op).",
                subscription.pk,
                period_start.isoformat(),
            )
            return

        logger.info(
            "Cycle close: wrote billing period statement for subscription %s period %s "
            "(overage_total=%s, drift=%s).",
            subscription.pk,
            period_start.isoformat(),
            overage_total,
            report.drift,
        )

    @staticmethod
    def _roll_period(subscription: Subscription, period_end: datetime.datetime) -> None:
        """Advance the stored period one **month** forward from ``period_end``.

        Monthly regardless of ``billing_interval`` (``overage_settlement_step`` — the
        spec's "overage settles monthly even for annually-billed plans"). Rolling the
        period is what **resets the postpaid counter**: the ``event_occurrences``
        usage counter and the approaching-limit debounce are both scoped to
        ``current_billing_period_start``, so advancing the anchor starts the next
        period at zero without deleting any ``MeteredOccurrence`` rows (they are
        retained for reconciliation and archival). The new
        ``current_period_start``/``current_period_end`` are the durable marker that
        this period is closed — a re-run's ``current_period_end <= now`` guard sees
        the advanced value and does nothing.
        """
        subscription.current_period_start = period_end
        subscription.current_period_end = period_end + overage_settlement_step()
        subscription.save(update_fields=["current_period_start", "current_period_end", "modified"])

    # ------------------------------------------------------------------
    # Deferred period-boundary actions
    # ------------------------------------------------------------------

    def _apply_pending_plan_change_if_due(
        self, subscription: Subscription, now: datetime.datetime
    ) -> None:
        """Apply a scheduled downgrade whose effective moment has passed (the
        deferred flip).

        ``SubscriptionService._schedule_downgrade`` stamped ``pending_plan`` /
        ``pending_plan_effective_at`` (the old ``current_period_end``) and applied
        the lower plan's limits immediately, but left ``subscription.plan`` on the
        still-paid higher plan until this boundary sweep. Here the flip finally
        happens: the plan and billing interval move to the pending values and the
        pending markers clear. ``change_plan`` re-syncs the limits — already synced
        to the pending plan at schedule time, so this is idempotent — keeping the
        limit re-copy in its one place rather than duplicating it here.

        ``billing_state`` is deliberately left untouched: a downgrade-originated
        grace episode's resolution is ``DunningService``'s job (it inspects the
        window on every ``process_dunning`` tick), and the downgrade-grace/
        billing-state interaction still needs product sign-off. That is inert today
        — no organization can voluntarily downgrade while every plan is
        ``unlimited`` — so this flip cannot fire against real data yet.
        """
        pending_plan = subscription.pending_plan
        if (
            pending_plan is None
            or subscription.pending_plan_effective_at is None
            or subscription.pending_plan_effective_at > now
        ):
            return

        pending_interval = subscription.pending_billing_interval
        if pending_interval:
            subscription.billing_interval = pending_interval
        subscription.pending_plan = None
        subscription.pending_billing_interval = ""
        subscription.pending_plan_effective_at = None
        subscription.save(
            update_fields=[
                "billing_interval",
                "pending_plan",
                "pending_billing_interval",
                "pending_plan_effective_at",
                "modified",
            ]
        )
        # `change_plan` sets `subscription.plan` and re-copies the (already-synced)
        # limits/entitlements through the single method both plan-change paths use.
        self._subscription_service.change_plan(subscription, pending_plan)
        logger.info(
            "Cycle close: applied scheduled downgrade for subscription %s onto plan %s.",
            subscription.pk,
            pending_plan.slug,
        )

    def _apply_cancelled_to_free_if_due(self, subscription: Subscription) -> None:
        """Move a ``CANCELLED`` subscription that has run out its paid cycle to
        ``FREE`` (the deferred period-close transition).

        A cancellation takes effect at the end of the paid cycle, not immediately —
        ``SubscriptionService.cancel_subscription`` moves the subscription to
        ``CANCELLED`` but leaves it running until this boundary sweep, which is
        reached only after at least one period has closed in this pass (the caller
        guards on ``closed`` being non-empty). Routed through
        ``transition_billing_state`` like every other ``billing_state`` write, so
        the ``CANCELLED -> FREE`` edge is validated against the one transition table.
        """
        if subscription.billing_state != BillingState.CANCELLED:
            return
        try:
            transition_billing_state(subscription, BillingState.FREE)
        except IllegalBillingStateTransitionError:
            logger.warning(
                "Cycle close: subscription %s is CANCELLED but the CANCELLED -> FREE edge was "
                "rejected; leaving billing_state unchanged.",
                subscription.pk,
            )
            return
        logger.info(
            "Cycle close: subscription %s reverted CANCELLED -> FREE at period end.",
            subscription.pk,
        )

    # ------------------------------------------------------------------
    # Reconciliation reporting
    # ------------------------------------------------------------------

    @staticmethod
    def _log_reconciliation(subscription: Subscription, report: ReconciliationReport) -> None:
        """Report the drift ``reconcile_period`` can see, and never present a clean
        report as proof the invoice is correct.

        ``reconcile_period`` compares occurrence *identity* (which occurrences the
        calendar still expands to vs. which were metered). It reports the
        already-metered ``orphaned`` case it *can* see, but it has a **known blind
        spot**: in the modify-then-sweep-once case it recomputes from the same
        inflated calendar the meter read, so it reports the period
        clean while the metered count is over-billed. A clean report therefore means
        "the metered set matches the calendar's current expansion", not "this
        invoice is correct" — pricing (``is_within_allowance`` / ``unit_price``) is
        out of reconciliation's scope entirely.
        """
        if report.is_clean:
            logger.info(
                "Cycle close: subscription %s period %s reconciled clean (metered=%s). NOTE: a "
                "clean reconcile audits occurrence identity only, not pricing, and is blind to "
                "the modify-then-sweep-once over-count; it is not proof the invoice is "
                "correct.",
                subscription.pk,
                report.billing_period_start.isoformat(),
                report.metered_count,
            )
            return
        logger.warning(
            "Cycle close: subscription %s period %s reconciled with drift=%s "
            "(unmetered=%s, orphaned=%s). Escalate — reconciliation reports, it does not repair.",
            subscription.pk,
            report.billing_period_start.isoformat(),
            report.drift,
            len(report.unmetered),
            len(report.orphaned),
        )
