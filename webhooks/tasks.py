import datetime
import logging
from typing import TYPE_CHECKING, Annotated

from dependency_injector.wiring import Provide, inject

from common.organization_context import organization_context
from tenancy.models import Organization
from vinta_schedule_api.celery import app
from webhooks.constants import WebhookStatus
from webhooks.models import WebhookEvent


if TYPE_CHECKING:
    from webhooks.services import WebhookService


logger = logging.getLogger(__name__)


@app.task
@inject
def process_webhook_event(
    event_id: int,
    organization_id: int,
    webhook_service: Annotated["WebhookService | None", Provide["webhook_service"]] = None,
):
    if not webhook_service:
        return

    # Resolved eagerly (not via a lazy binding): a stale/deleted
    # `organization_id` must be caught here, at the task boundary, rather
    # than surfacing later as a bound-but-null organization deep inside a
    # manager once Phase 2 starts consulting the context -- mirrors
    # `calendar_integration/tasks/calendar_sync_tasks.py`'s
    # `organization = Organization.objects.filter(id=organization_id).first()`
    # / `if not organization: return` guard.
    organization = Organization.objects.filter(id=organization_id).first()
    if organization is None:
        logger.info(
            "Skipping webhook event %s: organization %s no longer exists.",
            event_id,
            organization_id,
        )
        return

    with organization_context(organization):
        webhook_event = WebhookEvent.objects.filter(
            id=event_id,
            organization_id=organization_id,
            status=WebhookStatus.PENDING,
        ).first()

        if not webhook_event:
            return

        now = datetime.datetime.now(tz=datetime.UTC)
        if webhook_event.send_after and webhook_event.send_after > now:
            return

        webhook_service.process_webhook_event(event=webhook_event)
