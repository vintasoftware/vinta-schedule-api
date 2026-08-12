import datetime
from typing import TYPE_CHECKING, Annotated

from django.utils.functional import SimpleLazyObject

from dependency_injector.wiring import Provide, inject

from common.organization_context import organization_context
from organizations.models import Organization
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

    # The task is dispatched with `organization_id` only, never a loaded
    # `Organization`. Binding is resolved lazily (rather than fetched
    # eagerly) so this phase's binding is a genuine no-op today: nothing
    # reads the bound organization yet (the current manager ignores the
    # binding entirely), so wrapping the lookup in a `SimpleLazyObject`
    # means no extra query runs until Phase 2 actually starts consulting
    # the context.
    organization = SimpleLazyObject(lambda: Organization.objects.filter(id=organization_id).first())

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
