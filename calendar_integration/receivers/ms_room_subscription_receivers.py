"""Start and stop Microsoft room event subscriptions as rooms are synced or archived.

The receivers only enqueue a task: subscribing calls Graph, and a signal handler is
no place for that. The signals carry ``calendar_id`` and ``provider`` but not the
organization, so the receiver reads it off the calendar.
"""

import logging
from typing import Any

from django.db import transaction
from django.dispatch import receiver

from calendar_integration.constants import CalendarProvider
from calendar_integration.models import Calendar
from calendar_integration.signals import resource_room_archived, resource_room_synced


logger = logging.getLogger(__name__)


def _organization_id(calendar_id: int) -> int | None:
    # `unscoped()`: the signal names a calendar but no organization, and the sender
    # may run with none bound. Only the organization id is read, to bind the task.
    return (
        Calendar.objects.unscoped()
        .filter(pk=calendar_id)
        .values_list("organization_id", flat=True)
        .first()
    )


@receiver(
    resource_room_synced,
    dispatch_uid="calendar_integration.receivers.subscribe_synced_microsoft_room",
)
def subscribe_synced_microsoft_room(
    sender: Any, calendar_id: int, provider: str, created: bool, **kwargs: Any
) -> None:
    """Subscribe to a Microsoft room's events once Vinta Schedule syncs the room.

    Both a room Vinta just created (``created=True``) and one the resync linked
    (``created=False``). The task checks the flag and the connection.
    """
    # Late for the import cycle: the tasks import CalendarService, which imports models.
    from calendar_integration.tasks import subscribe_microsoft_room_task

    if provider != CalendarProvider.MICROSOFT:
        return
    organization_id = _organization_id(calendar_id)
    if organization_id is None:
        return
    transaction.on_commit(lambda: subscribe_microsoft_room_task.delay(calendar_id, organization_id))


@receiver(
    resource_room_archived,
    dispatch_uid="calendar_integration.receivers.unsubscribe_archived_microsoft_room",
)
def unsubscribe_archived_microsoft_room(
    sender: Any, calendar_id: int, provider: str, **kwargs: Any
) -> None:
    """Stop a Microsoft room's event subscription once the room is archived."""
    from calendar_integration.tasks import unsubscribe_microsoft_room_task

    if provider != CalendarProvider.MICROSOFT:
        return
    organization_id = _organization_id(calendar_id)
    if organization_id is None:
        return
    transaction.on_commit(
        lambda: unsubscribe_microsoft_room_task.delay(calendar_id, organization_id)
    )
