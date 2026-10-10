from dependency_injector import providers
from vintasend.services.notification_service import NotificationService
from vintasend_django.services.notification_backends.django_db_notification_backend import (
    DjangoDbNotificationBackend,
)
from vintasend_django.services.notification_template_renderers.django_templated_email_renderer import (
    DjangoTemplatedEmailRenderer,
)

from di_core.base import BaseContainer
from notifications.notification_adapters.django_email import (
    ReplyToDjangoEmailNotificationAdapter,
)
from notifications.notification_adapters.django_in_app import DjangoInAppNotificationAdapter
from notifications.notification_template_renderers.django_in_app_renderer import (
    DjangoTemplatedInAppRenderer,
)
from vintasend_django_sms_template_renderer.services.notification_template_renderers.django_sms_template_renderer import (
    DjangoTemplatedSMSRenderer,
)
from vintasend_twilio.services.notification_adapters.twilio import (
    TwilioSMSNotificationAdapter,
)


class NotificationsContainer(BaseContainer):
    """Providers for outbound notifications (vintasend)."""

    notification_service = providers.Singleton(
        NotificationService[
            ReplyToDjangoEmailNotificationAdapter[
                DjangoDbNotificationBackend, DjangoTemplatedEmailRenderer
            ],
            DjangoDbNotificationBackend,
        ],
        notification_adapters=[
            ReplyToDjangoEmailNotificationAdapter(
                DjangoTemplatedEmailRenderer(),
                DjangoDbNotificationBackend(),
            ),
            TwilioSMSNotificationAdapter(
                DjangoTemplatedSMSRenderer(),
                DjangoDbNotificationBackend(),
            ),
            DjangoInAppNotificationAdapter(
                DjangoTemplatedInAppRenderer(),
                DjangoDbNotificationBackend(),
            ),
        ],
        notification_backend=DjangoDbNotificationBackend(),
    )
