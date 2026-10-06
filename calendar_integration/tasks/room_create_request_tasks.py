"""Celery tasks for synced-room create requests (idempotency keys)."""

import logging

from calendar_integration.models import ResourceCalendarCreateRequest
from vinta_schedule_api.celery import app


logger = logging.getLogger(__name__)


@app.task
def purge_expired_resource_calendar_create_requests_task() -> int:
    """Delete the synced-room create requests whose idempotency window has closed.

    Runs daily from beat. Only the idempotency key rows go; the rooms they point at
    are untouched. Safe to run twice: a second run finds nothing to delete.

    Returns the number of rows deleted.
    """
    # Every organization at once on purpose: beat runs with no organization bound,
    # and an expired key is dead data whichever organization it belongs to.
    deleted, _ = ResourceCalendarCreateRequest.objects.unscoped().expired().delete()
    logger.info("Purged %s expired resource calendar create requests.", deleted)
    return deleted
