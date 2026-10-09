import logging

from celery import Task
from dependency_injector.wiring import Provide, inject

from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.calendar_webhook_service import PROVIDER_CALL_ERRORS
from common.organization_context import organization_context
from organizations.models import Organization
from vinta_schedule_api.celery import app


logger = logging.getLogger(__name__)

#: How many times a failed push is retried before the block is marked ``failed``.
PUSH_MAX_RETRIES = 5
#: Seconds before the first retry; each later retry waits twice as long.
PUSH_RETRY_BASE_DELAY = 60


# Services are injected as defaults rather than inside ``Annotated``; see the note
# above the tasks in ``calendar_sync_tasks``.
@app.task(bind=True, max_retries=PUSH_MAX_RETRIES)
@inject
def push_blocked_time_to_provider_task(
    self: Task,
    blocked_time_id: int,
    organization_id: int,
    calendar_service: CalendarService = Provide["calendar_service"],
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
    organization = Organization.objects.filter(id=organization_id).first()
    if organization is None:
        return

    with organization_context(organization):
        calendar_service.initialize_without_provider(organization=organization)
        try:
            calendar_service.push_blocked_time_to_provider(blocked_time_id)
        except PROVIDER_CALL_ERRORS as exc:
            # An eager run (local development) gives up at once: a retry would raise
            # into the request that queued it, after the block was already saved.
            if self.request.retries >= PUSH_MAX_RETRIES or self.request.is_eager:
                logger.warning(
                    "Giving up writing blocked time %s to its provider: %s",
                    blocked_time_id,
                    type(exc).__name__,
                )
                calendar_service.mark_blocked_time_push_failed(blocked_time_id)
                return
            logger.info(
                "Retrying the write of blocked time %s to its provider: %s",
                blocked_time_id,
                type(exc).__name__,
            )
            raise self.retry(countdown=PUSH_RETRY_BASE_DELAY * 2**self.request.retries) from exc


@app.task(bind=True, max_retries=PUSH_MAX_RETRIES)
@inject
def delete_blocked_time_from_provider_task(
    self: Task,
    calendar_id: int,
    provider_event_external_id: str,
    organization_id: int,
    calendar_service: CalendarService = Provide["calendar_service"],
) -> None:
    """Delete the Google event a deleted blocked time was written to.

    Queued after the block's delete is committed; the block is already gone, so the
    calendar and the provider event id arrive as arguments. Nobody is emailed. An event
    Google no longer has counts as deleted, so a repeated run is harmless.

    A provider error is retried with a growing delay; after the last retry (or at once,
    when the task runs eagerly) the task gives up and the Google event stays.

    Only the calendar id, the provider event id and the error class are logged.
    """
    organization = Organization.objects.filter(id=organization_id).first()
    if organization is None:
        return

    with organization_context(organization):
        calendar_service.initialize_without_provider(organization=organization)
        try:
            calendar_service.delete_blocked_time_from_provider(
                calendar_id, provider_event_external_id
            )
        except PROVIDER_CALL_ERRORS as exc:
            # See push_blocked_time_to_provider_task on giving up at once when eager.
            if self.request.retries >= PUSH_MAX_RETRIES or self.request.is_eager:
                logger.warning(
                    "Giving up deleting provider event %s of a blocked time on calendar %s: %s",
                    provider_event_external_id,
                    calendar_id,
                    type(exc).__name__,
                )
                return
            logger.info(
                "Retrying the delete of provider event %s of a blocked time on calendar %s: %s",
                provider_event_external_id,
                calendar_id,
                type(exc).__name__,
            )
            raise self.retry(countdown=PUSH_RETRY_BASE_DELAY * 2**self.request.retries) from exc
