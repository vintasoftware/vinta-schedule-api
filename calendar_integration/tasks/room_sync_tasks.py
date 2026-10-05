"""Celery tasks for the room (resource calendar) provider push engine."""

import datetime
import logging
import math

from django.utils import timezone

from dependency_injector.wiring import Provide, inject

from calendar_integration.services.room_sync_service import RoomSyncService
from common.organization_context import organization_context
from organizations.models import Organization
from vinta_schedule_api.celery import app


logger = logging.getLogger(__name__)


# Backoff between push attempts: 1 minute after the first failure, doubling, capped
# at 30 minutes. Attempts stop at the link's retry deadline, which the service checks.
PUSH_RETRY_BASE_SECONDS = 60
PUSH_RETRY_MAX_SECONDS = 30 * 60


def push_retry_countdown(attempt: int, deadline: datetime.datetime | None = None) -> int:
    """Seconds to wait before the push attempt after failed attempt number ``attempt``.

    ``attempt`` starts at 1: 60, 120, 240, 480, 960, then 1800 from the sixth on.

    With a ``deadline``, the wait is cut short so the next attempt runs at the
    deadline rather than after it. That last attempt is the one that either
    succeeds or moves the link to sync failed, so a push that keeps failing ends
    within a second or so of its deadline instead of up to 30 minutes later.
    """
    # The exponent is capped too, so a very high attempt count stays a small int.
    exponent = min(max(attempt, 1) - 1, 10)
    countdown = min(PUSH_RETRY_BASE_SECONDS * 2**exponent, PUSH_RETRY_MAX_SECONDS)
    if deadline is not None:
        until_deadline = math.ceil((deadline - timezone.now()).total_seconds())
        countdown = min(countdown, max(until_deadline, 1))
    return countdown


# `Provide[...]` is the default rather than inside `Annotated`; see the comment in
# `calendar_sync_tasks.py` and `tests/tasks/test_task_signatures.py` for why.


@app.task
@inject
def push_room_to_provider_task(
    link_id: int,
    organization_id: int,
    attempt_count: int,
    room_sync_service: RoomSyncService = Provide["room_sync_service"],
):
    """Push the change a ``ResourceCalendarProviderLink`` is waiting on to the provider.

    Binds the link's organization, runs ``RoomSyncService.push`` and, when the push
    failed transiently before its deadline, queues the next attempt with the backoff
    countdown. ``attempt_count`` is the link's attempt count when the task was
    queued; ``push`` ignores a task whose count is stale. Safe to run twice: a link
    that is no longer pending is left alone.
    """
    organization = Organization.objects.filter(id=organization_id).first()
    if organization is None:
        return

    with organization_context(organization):
        outcome = room_sync_service.push(link_id, attempt_count)

    if outcome.retry_attempt is None:
        return
    if push_room_to_provider_task.request.is_eager:
        # This run was eager (``CELERY_TASK_ALWAYS_EAGER``, the local default), so
        # the retry would run inline too, ignoring the countdown, and every retry
        # would run back to back until the deadline. The link stays pending, and
        # the next request_push queues a push with its current attempt count.
        logger.warning("Not retrying room link %s push: Celery runs eagerly.", link_id)
        return
    push_room_to_provider_task.apply_async(
        kwargs={
            "link_id": link_id,
            "organization_id": organization_id,
            "attempt_count": outcome.retry_attempt,
        },
        countdown=push_retry_countdown(outcome.retry_attempt, outcome.retry_deadline),
    )
