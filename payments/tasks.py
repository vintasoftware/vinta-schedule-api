"""Scheduled billing work.

``meter_event_occurrences`` is the only thing that turns computed calendar
occurrences into billable rows, so the correctness of post-paid billing rests on
it running — and on it being harmless when it runs twice.

``process_dunning`` is the beat entry point for the grace/dunning state
machine: it fans out one tick per subscription currently GRACE or RESTRICTED to
``DunningService.process_subscription`` — the single dispatch point that also
backs the webhook handlers in ``payments/views.py`` — so the transitions this
task can drive and the transitions the webhooks can drive are the same set,
defined once (``payments.services.billing_state_machine``).

``check_approaching_limits`` is the beat entry point for the proactive
usage-warning half of "an organization can see where it stands, and is warned
before it is blocked" — it fans out one tick per subscription (excluding
``RESTRICTED``/``CANCELLED``, see ``UsageWarningService.check_subscription``)
to ``UsageWarningService.check_subscription``, which is where "approaching a
limit" is actually defined.
"""

import datetime
import logging
from typing import Annotated

from django.utils import timezone

from dependency_injector.wiring import Provide, inject

from payments.billing_constants import BillingState
from payments.models import Subscription
from payments.services.cycle_close_service import CycleCloseService
from payments.services.dunning_service import DunningService
from payments.services.metering_service import MeteringService
from payments.services.usage_warning_service import UsageWarningService
from vinta_schedule_api.celery import app


logger = logging.getLogger(__name__)


#: How far back each sweep re-reads. Deliberately **wider than the beat interval**
#: (see ``CELERYBEAT_SCHEDULE``'s ``meter_event_occurrences`` entry, every 15
#: minutes), so consecutive runs overlap heavily: at six hours, up to 23
#: consecutive missed runs — a worker outage, a redeploy, a broker incident — are
#: made up for by the next successful run with no operator action and no backfill
#: command. Re-reading an already-metered stretch costs one expansion query and
#: inserts nothing, because ``MeteredOccurrence``'s unique constraint absorbs it.
#:
#: Widening this is cheap and safe; narrowing it below the beat interval would
#: leave gaps that are silently never billed.
#:
#: **Operator action after an outage longer than this.** Self-healing stops at six
#: hours; beyond that the un-swept stretch is never billed, and nothing raises,
#: because the next sweep only ever looks six hours back. There is no backfill
#: management command. Re-meter the gap by calling
#: ``MeteringService.meter_occurrences_for_period(subscription, gap_start, gap_end)``
#: for each subscription in ``MeteringService.subscriptions_to_sweep()`` — it is
#: idempotent, so an over-wide window is safe — then confirm with
#: ``reconcile_period``, which reports the recovered stretch as ``unmetered``
#: before the backfill and clean after it.
METERING_SWEEP_WINDOW = datetime.timedelta(hours=6)


@app.task
def meter_event_occurrences() -> None:
    """Beat entry point: fan out a metering sweep for every subscription.

    The window is computed **once here** and passed explicitly to each
    per-subscription task, rather than being recomputed inside them. A task that
    derived its own window from ``timezone.now()`` would sweep a different stretch
    on every ``CELERY_TASK_ACKS_LATE`` redelivery, so a retry would not be a repeat
    of the same work — which is exactly the property that makes redelivery safe.
    """
    window_end = timezone.now()
    window_start = window_end - METERING_SWEEP_WINDOW
    for subscription_id in MeteringService.subscriptions_to_sweep():
        meter_subscription_event_occurrences.delay(
            subscription_id, window_start.isoformat(), window_end.isoformat()
        )


@app.task
@inject
def meter_subscription_event_occurrences(
    subscription_id: int,
    window_start: str,
    window_end: str,
    metering_service: Annotated[MeteringService, Provide["metering_service"]],
) -> None:
    """Meter one subscription's pooled subtree over an explicit window.

    Idempotent, as ``CELERY_TASK_ACKS_LATE`` requires: the same arguments produce
    the same rows, and re-running inserts nothing.

    A subscription deleted between fan-out and execution is logged and skipped
    rather than raising — a raising task is redelivered and fails identically
    forever, turning a benign race into a permanent stream of alerts.
    """
    subscription = Subscription.objects.filter(pk=subscription_id).first()
    if subscription is None:
        logger.info(
            "Skipping occurrence metering for subscription %s: it no longer exists.",
            subscription_id,
        )
        return

    result = metering_service.meter_occurrences_for_period(
        subscription,
        datetime.datetime.fromisoformat(window_start),
        datetime.datetime.fromisoformat(window_end),
    )
    logger.info(
        "Metered subscription %s over [%s, %s): %s occurrences seen, %s newly recorded.",
        result.subscription_id,
        result.window_start,
        result.window_end,
        result.occurrences_seen,
        result.occurrences_recorded,
    )


@app.task
def process_dunning() -> None:
    """Beat entry point: fan out one dunning tick per subscription currently
    GRACE or RESTRICTED.

    Subscriptions on any other ``billing_state`` (``ACTIVE``, ``FREE``,
    ``CANCELLED``) are never selected -- once a subscription leaves GRACE for
    ACTIVE (a successful retry, confirmed through the subscription-payment
    webhook), the next run of this query no longer includes it, which is what
    stops the ladder from retrying an already-resolved subscription.
    """
    subscription_ids = list(
        Subscription.objects.filter(
            billing_state__in=(BillingState.GRACE, BillingState.RESTRICTED)
        ).values_list("pk", flat=True)
    )
    for subscription_id in subscription_ids:
        process_dunning_for_subscription.delay(subscription_id)


