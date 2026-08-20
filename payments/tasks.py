"""Scheduled billing work -- thin Celery wrappers over ``vinta_billing.jobs``.

``vinta_billing.jobs`` ships no Celery integration of its own (see that
module's own docstring: "wiring it is entirely a project's job") and its own
documented wiring pattern is exactly what this module does: a ``@shared_task``
per per-subscription job, and a beat task that calls the package's sweep with
an explicit ``dispatch`` that enqueues onto that local task rather than
running inline.

**Every service is resolved through this project's own DI container, not the
package's.** ``vinta_billing.jobs``' default service resolution
(``vinta_billing.services.container.get_dunning_service()`` and friends) is a
*process-wide cache* built from ``VINTA_BILLING`` settings directly -- it has
no way to see a test's ``di_container.stripe_subscription_gateway.override(...)``,
and, unlike the shipped views/admin, it does not consult
``VINTA_BILLING['SERVICE_CONTAINER']`` either (that setting is "where the
shipped views and the admin get their services" -- ``vinta_billing.conf``'s
own wording -- and ``vinta_billing.jobs`` imports the package's container
module directly, not through ``resolve_service()``). Every per-subscription
job function accepts its service as an optional keyword argument for exactly
this reason (see each job's docstring in ``vinta_billing.jobs``), so each task
below resolves its service via ``@inject`` / ``Provide[...]`` -- the same
container every view, GraphQL resolver and other Celery task in this project
uses -- and passes it through explicitly.

**The per-subscription fan-out therefore does not use
``VINTA_BILLING['JOB_DISPATCHER']``.** Each beat task below passes its own
``dispatch`` lambda straight to the package sweep. An earlier phase built
``payments.seams.dispatch.dispatch_via_celery`` for this role, before this DI
requirement was discovered against the real test suite; once every call site
here passed its own explicit ``dispatch``, that seam had no caller left, so
it -- and ``VINTA_BILLING['JOB_DISPATCHER']`` -- were deleted.

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

from typing import TYPE_CHECKING, Annotated

from dependency_injector.wiring import Provide, inject
from vinta_billing import jobs
from vinta_billing.models import Subscription
from vinta_billing.services.cycle_close_service import CycleCloseService
from vinta_billing.services.dunning_service import DunningService
from vinta_billing.services.metering_service import MeteringService
from vinta_billing.services.usage_warning_service import UsageWarningService

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
@inject
def meter_subscription_event_occurrences(
    subscription_id: int,
    window_start: str,
    window_end: str,
    metering_service: Annotated[MeteringService, Provide["metering_service"]],
) -> None:
    """One subscription's occurrence sweep, dispatched through
    ``vinta_billing.jobs.meter_subscription_event_occurrences`` with this
    project's DI-wired ``MeteringService``.

    A subscription deleted between fan-out and execution is logged and
    skipped by that call itself, not raised here.
    """
    with _bound_organization(subscription_id):
        jobs.meter_subscription_event_occurrences(
            subscription_id, window_start, window_end, metering_service=metering_service
        )


@app.task
def process_dunning() -> None:
    """Beat entry point: ``vinta_billing.jobs.process_dunning``, fanning out
    onto :func:`process_dunning_for_subscription` rather than running each
    subscription's tick inline."""
    jobs.process_dunning(dispatch=lambda job, *args: process_dunning_for_subscription.delay(*args))


@app.task
@inject
def process_dunning_for_subscription(
    subscription_id: int,
    dunning_service: Annotated[DunningService, Provide["dunning_service"]],
) -> None:
    """One dunning tick, dispatched through
    ``vinta_billing.jobs.process_dunning_for_subscription`` with this project's
    DI-wired ``DunningService``.

    A subscription deleted between fan-out and execution is logged and
    skipped by that call itself, not raised here. Same best-effort
    ``except Exception`` guard around a provider fault as before this phase
    -- carried inside ``vinta_billing.jobs.process_dunning_for_subscription``
    itself now, not here.
    """
    with _bound_organization(subscription_id):
        jobs.process_dunning_for_subscription(subscription_id, dunning_service=dunning_service)


@app.task
def check_approaching_limits() -> None:
    """Beat entry point: ``vinta_billing.jobs.check_approaching_limits``,
    fanning out onto :func:`check_approaching_limits_for_subscription` rather
    than running each subscription's check inline."""
    jobs.check_approaching_limits(
        dispatch=lambda job, *args: check_approaching_limits_for_subscription.delay(*args)
    )


@app.task
@inject
def check_approaching_limits_for_subscription(
    subscription_id: int,
    usage_warning_service: Annotated[UsageWarningService, Provide["usage_warning_service"]],
) -> None:
    """One approaching-limit check, dispatched through
    ``vinta_billing.jobs.check_approaching_limits_for_subscription`` with this
    project's DI-wired ``UsageWarningService``.

    A subscription deleted between fan-out and execution is logged and
    skipped by that call itself, not raised here.
    """
    with _bound_organization(subscription_id):
        jobs.check_approaching_limits_for_subscription(
            subscription_id, usage_warning_service=usage_warning_service
        )


@app.task
def close_billing_periods() -> None:
    """Beat entry point: ``vinta_billing.jobs.close_billing_periods``, fanning
    out onto :func:`close_subscription_billing_period` rather than running
    each subscription's close inline."""
    jobs.close_billing_periods(
        dispatch=lambda job, *args: close_subscription_billing_period.delay(*args)
    )


@app.task
@inject
def close_subscription_billing_period(
    subscription_id: int,
    cycle_close_service: Annotated[CycleCloseService, Provide["cycle_close_service"]],
) -> None:
    """One subscription's period close(s), dispatched through
    ``vinta_billing.jobs.close_subscription_billing_period`` with this
    project's DI-wired ``CycleCloseService``.

    A subscription deleted between fan-out and execution is logged and
    skipped by that call itself, not raised here. Same best-effort
    ``except Exception`` guard around a provider fault as before this phase
    -- carried inside ``vinta_billing.jobs.close_subscription_billing_period``
    itself now, not here.
    """
    with _bound_organization(subscription_id):
        jobs.close_subscription_billing_period(
            subscription_id, cycle_close_service=cycle_close_service
        )
