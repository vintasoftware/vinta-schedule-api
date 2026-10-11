from dependency_injector import providers

from payments.containers import BillingContainer
from webhooks.services import (
    WebhookCalendarEventSideEffectsService,
    WebhookMembershipSideEffectsService,
    WebhookService,
)


class WebhooksContainer(BillingContainer):
    """Providers for the webhook services."""

    webhook_service = providers.Factory(
        WebhookService,
        entitlement_service=BillingContainer.entitlement_service,
    )

    webhook_calendar_side_effects_service = providers.Factory(
        WebhookCalendarEventSideEffectsService,
        webhook_service=webhook_service,
    )

    webhook_membership_side_effects_service = providers.Factory(
        WebhookMembershipSideEffectsService,
        webhook_service=webhook_service,
    )
