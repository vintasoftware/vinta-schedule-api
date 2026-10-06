"""Starts provider event sync for rooms the push engine just created."""

from typing import Any

from django.db import transaction
from django.dispatch import receiver

from calendar_integration.constants import CalendarProvider
from calendar_integration.signals import resource_room_synced
from calendar_integration.tasks import start_room_event_sync_task


@receiver(
    resource_room_synced,
    dispatch_uid="calendar_integration.receivers.request_event_sync_for_created_google_room",
)
def request_event_sync_for_created_google_room(
    sender: Any,
    calendar_id: int,
    organization_id: int,
    provider: str,
    created: bool,
    **kwargs: Any,
) -> None:
    """Queue event sync for a Google room Vinta Schedule just created.

    Rooms the resync linked (``created=False``) and other providers are skipped;
    Microsoft room events sync through their own subscriptions. The task checks the
    feature flag and does the provider work, so a billing refusal there cannot
    fail the push that sent this signal.
    """
    if not created or provider != CalendarProvider.GOOGLE:
        return

    transaction.on_commit(
        lambda: start_room_event_sync_task.delay(
            calendar_id=calendar_id, organization_id=organization_id
        )
    )
