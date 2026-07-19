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

from audit.repositories import DjangoORMAuditRepository
from audit.services import AuditService
from calendar_integration.services.bookable_slots_service import BookableSlotsService
from calendar_integration.services.booking_policy_permission_service import (
    BookingPolicyPermissionService,
)
from calendar_integration.services.booking_policy_service import BookingPolicyService
from calendar_integration.services.calendar_group_service import CalendarGroupService
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.calendar_side_effects_service import CalendarSideEffectsService
from calendar_integration.services.external_event_change_request_service import (
    ExternalEventChangeRequestService,
)
from legal.services import ConsentService
from notifications.notification_adapters.django_in_app import DjangoInAppNotificationAdapter
from notifications.notification_template_renderers.django_in_app_renderer import (
    DjangoTemplatedInAppRenderer,
)
from organizations.services import OrganizationService
from payments.constants import PaymentProviders
from payments.services.payment_adapters.mercadopago_payment_adapter import MercadoPagoPaymentAdapter
from payments.services.payment_adapters.stripe_payment_adapter import StripePaymentAdapter
from payments.services.payment_service import PaymentService
from payments.services.subscription_adapters.mercadopago_subscription_adapter import (
    MercadoPagoSubscriptionAdapter,
)
from payments.services.subscription_adapters.stripe_subscription_adapter import (
    StripeSubscriptionAdapter,
)
from payments.services.subscription_plan_factory.billing_plan_factory import BillingPlanFactory
from payments.services.subscription_service import SubscriptionService
from public_api.services import PublicAPIAuthService
from vintasend_django_sms_template_renderer.services.notification_template_renderers.django_sms_template_renderer import (
    DjangoTemplatedSMSRenderer,
)
from vintasend_twilio.services.notification_adapters.twilio import (
    TwilioSMSNotificationAdapter,
)
from webhooks.services import (
    WebhookCalendarEventSideEffectsService,
    WebhookMembershipSideEffectsService,
    WebhookService,
)


class AppContainer(containers.DeclarativeContainer):
    config = providers.Configuration()

    audit_repository = providers.Singleton(DjangoORMAuditRepository)
    audit_service = providers.Factory(AuditService)

    payment_gateway = providers.Factory(
        MercadoPagoPaymentAdapter,
        access_token=config.MERCADOPAGO_ACCESS_TOKEN,
        webhook_secret=config.MERCADOPAGO_WEBHOOK_SECRET,
    )
    subscription_gateway = providers.Factory(
        MercadoPagoSubscriptionAdapter,
        access_token=config.MERCADOPAGO_ACCESS_TOKEN,
        webhook_secret=config.MERCADOPAGO_WEBHOOK_SECRET,
    )

    #: Registered so the `payment_provider_registry`/`subscription_provider_registry`
    #: `provider` URL kwarg can select Stripe, and so the adapter conformance
    #: suite can exercise it — no organization is routed onto Stripe yet (that's
    #: Phase 9's job).
    stripe_payment_gateway = providers.Factory(
        StripePaymentAdapter,
        api_key=config.STRIPE_SECRET_KEY,
        webhook_secret=config.STRIPE_WEBHOOK_SECRET,
    )
    stripe_subscription_gateway = providers.Factory(
        StripeSubscriptionAdapter,
        api_key=config.STRIPE_SECRET_KEY,
        webhook_secret=config.STRIPE_WEBHOOK_SECRET,
    )

    #: Selects the payment/subscription adapter by provider slug (the `provider`
    #: URL kwarg on the payment webhook views). A future provider registers here
    #: rather than the webhook views or `PaymentService` hardcoding a single
    #: provider.
    payment_provider_registry = providers.Dict(
        {
            PaymentProviders.MERCADOPAGO: payment_gateway,
            PaymentProviders.STRIPE: stripe_payment_gateway,
        }
    )
    subscription_provider_registry = providers.Dict(
        {
            PaymentProviders.MERCADOPAGO: subscription_gateway,
            PaymentProviders.STRIPE: stripe_subscription_gateway,
        }
    )

    subscription_plan_factory = providers.Factory(
        BillingPlanFactory,
    )

    payment_service = providers.Factory(
        PaymentService,
        subscription_plan_factory=subscription_plan_factory,
        payment_gateway=payment_gateway,
        subscription_gateway=subscription_gateway,
        payment_provider_registry=payment_provider_registry,
        subscription_provider_registry=subscription_provider_registry,
    )

    subscription_service = providers.Factory(
        SubscriptionService,
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
            DjangoInAppNotificationAdapter(
                DjangoTemplatedInAppRenderer(),
                DjangoDbNotificationBackend(),
            ),
        ],
        notification_backend=DjangoDbNotificationBackend(),
    )

    webhook_service = providers.Factory(
        WebhookService,
    )

    webhook_calendar_side_effects_service = providers.Factory(
        WebhookCalendarEventSideEffectsService,
        webhook_service=webhook_service,
    )

    webhook_membership_side_effects_service = providers.Factory(
        WebhookMembershipSideEffectsService,
        webhook_service=webhook_service,
    )

    calendar_side_effects_service = providers.Factory(
        CalendarSideEffectsService,
        side_effects_pipeline=(webhook_calendar_side_effects_service,),
    )

    calendar_permission_service = providers.Factory(
        CalendarPermissionService,
        audit_service=audit_service,
    )

    external_event_change_request_service = providers.Factory(
        ExternalEventChangeRequestService,
        audit_service=audit_service,
        notification_service=notification_service,
    )

    booking_policy_service = providers.Factory(
        BookingPolicyService,
        audit_service=audit_service,
    )

    booking_policy_permission_service = providers.Factory(
        BookingPolicyPermissionService,
    )

    calendar_service = providers.Factory(
        CalendarService,
        calendar_side_effects_service=calendar_side_effects_service,
        calendar_permission_service=calendar_permission_service,
        audit_service=audit_service,
        external_event_change_request_service=external_event_change_request_service,
        booking_policy_service=booking_policy_service,
    )

    bookable_slots_service = providers.Factory(
        BookableSlotsService,
        booking_policy_service=booking_policy_service,
    )

    calendar_group_service = providers.Factory(
        CalendarGroupService,
        calendar_service=calendar_service,
        calendar_permission_service=calendar_permission_service,
        audit_service=audit_service,
        booking_policy_service=booking_policy_service,
    )

    organization_service = providers.Factory(
        OrganizationService,
        calendar_service=calendar_service,
        webhook_membership_side_effects_service=webhook_membership_side_effects_service,
        audit_service=audit_service,
        subscription_service=subscription_service,
    )

    public_api_auth_service = providers.Factory(
        PublicAPIAuthService,
        audit_service=audit_service,
    )

    consent_service = providers.Factory(
        ConsentService,
        audit_service=audit_service,
    )


container: AppContainer | None = None  # set during app startup
