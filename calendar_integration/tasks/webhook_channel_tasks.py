import logging

from django.db.models import Q
from django.utils import timezone

from dependency_injector.wiring import Provide, inject
from vinta_billing.services.entitlement_service import EntitlementService

from calendar_integration.constants import CalendarProvider
from calendar_integration.models import CalendarWebhookSubscription
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.calendar_webhook_service import GOOGLE_CHANNEL_RENEW_WITHIN
from calendar_integration.tasks.calendar_sync_tasks import (
    _authenticate_or_skip,
    _restricted_or_skip,
    ensure_watch_channel_or_log,
)
from common.organization_context import organization_context
from organizations.models import Organization
from vinta_schedule_api.celery import app


logger = logging.getLogger(__name__)


@app.task
def renew_google_calendar_watch_channels_task() -> None:
    """Queue a renewal for every active Google push channel that expires soon.

    Scheduled hourly. Google channels expire after at most a few days and cannot be
    extended, so each one is replaced before it lapses. A channel with no expiry
    recorded (opened before expiries were stored) is renewed too. One task per
    channel, so one organization's failure never blocks another's.
    """
    renew_before = timezone.now() + GOOGLE_CHANNEL_RENEW_WITHIN
    # ``original_manager``: the scheduler runs with no organization bound, and this
    # sweep spans every organization. Each renewal task binds its own.
    due = CalendarWebhookSubscription.original_manager.filter(
        Q(expires_at__lte=renew_before) | Q(expires_at__isnull=True),
        provider=CalendarProvider.GOOGLE,
        is_active=True,
    ).values_list("id", "organization_id")
    for subscription_id, organization_id in due:
        renew_google_calendar_watch_channel_task.delay(subscription_id, organization_id)


# Services are injected as defaults rather than inside ``Annotated``; see the note
# above the tasks in ``calendar_sync_tasks``.
@app.task
@inject
def renew_google_calendar_watch_channel_task(
    subscription_id: int,
    organization_id: int,
    calendar_service: CalendarService = Provide["calendar_service"],
    entitlement_service: EntitlementService = Provide["entitlement_service"],
) -> None:
    """Renew one Google push channel as the account that opened it.

    Idempotent: it goes through ``ensure_calendar_watch_channel``, which does nothing
    to a channel that is no longer close to expiring, so a redelivered task (acks are
    late) does not open a second channel.
    """
    organization = Organization.objects.filter(id=organization_id).first()
    if not organization:
        return

    with organization_context(organization):
        if _restricted_or_skip(entitlement_service, organization):
            return

        subscription = CalendarWebhookSubscription.objects.filter(
            id=subscription_id, is_active=True
        ).first()
        if subscription is None:
            return

        account = subscription.account
        if account is None:
            logger.warning(
                "Cannot renew Google channel subscription %s: no account recorded to "
                "renew it as. It is renewed after the calendar's next sync instead.",
                subscription.id,
            )
            return

        if not _authenticate_or_skip(calendar_service, account, organization):
            return
        ensure_watch_channel_or_log(calendar_service, subscription.calendar)
