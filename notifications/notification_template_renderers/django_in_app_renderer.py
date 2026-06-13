from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.template.loader import render_to_string

from vintasend.exceptions import NotificationBodyTemplateRenderingError
from vintasend.services.dataclasses import Notification, OneOffNotification
from vintasend.services.notification_template_renderers.base import (
    BaseNotificationTemplateRenderer,
    NotificationSendInput,
)


if TYPE_CHECKING:
    from vintasend.services.notification_service import NotificationContextDict


@dataclass
class RenderedInAppNotification(NotificationSendInput):
    """Represents a rendered in-app notification ready for persistence."""

    body: str


class DjangoTemplatedInAppRenderer(BaseNotificationTemplateRenderer):
    """
    Renders in-app notification templates using Django's template engine.

    Renders only the body template (single-template, similar to the SMS renderer).
    The title is stored as-is on the notification model.
    """

    def render(
        self,
        notification: "Notification | OneOffNotification",
        context: "NotificationContextDict",
    ) -> RenderedInAppNotification:
        """
        Render the notification body template with the given context.

        :param notification: The notification to render.
        :param context: The context dict to render the template with.
        :returns: A RenderedInAppNotification with the rendered body.
        :raises NotificationBodyTemplateRenderingError: When body template rendering fails.
        """
        body_template = notification.body_template

        try:
            body = render_to_string(body_template, context)
        except Exception as e:  # noqa: BLE001
            raise NotificationBodyTemplateRenderingError("Failed to render body template") from e

        return RenderedInAppNotification(body=body)
