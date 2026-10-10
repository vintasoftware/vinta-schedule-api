from vinta_billing.services.entitlement_service import EntitlementService

from di_core.containers import AppContainer
from webhooks.containers import WebhooksContainer
from webhooks.services import (
    WebhookCalendarEventSideEffectsService,
    WebhookMembershipSideEffectsService,
    WebhookService,
)


def test_webhook_service_receives_entitlement_service() -> None:
    """webhook_service resolves to a WebhookService with entitlement_service."""
    container = AppContainer()

    webhook_svc = container.webhook_service()

    assert isinstance(webhook_svc, WebhookService)
    assert isinstance(webhook_svc.entitlement_service, EntitlementService)


def test_side_effects_services_receive_webhook_service() -> None:
    """Both side-effects services receive WebhookService with correct type."""
    container = AppContainer()

    calendar_side_effects = container.webhook_calendar_side_effects_service()
    membership_side_effects = container.webhook_membership_side_effects_service()

    assert isinstance(calendar_side_effects, WebhookCalendarEventSideEffectsService)
    assert isinstance(membership_side_effects, WebhookMembershipSideEffectsService)
    assert isinstance(calendar_side_effects.webhook_service, WebhookService)
    assert isinstance(membership_side_effects.webhook_service, WebhookService)
    assert isinstance(calendar_side_effects.webhook_service.entitlement_service, EntitlementService)
    assert isinstance(
        membership_side_effects.webhook_service.entitlement_service, EntitlementService
    )


def test_webhook_providers_same_from_both_containers() -> None:
    """The alias in AppContainer points to the same provider object."""
    assert AppContainer.webhook_service is WebhooksContainer.webhook_service
    assert (
        AppContainer.webhook_calendar_side_effects_service
        is WebhooksContainer.webhook_calendar_side_effects_service
    )
    assert (
        AppContainer.webhook_membership_side_effects_service
        is WebhooksContainer.webhook_membership_side_effects_service
    )
