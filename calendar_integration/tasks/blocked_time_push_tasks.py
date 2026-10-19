import logging
from collections.abc import Iterator
from contextlib import contextmanager

from celery import Task
from dependency_injector.wiring import Provide, inject

from calendar_integration.services.blocked_time_provider_sync_service import (
    BlockedTimeProviderSyncService,
)
from calendar_integration.services.calendar_webhook_service import PROVIDER_CALL_ERRORS
from common.organization_context import organization_context
from organizations.models import Organization
from vinta_schedule_api.celery import app


logger = logging.getLogger(__name__)

#: How many times a failed push is retried before the block is marked ``failed``.
PUSH_MAX_RETRIES = 5
#: Seconds before the first retry; each later retry waits twice as long.
PUSH_RETRY_BASE_DELAY = 60


def _retry_or_give_up(task: Task, exc: Exception, action: str) -> None:
    """Retry ``task`` with a growing delay, or return after its last retry.

    An eager run (local development) gives up at once: a retry would raise into the
    request that queued it, after the block was already saved. ``action`` names what
    failed, by ids only, for the log.
    """
    if task.request.retries >= PUSH_MAX_RETRIES or task.request.is_eager:
        logger.warning("Giving up %s: %s", action, type(exc).__name__)
        return
    logger.info("Retrying %s: %s", action, type(exc).__name__)
    raise task.retry(countdown=PUSH_RETRY_BASE_DELAY * 2**task.request.retries) from exc


@contextmanager
def _bound_to_organization(
    sync_service: BlockedTimeProviderSyncService, organization_id: int
) -> Iterator[bool]:
    """Bind the organization context and ``sync_service`` to ``organization_id``.

    Yields False, binding nothing, when the organization no longer exists.
    """
    organization = Organization.objects.filter(id=organization_id).first()
    if organization is None:
        yield False
        return
    with organization_context(organization):
        sync_service.initialize(organization)
        yield True


# Services are injected as defaults rather than inside ``Annotated``; see the note
# above the tasks in ``calendar_sync_tasks``.
@app.task(bind=True, max_retries=PUSH_MAX_RETRIES)
@inject
def push_blocked_time_to_provider_task(
    self: Task,
    blocked_time_id: int,
    organization_id: int,
    sync_service: BlockedTimeProviderSyncService = Provide["blocked_time_provider_sync_service"],
) -> None:
    """Write a blocked time to its Google calendar as an Out of office or busy event.

    Queued after the block is created or updated and committed. The event gets a fixed,
    neutral title ("Out of office" or "Busy") and declines with Google's default reply;
    the block's reason and decline message are never sent.

    A provider error is retried with a growing delay; after the last retry (or at once,
    when the task runs eagerly) the block is marked ``failed`` and stays in place, still
    blocking time in Vinta Schedule. Idempotent: the service skips a block already
    written, and the provider event id is fixed before the first write, so a repeat
    updates the same event.

    Only the block id and the error class are logged: the block's title and message
    can hold patient details.
    """
    with _bound_to_organization(sync_service, organization_id) as bound:
        if not bound:
            return
        try:
            sync_service.push(blocked_time_id)
        except PROVIDER_CALL_ERRORS as exc:
            _retry_or_give_up(self, exc, f"writing blocked time {blocked_time_id} to its provider")
            sync_service.mark_failed(blocked_time_id)


@app.task(bind=True, max_retries=PUSH_MAX_RETRIES)
@inject
def delete_blocked_time_from_provider_task(
    self: Task,
    calendar_id: int,
    provider_event_external_id: str,
    organization_id: int,
    sync_service: BlockedTimeProviderSyncService = Provide["blocked_time_provider_sync_service"],
) -> None:
    """Delete the Google event a deleted blocked time was written to.

    Queued after the block's delete is committed; the block is already gone, so the
    calendar and the provider event id arrive as arguments. Nobody is emailed. An event
    Google no longer has counts as deleted, so a repeated run is harmless.

    A provider error is retried with a growing delay; after the last retry (or at once,
    when the task runs eagerly) the task gives up and the Google event stays.

    Only the calendar id, the provider event id and the error class are logged.
    """
    with _bound_to_organization(sync_service, organization_id) as bound:
        if not bound:
            return
        try:
            sync_service.delete(calendar_id, provider_event_external_id)
        except PROVIDER_CALL_ERRORS as exc:
            _retry_or_give_up(
                self,
                exc,
                f"deleting provider event {provider_event_external_id} "
                f"of a blocked time on calendar {calendar_id}",
            )
