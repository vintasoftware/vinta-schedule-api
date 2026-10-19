"""Request-side steps of writing blocked times with decline options to their provider.

These run inside the request that changes a block: they save the block's options and
mark it to be written, then queue the provider write for after the transaction commits.
The write itself runs in a task through ``BlockedTimeProviderSyncService``.
"""

from django.db import transaction

from calendar_integration.exceptions import BlockDeclineOptionsWithRecurrenceError
from calendar_integration.models import BlockedTime
from calendar_integration.services.dataclasses import BlockDeclineOptions


def check_recurrence(decline_scope: str | None, rrule_string: str | None) -> None:
    """Refuse decline options on a recurring block.

    A recurring block is never written to the provider and cancels nothing, so its
    options would have no effect.

    :raises BlockDeclineOptionsWithRecurrenceError: When the block has a
        ``decline_scope`` and ``rrule_string`` is set.
    """
    if decline_scope is not None and rrule_string:
        raise BlockDeclineOptionsWithRecurrenceError()


def apply_decline_options(blocked_time: BlockedTime, decline_options: BlockDeclineOptions) -> None:
    """Save ``decline_options`` on a new block and queue its provider write.

    Only a block on a Google calendar is written to its provider
    (``BlockedTime.is_written_to_provider``); on any other calendar the options are saved
    and nothing is queued.
    """
    blocked_time.decline_scope = decline_options.scope
    blocked_time.decline_message = decline_options.message
    blocked_time.save(update_fields=["decline_scope", "decline_message", "modified"])
    if blocked_time.is_written_to_provider:
        queue_push(blocked_time)


def mark_changed(blocked_time: BlockedTime) -> None:
    """Mark an edited block to be written again, before it is saved.

    Does nothing for a block that is not written to its provider. Otherwise its
    ``provider_push_version`` moves on, so a push already running does not mark the edit
    written, and a new push is queued for after the commit.
    """
    if not blocked_time.is_written_to_provider:
        return
    blocked_time.provider_push_version += 1
    queue_push(blocked_time)


def queue_push(blocked_time: BlockedTime) -> None:
    """Queue ``push_blocked_time_to_provider_task`` for after the transaction commits."""
    # Imported here: the tasks package imports CalendarService, which imports this module.
    from calendar_integration.tasks.blocked_time_push_tasks import (
        push_blocked_time_to_provider_task,
    )

    blocked_time_id = blocked_time.id
    organization_id = blocked_time.organization_id
    transaction.on_commit(
        lambda: push_blocked_time_to_provider_task.delay(  # type: ignore[attr-defined]
            blocked_time_id, organization_id
        )
    )


def queue_delete(blocked_time: BlockedTime) -> None:
    """Queue the delete of a block's provider event for after the transaction commits.

    Does nothing for a block that has no provider event. Call it before the block is
    deleted: the task gets the calendar and the event id as arguments.
    """
    provider_event_external_id = blocked_time.provider_event_external_id
    if not provider_event_external_id:
        return
    # Imported here: the tasks package imports CalendarService, which imports this module.
    from calendar_integration.tasks.blocked_time_push_tasks import (
        delete_blocked_time_from_provider_task,
    )

    calendar_id = blocked_time.calendar_fk_id
    organization_id = blocked_time.organization_id
    transaction.on_commit(
        lambda: delete_blocked_time_from_provider_task.delay(  # type: ignore[attr-defined]
            calendar_id, provider_event_external_id, organization_id
        )
    )
