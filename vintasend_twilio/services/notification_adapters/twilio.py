from typing import TYPE_CHECKING, Generic, TypeVar

from django.conf import settings
from django.contrib.auth import get_user_model

from twilio.rest import Client
from vintasend.app_settings import NotificationSettings
from vintasend.constants import NotificationTypes
from vintasend.exceptions import NotificationSendError
from vintasend.services.dataclasses import Notification
from vintasend.services.notification_adapters.base import BaseNotificationAdapter
from vintasend.services.notification_backends.base import BaseNotificationBackend

from vintasend_django_sms_template_renderer.services.notification_template_renderers.base_sms_template_renderer import (
    BaseTemplatedSMSRenderer,
)


if TYPE_CHECKING:
    from vintasend.services.notification_service import NotificationContextDict


User = get_user_model()


B = TypeVar("B", bound=BaseNotificationBackend)
T = TypeVar("T", bound=BaseTemplatedSMSRenderer)


class TwilioSMSNotificationAdapter(Generic[B, T], BaseNotificationAdapter[B, T]):
    notification_type = NotificationTypes.SMS

    def send(
        self,
        notification: Notification,
        context: "NotificationContextDict",
        headers: dict[str, str] | None = None,
    ) -> None:
        """
        Send the notification to the user through email.

        :param notification: The notification to send.
        :param context: The context to render the notification templates.
        """
        notification_settings = NotificationSettings()

        user = User.objects.get(id=notification.user_id)

        if not user.phone_number:
            raise NotificationSendError("User does not have a verified phone number.")

        context_with_base_url: NotificationContextDict = context.copy()
        context_with_base_url[
            "base_url"
        ] = f"{notification_settings.NOTIFICATION_DEFAULT_BASE_URL_PROTOCOL}://{notification_settings.NOTIFICATION_DEFAULT_BASE_URL_DOMAIN}"

        template = self.template_renderer.render(notification, context_with_base_url)

        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)

        client.messages.create(
            body=template.body,
            from_=settings.TWILIO_NUMBER,
            to=user.phone_number,
        )
