"""Starts provider event sync for rooms the push engine just created."""

import datetime
import logging
from typing import Any

from django.dispatch import receiver

from calendar_integration.constants import CalendarProvider
from calendar_integration.models import Calendar, GoogleCalendarServiceAccount
from calendar_integration.signals import resource_room_synced
from common.feature_flags import RESOURCE_CALENDAR_PROVIDER_SYNC, is_enabled
from common.organization_context import organization_context
from di_core.containers import get_container


logger = logging.getLogger(__name__)

EVENT_SYNC_WINDOW = datetime.timedelta(days=365)


@receiver(resource_room_synced)
def request_event_sync_for_created_google_room(
    sender: Any, calendar_id: int, provider: str, created: bool, **kwargs: Any
) -> None:
    """Request event sync for a Google room Vinta Schedule just created.

    Syncs through the organization's service account, as the resource import does.
    Rooms the resync linked (``created=False``), other providers and flag-off
    organizations are skipped. Microsoft room events sync through their own
    subscriptions.
    """
    if not created or provider != CalendarProvider.GOOGLE:
        return

    # Cross-organization on purpose: the signal carries only the calendar id, and the
    # organization to bind comes from the row itself.
    calendar = (
        Calendar.objects.unscoped().select_related("organization").filter(id=calendar_id).first()
    )
    if calendar is None:
        return
    organization = calendar.organization
    if not is_enabled(RESOURCE_CALENDAR_PROVIDER_SYNC, organization.id):
        return

    with organization_context(organization):
        service_account = (
            GoogleCalendarServiceAccount.objects.filter_by_organization(organization.id)
            .filter(calendar_fk__isnull=True)
            .first()
        )
        if service_account is None:
            logger.warning(
                "No Google service account to sync events for room %s (organization %s)",
                calendar_id,
                organization.id,
            )
            return

        calendar_service = get_container().calendar_service()
        calendar_service.authenticate(account=service_account, organization=organization)
        now = datetime.datetime.now(datetime.UTC)
        calendar_service.request_calendar_sync(
            calendar=calendar,
            start_datetime=now,
            end_datetime=now + EVENT_SYNC_WINDOW,
            should_update_events=True,
        )
