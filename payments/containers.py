from dependency_injector import providers
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

from di_core.base import BaseContainer
from notifications.containers import NotificationsContainer


class BillingContainer(NotificationsContainer):
    """Providers for the vinta-django-billing services."""

    payment_gateway = providers.Factory(
        MercadoPagoPaymentAdapter,
        access_token=BaseContainer.config.MERCADOPAGO_ACCESS_TOKEN,
        webhook_secret=BaseContainer.config.MERCADOPAGO_WEBHOOK_SECRET,
    )
    subscription_gateway = providers.Factory(
        MercadoPagoSubscriptionAdapter,
        access_token=BaseContainer.config.MERCADOPAGO_ACCESS_TOKEN,
        webhook_secret=BaseContainer.config.MERCADOPAGO_WEBHOOK_SECRET,
    )

    #: Registered so the `payment_provider_registry`/`subscription_provider_registry`
    #: `provider` URL kwarg can select Stripe, and so the adapter conformance
    #: suite can exercise it. `DEFAULT_PAYMENT_PROVIDER` is `stripe`, so every
    #: unpinned organization routes onto this adapter.
    stripe_payment_gateway = providers.Factory(
        StripePaymentAdapter,
        api_key=BaseContainer.config.STRIPE_SECRET_KEY,
        webhook_secret=BaseContainer.config.STRIPE_WEBHOOK_SECRET,
    )
    stripe_subscription_gateway = providers.Factory(
        StripeSubscriptionAdapter,
        api_key=BaseContainer.config.STRIPE_SECRET_KEY,
        webhook_secret=BaseContainer.config.STRIPE_WEBHOOK_SECRET,
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

    dunning_service = providers.Factory(
        DunningService,
        subscription_service=subscription_service,
        entitlement_service=entitlement_service,
        notification_service=NotificationsContainer.notification_service,
    )

    usage_warning_service = providers.Factory(
        UsageWarningService,
        entitlement_service=entitlement_service,
        notification_service=NotificationsContainer.notification_service,
    )

    cycle_close_service = providers.Factory(
        CycleCloseService,
        metering_service=metering_service,
        subscription_service=subscription_service,
        payment_service=payment_service,
        entitlement_service=entitlement_service,
    )