@app.task
@inject
def process_dunning_for_subscription(
    subscription_id: int,
    dunning_service: Annotated[DunningService, Provide["dunning_service"]],
) -> None:
    """One dunning tick for one subscription, dispatched through
    ``DunningService.process_subscription`` -- never a direct
    ``billing_state`` write here (see ``payments.services.billing_state_machine``).

    Idempotent under ``CELERY_TASK_ACKS_LATE`` redelivery:
    ``DunningService``'s own retry-bucket gate (``Subscription.last_dunning_attempt_at``)
    and the retry charge's bucket-derived ``idempotency_key`` -- both views of
    one ``_retry_attempt_ordinal`` -- are what make a redelivered tick harmless,
    not anything here.

    A subscription deleted between fan-out and execution is logged and skipped
    rather than raising -- a raising task is redelivered and fails identically
    forever, turning a benign race into a permanent stream of alerts (same
    reasoning as ``meter_subscription_event_occurrences``, above).
    """
    subscription = Subscription.objects.filter(pk=subscription_id).first()
    if subscription is None:
        logger.info(
            "Skipping dunning tick for subscription %s: it no longer exists.",
            subscription_id,
        )
        return
    dunning_service.process_subscription(subscription)


@app.task
def close_billing_periods() -> None:
    """Beat entry point: fan out one cycle-close per subscription whose current
    billing period has ended.

    The window (which subscriptions are due) is decided **once here** from
    ``timezone.now()`` and each subscription is closed in its own task, so one
    subscription's close failing (a declined overage charge, a provider error)
    records its failure and does not abort the rest of the sweep — a
    best-effort-across-subscriptions approach. Each close is idempotent (the rolled
    ``current_period_start`` is the durable marker; the overage charge carries a
    ``(subscription, period_start)`` idempotency key), so a
    ``CELERY_TASK_ACKS_LATE`` redelivery is harmless.

    Only billing-root subscriptions with an elapsed period are selected
    (``CycleCloseService.subscriptions_to_close`` reuses
    ``MeteringService.subscriptions_to_sweep`` so "which subscription owns this
    usage" has a single definition).
    """
    for subscription_id in CycleCloseService.subscriptions_to_close():
        close_subscription_billing_period.delay(subscription_id)


@app.task
@inject
def close_subscription_billing_period(
    subscription_id: int,
    cycle_close_service: Annotated[CycleCloseService, Provide["cycle_close_service"]],
) -> None:
    """Close every elapsed period for one subscription, dispatched through
    ``CycleCloseService.close_subscription`` — the single place a period is
    settled and rolled (see that method's docstring).

    A subscription deleted between fan-out and execution is logged and skipped
    rather than raising (same reasoning as ``meter_subscription_event_occurrences``).

    A close failure (declined charge, provider error) is caught and logged rather
    than re-raised: the period stays unrolled, so the next beat tick re-dispatches
    and retries it (with the same overage idempotency key, so a partially-charged
    period does not double-charge), and one poison subscription never spins the
    task or blocks the rest of the sweep.
    """
    subscription = Subscription.objects.filter(pk=subscription_id).first()
    if subscription is None:
        logger.info(
            "Skipping cycle close for subscription %s: it no longer exists.",
            subscription_id,
        )
        return
    try:
        closed = cycle_close_service.close_subscription(subscription)
    except Exception:  # noqa: BLE001 - best-effort: never let one close abort the sweep
        logger.exception(
            "Cycle close failed for subscription %s; the period is left unrolled and will be "
            "retried on the next sweep (the overage idempotency key prevents a double charge).",
            subscription_id,
        )
        return
    logger.info(
        "Cycle close for subscription %s settled %s period(s).",
        subscription_id,
        len(closed),
    )


@app.task
def check_approaching_limits() -> None:
    """Beat entry point: fan out one approaching-limit check per subscription
    that could still be warned before being blocked.

    Excludes ``RESTRICTED`` (already blocked -- see
    ``UsageWarningService.check_subscription`` for why warning it further adds
    nothing) and ``CANCELLED`` (running out the clock to ``FREE``, not
    accruing toward a block). ``FREE``, ``ACTIVE``, and ``GRACE`` subscriptions
    are all in scope -- a free-tier organization approaching its seat limit
    needs the same proactive warning as a paid one.
    """
    subscription_ids = list(
        Subscription.objects.exclude(
            billing_state__in=(BillingState.RESTRICTED, BillingState.CANCELLED)
        ).values_list("pk", flat=True)
    )
    for subscription_id in subscription_ids:
        check_approaching_limits_for_subscription.delay(subscription_id)


@app.task
@inject
def check_approaching_limits_for_subscription(
    subscription_id: int,
    usage_warning_service: Annotated[UsageWarningService, Provide["usage_warning_service"]],
) -> None:
    """One approaching-limit sweep for one subscription, dispatched through
    ``UsageWarningService.check_subscription`` -- the single place "approaching
    a limit" is defined (see that method's docstring).

    Idempotent under ``CELERY_TASK_ACKS_LATE`` redelivery and safe to re-run on
    every beat tick: ``LimitWarningNotification``'s unique constraint, not
    anything here, is what keeps a still-crossed threshold from re-notifying
    every tick within the same billing cycle.

    A subscription deleted between fan-out and execution is logged and skipped
    rather than raising -- a raising task is redelivered and fails identically
    forever, turning a benign race into a permanent stream of alerts (same
    reasoning as ``meter_subscription_event_occurrences``/
    ``process_dunning_for_subscription``, above).
    """
    subscription = Subscription.objects.filter(pk=subscription_id).first()
    if subscription is None:
        logger.info(
            "Skipping approaching-limit check for subscription %s: it no longer exists.",
            subscription_id,
        )
        return
    usage_warning_service.check_subscription(subscription)
