from dependency_injector import containers, providers
from vinta_billing.constants import PaymentProviders
from vinta_billing.services.cycle_close_service import CycleCloseService
from vinta_billing.services.dunning_service import DunningService
from vinta_billing.services.entitlement_service import EntitlementService
from vinta_billing.services.metering_service import MeteringService
from vinta_billing.services.payment_adapters.mercadopago_payment_adapter import (
    MercadoPagoPaymentAdapter,
)
from vinta_billing.services.payment_adapters.stripe_payment_adapter import StripePaymentAdapter
from vinta_billing.services.payment_provider_resolver import PaymentProviderResolver
from vinta_billing.services.payment_service import PaymentService
from vinta_billing.services.subscription_adapters.mercadopago_subscription_adapter import (
    MercadoPagoSubscriptionAdapter,
)
from vinta_billing.services.subscription_adapters.stripe_subscription_adapter import (
    StripeSubscriptionAdapter,
)
from vinta_billing.services.subscription_plan_factory.billing_plan_factory import (
    BillingPlanFactory,
)
from vinta_billing.services.subscription_service import SubscriptionService
from vinta_billing.services.usage_warning_service import UsageWarningService
from vintasend.services.notification_service import NotificationService
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
from calendar_integration.services.external_client_identifier_service import (
    ExternalClientIdentifierService,
)
from calendar_integration.services.external_event_change_request_service import (
    ExternalEventChangeRequestService,
)
from legal.services import ConsentService
from notifications.notification_adapters.django_email import (
    ReplyToDjangoEmailNotificationAdapter,
)
from notifications.notification_adapters.django_in_app import DjangoInAppNotificationAdapter
from notifications.notification_template_renderers.django_in_app_renderer import (
    DjangoTemplatedInAppRenderer,
)
from organizations.services import OrganizationService
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
    #: suite can exercise it. `DEFAULT_PAYMENT_PROVIDER` is `stripe`, so every
    #: unpinned organization routes onto this adapter.
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

    #: Single source of the pin -> default provider resolution rule -- shared by the
    #: provider-credentials endpoints (`vinta_billing.views.PaymentProviderViewSet`,
    #: resolved through `VINTA_BILLING['SERVICE_CONTAINER']`) and
    #: `PaymentService`'s charge-routing (`create_payment`/`create_subscription`). No
    #: adapter dependency, so it does not need the `payment_gateway`/`subscription_gateway`
    #: providers above.
    payment_provider_resolver = providers.Factory(
        PaymentProviderResolver,
    )

    #: `PaymentService` resolves every adapter through the two registries above --
    #: it does not take the singular `payment_gateway`/`subscription_gateway`
    #: providers directly. Those providers stay
    #: registered because the registries above are built from them.
    payment_service = providers.Factory(
        PaymentService,
        subscription_plan_factory=subscription_plan_factory,
        payment_provider_resolver=payment_provider_resolver,
        payment_provider_registry=payment_provider_registry,
        subscription_provider_registry=subscription_provider_registry,
    )

    #: `payment_provider_resolver` is injected here too (not only into
    #: `PaymentService`): `create_subscription_for_organization` stamps the
    #: organization's resolved provider onto the one `Subscription` it will ever
    #: have, which is the row every later subscription operation resolves its
    #: adapter from.
    #: No `audit_service` here, unlike every other audited service below:
    #: `SubscriptionService` is `vinta_billing`'s now, and a library cannot take
    #: this project's audit service as a constructor argument. It publishes
    #: `vinta_billing.signals.payment_provider_repointed` at the same point the
    #: inline `audit_service.record(...)` used to sit, and
    #: `payments/seams/audit.py` receives it. Passing the kwarg would be a
    #: `TypeError` at first resolution.
    subscription_service = providers.Factory(
        SubscriptionService,
        payment_service=payment_service,
        payment_provider_resolver=payment_provider_resolver,
    )

    entitlement_service = providers.Factory(
        EntitlementService,
    )

    metering_service = providers.Factory(
        MeteringService,
        entitlement_service=entitlement_service,
    )

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

    dunning_service = providers.Factory(
        DunningService,
        subscription_service=subscription_service,
        entitlement_service=entitlement_service,
        notification_service=notification_service,
    )

    usage_warning_service = providers.Factory(
        UsageWarningService,
        entitlement_service=entitlement_service,
        notification_service=notification_service,
    )

    cycle_close_service = providers.Factory(
        CycleCloseService,
        metering_service=metering_service,
        subscription_service=subscription_service,
        payment_service=payment_service,
        entitlement_service=entitlement_service,
    )

    webhook_service = providers.Factory(
        WebhookService,
        entitlement_service=entitlement_service,
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
        # providers.List, not a plain tuple. dependency_injector only resolves a
        # provider passed as a direct kwarg value; one nested inside a tuple is
        # handed to the constructor as the Provider object itself. The pipeline
        # then held a Factory instead of a handler, every
        # ``isinstance(handler, On*Handler)`` check in CalendarSideEffectsService
        # returned False, and no calendar event webhook ever dispatched.
        side_effects_pipeline=providers.List(webhook_calendar_side_effects_service),
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

    external_client_identifier_service = providers.Factory(
        ExternalClientIdentifierService,
    )

    calendar_service = providers.Factory(
        CalendarService,
        calendar_side_effects_service=calendar_side_effects_service,
        calendar_permission_service=calendar_permission_service,
        audit_service=audit_service,
        external_event_change_request_service=external_event_change_request_service,
        booking_policy_service=booking_policy_service,
        entitlement_service=entitlement_service,
        external_client_identifier_service=external_client_identifier_service,
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
        entitlement_service=entitlement_service,
    )

    organization_service = providers.Factory(
        OrganizationService,
        calendar_service=calendar_service,
        webhook_membership_side_effects_service=webhook_membership_side_effects_service,
        audit_service=audit_service,
        subscription_service=subscription_service,
        entitlement_service=entitlement_service,
    )

    public_api_auth_service = providers.Factory(
        PublicAPIAuthService,
        audit_service=audit_service,
        entitlement_service=entitlement_service,
    )

    consent_service = providers.Factory(
        ConsentService,
        audit_service=audit_service,
    )


container: AppContainer | None = None  # set during app startup
