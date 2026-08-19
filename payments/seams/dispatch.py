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
``autodiscover_tasks()`` executes. ``payments.dispatch_billing_job`` is
reachable via ``.delay()`` from Phase 0 onward; nothing calls it yet, but that
is a matter of no caller existing, not of the task being unregistered. Phase 2
(``payments/tasks.py``, the beat wrappers) is where the first caller lands.
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
    per-request transaction. The beat tasks that call these sweeps (wired in
    Phase 2, ``payments/tasks.py``) are their own top-level Celery tasks, not
    request handlers.
    """
    dotted_path = f"{job.__module__}.{job.__qualname__}"
    _dispatch_billing_job.delay(dotted_path, list(args))
