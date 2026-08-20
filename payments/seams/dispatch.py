"""Celery bridge for ``vinta_billing.jobs``' per-subscription fan-out.

Every sweep in ``vinta_billing.jobs`` (``meter_event_occurrences``,
``process_dunning``, ``close_billing_periods``, ``check_approaching_limits``)
finds the subscriptions that need work and hands each one to a
per-subscription job through one ``dispatch(job, *args)`` call --
``vinta_billing.jobs.Dispatch``. The package ships no Celery integration on
purpose (see that module's docstring): wiring it is entirely a project's job,
and this is that wiring, pointed at ``VINTA_BILLING['JOB_DISPATCHER']``.

``job`` is a plain function object from ``vinta_billing.jobs`` -- none of the
four per-subscription jobs are themselves ``@shared_task``-decorated, so
``dispatch`` cannot serialize ``job`` directly (Celery workers execute a task
by importing it fresh in their own process; an arbitrary function object is
not something the JSON-default serializer can carry, and even if it were, the
worker process may not yet have imported ``vinta_billing.jobs`` at all). This
seam serializes the job's dotted import path instead and re-imports it inside
the one generic task every dispatch call funnels through.

Already registered and callable from a running worker, today: a Celery worker
populates the Django app registry before it calls
``app.autodiscover_tasks()`` (``vinta_schedule_api/celery.py``), so
``di_core.apps.DICoreConfig.ready()`` -- which wires every package in
``INTERNAL_INSTALLED_APPS``, ``payments`` included, and so imports this module
-- has already run ``@shared_task`` on ``_dispatch_billing_job`` by the time
``autodiscover_tasks()`` executes. ``payments.dispatch_billing_job`` has been
reachable via ``.delay()`` since Phase 0.

**Not actually on the path ``payments/tasks.py``'s four beat tasks take, as of
Phase 2.** The expectation when this seam was built was that the beat wrappers
would call ``vinta_billing.jobs.<sweep>()`` with no explicit ``dispatch``,
letting ``VINTA_BILLING['JOB_DISPATCHER']`` (this module's
``dispatch_via_celery``) resolve automatically. Phase 2 found that does not
work for this project: the per-subscription jobs this dispatches
(``vinta_billing.jobs.process_dunning_for_subscription`` and friends) resolve
their service from ``vinta_billing.services.container`` when nothing supplies
one, which is a cache built from ``VINTA_BILLING`` settings directly and does
not see a test's (or production's) ``di_container.<provider>.override(...)``
-- unlike the shipped views/admin, it does not consult
``VINTA_BILLING['SERVICE_CONTAINER']`` either. So ``payments/tasks.py``
resolves each service through this project's own DI container via
``@inject`` and passes each one into ``vinta_billing.jobs`` explicitly,
bypassing ``JOB_DISPATCHER`` (and this module) with a `dispatch` argument
passed directly to each sweep. This module -- and the ``JOB_DISPATCHER``
setting -- are left in place for a caller of ``vinta_billing.jobs.*`` that
does not need a DI-wired service; there is none today.
"""

from __future__ import annotations

import importlib
from typing import Any

from celery import shared_task


@shared_task(name="payments.dispatch_billing_job")
def _dispatch_billing_job(dotted_path: str, args: list[Any]) -> None:
    """Re-import ``dotted_path`` and call it with ``args``, inside a worker.

    The dotted path always names a module-level callable in
    ``vinta_billing.jobs`` (a per-subscription job), so a plain
    ``import_module`` + ``getattr`` is enough -- no need for Django's
    ``import_string``, which this project reserves for dotted *settings*
    values.
    """
    module_name, _, attribute_name = dotted_path.rpartition(".")
    job = getattr(importlib.import_module(module_name), attribute_name)
    job(*args)


def dispatch_via_celery(job: Any, *args: Any) -> None:
    """``vinta_billing.jobs.Dispatch``: enqueue ``job(*args)`` on the existing
    Celery queue rather than running it inline in the beat process.

    Not wrapped in ``transaction.on_commit`` -- unlike a view, nothing that
    calls into ``vinta_billing.jobs`` runs inside ``ATOMIC_REQUESTS``'s
    per-request transaction. Any caller of a sweep would itself be its own
    top-level Celery task, not a request handler.

    See this module's docstring: ``payments/tasks.py``'s four beat tasks do
    not call this function as of Phase 2 -- each passes its own ``dispatch``
    directly to the ``vinta_billing.jobs`` sweep it calls instead, so its
    per-subscription jobs resolve their services from this project's DI
    container rather than from here.
    """
    dotted_path = f"{job.__module__}.{job.__qualname__}"
    _dispatch_billing_job.delay(dotted_path, list(args))
