import datetime
from typing import TYPE_CHECKING, Annotated

from dependency_injector.wiring import Provide, inject

from vinta_schedule_api.celery import app
from webhooks.constants import WebhookStatus
from webhooks.models import WebhookEvent


if TYPE_CHECKING:
    from webhooks.services import WebhookService


@app.task
@inject
def process_webhook_event(
    event_id: int,
    organization_id: int,
    webhook_service: Annotated["WebhookService | None", Provide["webhook_service"]] = None,
):
    if not webhook_service:
        return

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
