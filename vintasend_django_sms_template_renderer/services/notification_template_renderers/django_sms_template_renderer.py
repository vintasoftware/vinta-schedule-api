from typing import TYPE_CHECKING

from django.template.loader import render_to_string

from vintasend.exceptions import NotificationBodyTemplateRenderingError
from vintasend.services.dataclasses import Notification

from vintasend_django_sms_template_renderer.services.notification_template_renderers.base_sms_template_renderer import (
    BaseTemplatedSMSRenderer,
    TemplatedSMS,
)


if TYPE_CHECKING:
    from vintasend.services.notification_service import NotificationContextDict


class DjangoTemplatedSMSRenderer(BaseTemplatedSMSRenderer):
    def render(
        self, notification: Notification, context: "NotificationContextDict"
    ) -> TemplatedSMS:
        body_template = notification.body_template

        try:
            body = render_to_string(body_template, context)
        except Exception as e:  # noqa: BLE001
            raise NotificationBodyTemplateRenderingError("Failed to render body template") from e

        return TemplatedSMS(body=body)
