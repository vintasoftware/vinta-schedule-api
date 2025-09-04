from dependency_injector import containers, providers
from vintasend.services.notification_service import NotificationService
from vintasend_django.services.notification_adapters.django_email import (
    DjangoEmailNotificationAdapter,
)
from vintasend_django.services.notification_backends.django_db_notification_backend import (
    DjangoDbNotificationBackend,
)
from vintasend_django.services.notification_template_renderers.django_templated_email_renderer import (
    DjangoTemplatedEmailRenderer,
)

from calendar_integration.services.calendar_service import CalendarService
from organizations.organization_subscription_plan_factory import OrganizationSubscriptionPlanFactory
from payments.services.payment_adapters.mercadopago_payment_adapter import MercadoPagoPaymentAdapter
from payments.services.payment_service import PaymentService
from payments.services.subscription_adapters.mercadopago_subscription_adapter import (
    MercadoPagoSubscriptionAdapter,
)
from public_api.services import PublicAPIAuthService
from vintasend_django_sms_template_renderer.services.notification_template_renderers.django_sms_template_renderer import (
    DjangoTemplatedSMSRenderer,
)
from vintasend_twilio.services.notification_adapters.twilio import (
    TwilioSMSNotificationAdapter,
)
from webhooks.services import WebhookService


class AppContainer(containers.DeclarativeContainer):
    config = providers.Configuration()

    payment_gateway = providers.Factory(
        MercadoPagoPaymentAdapter,
        access_token=config.MERCADOPAGO_ACCESS_TOKEN,
    )
    subscription_gateway = providers.Factory(
        MercadoPagoSubscriptionAdapter,
        access_token=config.MERCADOPAGO_ACCESS_TOKEN,
    )

    subscription_plan_factory = providers.Factory(
        OrganizationSubscriptionPlanFactory,
    )

    payment_service = providers.Factory(
        PaymentService,
        subscription_plan_factory=subscription_plan_factory,
        payment_gateway=payment_gateway,
        subscription_gateway=subscription_gateway,
    )

    notification_service = providers.Singleton(
        NotificationService[
            DjangoEmailNotificationAdapter[
                DjangoDbNotificationBackend, DjangoTemplatedEmailRenderer
            ],
            DjangoDbNotificationBackend,
        ],
        notification_adapters=[
            DjangoEmailNotificationAdapter(
                DjangoTemplatedEmailRenderer(),
                DjangoDbNotificationBackend(),
            ),
            TwilioSMSNotificationAdapter(
                DjangoTemplatedSMSRenderer(),
                DjangoDbNotificationBackend(),
            ),
        ],
        notification_backend=DjangoDbNotificationBackend(),
    )

    webhook_service = providers.Factory(
        WebhookService,
    )

    calendar_service = providers.Factory(
        CalendarService,
        webhook_service=webhook_service,
    )

    public_api_auth_service = providers.Factory(
        PublicAPIAuthService,
    )


container: AppContainer | None = None  # set during app startup
