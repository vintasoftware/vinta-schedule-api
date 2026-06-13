from typing import TYPE_CHECKING, TypeVar

from vintasend.constants import NotificationTypes
from vintasend.services.notification_adapters.base import BaseNotificationAdapter
from vintasend.services.notification_backends.base import BaseNotificationBackend

from notifications.notification_template_renderers.django_in_app_renderer import (
    DjangoTemplatedInAppRenderer,
)


if TYPE_CHECKING:
    from vintasend.services.dataclasses import Notification, OneOffNotification
    from vintasend.services.notification_service import NotificationContextDict


B = TypeVar("B", bound=BaseNotificationBackend)
T = TypeVar("T", bound=DjangoTemplatedInAppRenderer)


class DjangoInAppNotificationAdapter(BaseNotificationAdapter[B, T]):
    """
    In-app notification adapter for Django.

    Renders the body template on send. Persistence and status transitions
    (PENDING_SEND → SENT) are handled by the DjangoDbNotificationBackend.

    This adapter does not deliver through any external channel — it only validates
    that the template renders correctly. The rendered body is later read from the
    persisted context_used on the Notification model.
    """

    notification_type = NotificationTypes.IN_APP

    def send(
        self,
        notification: "Notification | OneOffNotification",
        context: "NotificationContextDict",
    ) -> None:
        """
        Render the body template and validate that it produces output.

        The notification is already persisted before send() is called.
        DjangoDbNotificationBackend.mark_pending_as_sent() transitions the status
        after this method returns without raising.

        :param notification: The notification to send.
        :param context: The context dict generated from the registered context function.
        :raises NotificationBodyTemplateRenderingError: When body template rendering fails.
        """
        self.template_renderer.render(notification, context)
