"""Celery tasks for Microsoft room event sync and its Graph subscriptions (app-only)."""

import logging

from dependency_injector.wiring import Provide, inject
from vinta_billing.services.entitlement_service import EntitlementService

from calendar_integration.constants import CalendarProvider
from calendar_integration.models import Calendar, ResourceCalendarProviderLink
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.calendar_webhook_service import (
    ROOM_GRAPH_ERRORS,
    MicrosoftRoomWebhookService,
)
from calendar_integration.tasks.calendar_sync_tasks import _restricted_or_skip
from common.feature_flags import RESOURCE_CALENDAR_PROVIDER_SYNC, organization_ids_with_flag
from common.organization_context import organization_context
from organizations.models import Organization
from vinta_schedule_api.celery import app


logger = logging.getLogger(__name__)


# Injected services default to `Provide[...]` for the reason given above the tasks in
# `calendar_sync_tasks`: Celery checks `.delay()` arguments against the real signature.


@app.task
@inject
def sync_microsoft_room_events_task(
    calendar_id: int,
    organization_id: int,
    calendar_service: CalendarService = Provide["calendar_service"],
    entitlement_service: EntitlementService = Provide["entitlement_service"],
) -> None:
    """Sync one Microsoft room's events into Vinta Schedule.

    Safe to run again or twice at once: the sync locks the room's calendar, and a run
    that waited on the lock starts from the delta token the other run stored. Does
    nothing for a missing organization or calendar, a restricted organization, a
    flag-off organization, or one without a write-enabled Microsoft connection.
    """
    organization = Organization.objects.filter(id=organization_id).first()
    if not organization:
        return

    with organization_context(organization):
        if _restricted_or_skip(entitlement_service, organization):
            return

        # No explicit organization filter: `organization_context` binds the
        # organization this task was handed, and `.objects` scopes to it.
        calendar = Calendar.objects.filter(id=calendar_id).first()
        if calendar is None:
            logger.info("Skipping Microsoft room sync: calendar %s not found.", calendar_id)
            return

        calendar_service.initialize_without_provider(organization=organization)
        calendar_service.sync_microsoft_room_events(calendar)


def _room_calendar(calendar_id: int) -> Calendar | None:
    # `organization_context` is bound by the caller, so `.objects` scopes to it.
    return Calendar.objects.filter(id=calendar_id).first()


@app.task
@inject
def subscribe_microsoft_room_task(
    calendar_id: int,
    organization_id: int,
    microsoft_room_webhook_service: MicrosoftRoomWebhookService = Provide[
        "microsoft_room_webhook_service"
    ],
) -> None:
    """Subscribe to a Microsoft room's event changes. A re-run keeps a live subscription."""
    organization = Organization.objects.filter(id=organization_id).first()
    if not organization:
        return
    with organization_context(organization):
        calendar = _room_calendar(calendar_id)
        if calendar is None:
            return
        microsoft_room_webhook_service.subscribe_microsoft_room(calendar)


@app.task
@inject
def unsubscribe_microsoft_room_task(
    calendar_id: int,
    organization_id: int,
    microsoft_room_webhook_service: MicrosoftRoomWebhookService = Provide[
        "microsoft_room_webhook_service"
    ],
) -> None:
    """Stop a Microsoft room's event subscription. A re-run finds nothing active."""
    organization = Organization.objects.filter(id=organization_id).first()
    if not organization:
        return
    with organization_context(organization):
        calendar = _room_calendar(calendar_id)
        if calendar is None:
            return
        microsoft_room_webhook_service.unsubscribe_microsoft_room(calendar)


@app.task
@inject
def renew_microsoft_room_subscriptions_task(
    microsoft_room_webhook_service: MicrosoftRoomWebhookService = Provide[
        "microsoft_room_webhook_service"
    ],
) -> None:
    """Beat: renew room subscriptions that expire within a day, in flag-on organizations."""
    renewed = microsoft_room_webhook_service.renew_expiring_microsoft_room_subscriptions()
    logger.info("Renewed %s Microsoft room subscriptions", renewed)


@app.task
@inject
def sweep_microsoft_room_events_task(
    microsoft_room_webhook_service: MicrosoftRoomWebhookService = Provide[
        "microsoft_room_webhook_service"
    ],
) -> None:
    """Beat, daily: sync every Microsoft room in flag-on organizations, and re-subscribe.

    The fallback for notifications Graph never delivered. Each room is subscribed
    again if it has no live subscription, then gets a delta sync.
    """
    for organization_id in organization_ids_with_flag(RESOURCE_CALENDAR_PROVIDER_SYNC):
        organization = Organization.objects.filter(id=organization_id).first()
        if organization is None:
            continue
        with organization_context(organization):
            for calendar in Calendar.objects.filter(
                id__in=ResourceCalendarProviderLink.objects.for_resync(
                    CalendarProvider.MICROSOFT
                ).values("calendar_fk_id")
            ):
                # One room's failure, or an organization whose consent was revoked,
                # must not stop the sweep for everyone else.
                try:
                    microsoft_room_webhook_service.subscribe_microsoft_room(calendar)
                except ROOM_GRAPH_ERRORS as exc:
                    logger.warning(
                        "Could not subscribe Microsoft room calendar %s of organization %s: %s",
                        calendar.pk,
                        organization_id,
                        type(exc).__name__,
                    )
                sync_microsoft_room_events_task.delay(calendar.pk, organization_id)
