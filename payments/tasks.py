"""Scheduled billing work -- thin Celery wrappers over ``vinta_billing.jobs``.

``vinta_billing.jobs`` ships no Celery integration of its own (see that
module's own docstring: "wiring it is entirely a project's job") and its own
documented wiring pattern is exactly what this module does: a ``@shared_task``
per per-subscription job, and a beat task that calls the package's sweep with
an explicit ``dispatch`` that enqueues onto that local task rather than
running inline.

**Services are not resolved here.** ``vinta_billing.jobs`` resolves each one
through ``VINTA_BILLING['SERVICE_CONTAINER']``, which this project points at
``di_core.containers.container`` -- the same container every view, GraphQL
resolver and other Celery task uses, and the same one a test's
``di_container.<provider>.override(...)`` reaches.

Through ``vinta-django-billing`` 0.5.0 that was not true: ``vinta_billing.jobs``
imported the package's own factories directly instead of going through
``resolve_service()``, so a beat tick got a second, parallel set of services
built straight from settings, blind to any DI override. Each task below
therefore carried ``@inject`` / ``Provide[...]`` and passed its service in by
hand. 0.6.0 closed that gap and the plumbing came out; the per-subscription job
functions still accept an optional service keyword, and an explicit argument
still wins, but nothing here needs to supply one.

**The per-subscription fan-out does not use
``VINTA_BILLING['JOB_DISPATCHER']``, and should not.** Each beat task passes
its own ``dispatch`` lambda so the fan-out lands on the *local* task beside it,
inside ``_bound_organization(...)``. A ``JOB_DISPATCHER`` is handed the package
job function itself, so any implementation general enough to serialize it
(``payments.seams.dispatch``, deleted in Phase 2, funnelled every job through
one generic task that re-imported it by dotted path) calls
``vinta_billing.jobs.<job>`` directly and skips these wrappers -- and with them
the organization binding that ``_bound_organization``'s docstring explains is
not cosmetic. The four lambdas are what keep the fan-out on this module's own
tasks.

Task dotted paths stay ``payments.tasks.*`` for the four beat entry points
(``vinta_schedule_api/celerybeat_schedule.py`` names them literally) and for
the four per-subscription tasks (nothing outside this module names their
dotted paths, but keeping them as real Celery tasks, not inline closures, is
what lets ``.delay()`` serialize them for a real worker).

**Each per-subscription task binds ``organization_context(subscription
.organization)`` around its call into ``vinta_billing.jobs``**, mirroring
what this module did before this phase. ``Subscription`` itself is not
organization-scoped (billing is read at the billing root, often an ancestor
of a single organization -- see ``vinta_billing.models``), so nothing here
strictly *requires* a bound organization to resolve the subscription row.
But the seams a job's call graph reaches into read organization-scoped models
(``payments.seams.occurrences.CalendarEventOccurrenceSource`` chief among
them) and, while every one of those already names its organization
explicitly (``unscoped()`` + ``organization_id__in=...`` / a safe-relation
join), an unbound context is enough to turn a structurally-empty scoped
``SELECT`` (e.g. ``id__in=[None]``, which a plain recurring event with no
exceptions produces) into a crash rather than the empty result Django's own
query planner would otherwise hand back silently. Binding the subscription's
organization here is the same "the obvious, single-organization boundary for
a per-subscription unit of work" this module always used, and it is not
merely cosmetic.
"""

from typing import TYPE_CHECKING

from vinta_billing import jobs
from vinta_billing.models import Subscription

from common.organization_context import organization_context
from organizations.models import Organization
from vinta_schedule_api.celery import app


if TYPE_CHECKING:
    from vinta_orgs.state import OrganizationContext


def _bound_organization(subscription_id: int) -> "OrganizationContext[Organization]":
    """``organization_context(...)`` bound to ``subscription_id``'s
    organization, or a no-op context when the subscription no longer exists
    (the job function being called handles that race on its own -- see each
    task's docstring below -- so this only has to not raise)."""
    subscription = Subscription.objects.filter(pk=subscription_id).first()
    return organization_context(subscription.organization if subscription is not None else None)


@app.task
def meter_event_occurrences() -> None:
    """Beat entry point: ``vinta_billing.jobs.meter_event_occurrences``, fanning
    out onto :func:`meter_subscription_event_occurrences` rather than running
    each subscription's sweep inline."""
    jobs.meter_event_occurrences(
        dispatch=lambda job, *args: meter_subscription_event_occurrences.delay(*args)
    )


@app.task
def meter_subscription_event_occurrences(
    subscription_id: int,
    window_start: str,
    window_end: str,
) -> None:
    """One subscription's occurrence sweep, dispatched through
    ``vinta_billing.jobs.meter_subscription_event_occurrences``.

    A subscription deleted between fan-out and execution is logged and
    skipped by that call itself, not raised here.
    """
    with _bound_organization(subscription_id):
        jobs.meter_subscription_event_occurrences(subscription_id, window_start, window_end)


@app.task
def process_dunning() -> None:
    """Beat entry point: ``vinta_billing.jobs.process_dunning``, fanning out
    onto :func:`process_dunning_for_subscription` rather than running each
    subscription's tick inline."""
    jobs.process_dunning(dispatch=lambda job, *args: process_dunning_for_subscription.delay(*args))


@app.task
def process_dunning_for_subscription(subscription_id: int) -> None:
    """One dunning tick, dispatched through
    ``vinta_billing.jobs.process_dunning_for_subscription``.

    A subscription deleted between fan-out and execution is logged and
    skipped by that call itself, not raised here. Same best-effort
    ``except Exception`` guard around a provider fault as before this phase
    -- carried inside ``vinta_billing.jobs.process_dunning_for_subscription``
    itself now, not here.
    """
    with _bound_organization(subscription_id):
        jobs.process_dunning_for_subscription(subscription_id)


@app.task
def check_approaching_limits() -> None:
    """Beat entry point: ``vinta_billing.jobs.check_approaching_limits``,
    fanning out onto :func:`check_approaching_limits_for_subscription` rather
    than running each subscription's check inline."""
    jobs.check_approaching_limits(
        dispatch=lambda job, *args: check_approaching_limits_for_subscription.delay(*args)
    )


@app.task
def check_approaching_limits_for_subscription(subscription_id: int) -> None:
    """One approaching-limit check, dispatched through
    ``vinta_billing.jobs.check_approaching_limits_for_subscription``.

    A subscription deleted between fan-out and execution is logged and
    skipped by that call itself, not raised here.
    """
    with _bound_organization(subscription_id):
        jobs.check_approaching_limits_for_subscription(subscription_id)


@app.task
def close_billing_periods() -> None:
    """Beat entry point: ``vinta_billing.jobs.close_billing_periods``, fanning
    out onto :func:`close_subscription_billing_period` rather than running
    each subscription's close inline."""
    jobs.close_billing_periods(
        dispatch=lambda job, *args: close_subscription_billing_period.delay(*args)
    )


@app.task
def close_subscription_billing_period(subscription_id: int) -> None:
    """One subscription's period close(s), dispatched through
    ``vinta_billing.jobs.close_subscription_billing_period``.

    A subscription deleted between fan-out and execution is logged and
    skipped by that call itself, not raised here. Same best-effort
    ``except Exception`` guard around a provider fault as before this phase
    -- carried inside ``vinta_billing.jobs.close_subscription_billing_period``
    itself now, not here.
    """
    with _bound_organization(subscription_id):
        jobs.close_subscription_billing_period(subscription_id)
