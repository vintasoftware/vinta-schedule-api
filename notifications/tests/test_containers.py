from di_core.containers import AppContainer
from notifications.containers import NotificationsContainer


def test_notification_service_same_instance_across_resolutions() -> None:
    container = AppContainer()

    service1 = container.notification_service()
    service2 = container.notification_service()

    assert service1 is service2


def test_notification_service_is_singleton_from_notifications_container() -> None:
    container = NotificationsContainer()

    service1 = container.notification_service()
    service2 = container.notification_service()

    assert service1 is service2


def test_notification_service_same_provider_from_both_containers() -> None:
    """The alias in AppContainer points to the same provider object."""
    assert AppContainer.notification_service is NotificationsContainer.notification_service
