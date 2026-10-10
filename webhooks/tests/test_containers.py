import pytest
from dependency_injector import providers

from di_core.containers import AppContainer
from webhooks.containers import WebhooksContainer
from webhooks.services import (
    WebhookCalendarEventSideEffectsService,
    WebhookMembershipSideEffectsService,
    WebhookService,
)


WEBHOOK_PROVIDER_NAMES = [
    "webhook_service",
    "webhook_calendar_side_effects_service",
    "webhook_membership_side_effects_service",
]


@pytest.mark.parametrize("name", WEBHOOK_PROVIDER_NAMES)
def test_app_container_alias_is_webhooks_container_provider(name: str) -> None:
    assert getattr(AppContainer, name) is getattr(WebhooksContainer, name)


def test_webhooks_container_resolves_on_its_own() -> None:
    container = WebhooksContainer()

    assert isinstance(container.webhook_service(), WebhookService)
    assert isinstance(
        container.webhook_calendar_side_effects_service(),
        WebhookCalendarEventSideEffectsService,
    )
    assert isinstance(
        container.webhook_membership_side_effects_service(),
        WebhookMembershipSideEffectsService,
    )


def test_webhook_service_receives_the_billing_entitlement_service() -> None:
    container = AppContainer()
    sentinel = object()

    with container.entitlement_service.override(providers.Object(sentinel)):
        assert container.webhook_service().entitlement_service is sentinel


def test_side_effects_services_share_the_webhook_service_provider() -> None:
    container = AppContainer()
    sentinel = object()

    with container.webhook_service.override(providers.Object(sentinel)):
        assert container.webhook_calendar_side_effects_service().webhook_service is sentinel
        assert container.webhook_membership_side_effects_service().webhook_service is sentinel
