from typing import TYPE_CHECKING, Generic, TypeVar

from django.conf import settings
from django.contrib.auth import get_user_model

from twilio.rest import Client
from vintasend.app_settings import NotificationSettings
from vintasend.constants import NotificationTypes
from vintasend.exceptions import NotificationSendError
from vintasend.services.dataclasses import Notification, OneOffNotification
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

    def _is_valid_phone(self, phone_number: str) -> bool:
        # Basic validation: check if the phone number is not empty and has at least 10 digits
        digits = [c for c in phone_number if c.isdigit()]
        return len(digits) >= 10

    def _clean_phone(self, phone_number: str) -> str:
        # Remove spaces, dashes, and parentheses
        return "".join(c for c in phone_number if c.isdigit() or c == "+")

    def send(
        self,
        notification: Notification | OneOffNotification,
        context: "NotificationContextDict",
        headers: dict[str, str] | None = None,
    ) -> None:
        """
        Send the notification to the user through email.

        :param notification: The notification to send.
        :param context: The context to render the notification templates.
        """
        notification_settings = NotificationSettings()

        if isinstance(notification, Notification):
            user = User.objects.get(id=notification.user_id)

            if not user.phone_number:
                raise NotificationSendError("User does not have a verified phone number.")

            phone_number = user.phone_number
        else:
            if not notification.email_or_phone:
                raise NotificationSendError("No phone number provided for one-off notification.")

            if not self._is_valid_phone(notification.email_or_phone):
                raise NotificationSendError("Invalid phone number format.")

            phone_number = self._clean_phone(notification.email_or_phone)

        context_with_base_url: NotificationContextDict = context.copy()
        context_with_base_url[
            "base_url"
        ] = f"{notification_settings.NOTIFICATION_DEFAULT_BASE_URL_PROTOCOL}://{notification_settings.NOTIFICATION_DEFAULT_BASE_URL_DOMAIN}"

        template = self.template_renderer.render(notification, context_with_base_url)

        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)

        client.messages.create(
            body=template.body,
            from_=settings.TWILIO_NUMBER,
            to=phone_number,
        )
