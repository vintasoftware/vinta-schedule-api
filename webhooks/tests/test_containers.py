from di_core.containers import AppContainer
from webhooks.containers import WebhooksContainer


def test_webhook_service_accessible_from_app_container() -> None:
    assert hasattr(AppContainer, "webhook_service")


def test_webhook_calendar_side_effects_service_accessible_from_app_container() -> None:
    assert hasattr(AppContainer, "webhook_calendar_side_effects_service")


def test_webhook_membership_side_effects_service_accessible_from_app_container() -> None:
    assert hasattr(AppContainer, "webhook_membership_side_effects_service")


def test_side_effects_services_receive_webhook_service() -> None:
    """Both side-effects services receive webhook_service in their constructor."""
    container = AppContainer()

    calendar_side_effects = container.webhook_calendar_side_effects_service()
    membership_side_effects = container.webhook_membership_side_effects_service()

    assert hasattr(calendar_side_effects, "webhook_service")
    assert hasattr(membership_side_effects, "webhook_service")


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
