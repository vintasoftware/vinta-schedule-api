"""Scheduled billing work.

``meter_event_occurrences`` is the only thing that turns computed calendar
occurrences into billable rows, so the correctness of post-paid billing rests on
it running — and on it being harmless when it runs twice.
"""

import datetime
import logging
from typing import Annotated

from django.utils import timezone

from dependency_injector.wiring import Provide, inject

from payments.models import Subscription
from payments.services.metering_service import MeteringService
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
