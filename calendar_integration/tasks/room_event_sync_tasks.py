"""Celery tasks for syncing Microsoft room events with app-only credentials."""

import logging

from dependency_injector.wiring import Provide, inject
from vinta_billing.services.entitlement_service import EntitlementService

from calendar_integration.models import Calendar
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.tasks.calendar_sync_tasks import _restricted_or_skip
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
