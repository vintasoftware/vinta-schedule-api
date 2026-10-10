from dependency_injector import providers

from audit_integration.containers import AuditContainer
from calendar_integration.containers import CalendarContainer
from legal.containers import LegalContainer
from legal.services import ConsentService
from notifications.containers import NotificationsContainer
from organizations.containers import OrganizationsContainer
from organizations.services import OrganizationService
from payments.containers import BillingContainer
from public_api.containers import PublicApiContainer
from public_api.services import PublicAPIAuthService
from webhooks.containers import WebhooksContainer


class AppContainer(
    OrganizationsContainer,
    CalendarContainer,
    PublicApiContainer,
    WebhooksContainer,
    LegalContainer,
    BillingContainer,
    NotificationsContainer,
    AuditContainer,
):
    audit_repository = AuditContainer.audit_repository
    audit_additional_repositories = AuditContainer.audit_additional_repositories
    audit_service = AuditContainer.audit_service

    payment_gateway = BillingContainer.payment_gateway
    subscription_gateway = BillingContainer.subscription_gateway
    stripe_payment_gateway = BillingContainer.stripe_payment_gateway
    stripe_subscription_gateway = BillingContainer.stripe_subscription_gateway
    payment_provider_registry = BillingContainer.payment_provider_registry
    subscription_provider_registry = BillingContainer.subscription_provider_registry
    subscription_plan_factory = BillingContainer.subscription_plan_factory
    payment_provider_resolver = BillingContainer.payment_provider_resolver
    payment_service = BillingContainer.payment_service
    subscription_service = BillingContainer.subscription_service
    entitlement_service = BillingContainer.entitlement_service
    metering_service = BillingContainer.metering_service

    notification_service = NotificationsContainer.notification_service
    dunning_service = BillingContainer.dunning_service
    usage_warning_service = BillingContainer.usage_warning_service
    cycle_close_service = BillingContainer.cycle_close_service

    webhook_service = WebhooksContainer.webhook_service
    webhook_calendar_side_effects_service = WebhooksContainer.webhook_calendar_side_effects_service
    webhook_membership_side_effects_service = (
        WebhooksContainer.webhook_membership_side_effects_service
    )

    calendar_side_effects_service = CalendarContainer.calendar_side_effects_service
    calendar_permission_service = CalendarContainer.calendar_permission_service
    external_event_change_request_service = CalendarContainer.external_event_change_request_service
    booking_policy_service = CalendarContainer.booking_policy_service
    booking_policy_permission_service = CalendarContainer.booking_policy_permission_service
    external_client_identifier_service = CalendarContainer.external_client_identifier_service
    calendar_service = CalendarContainer.calendar_service
    bookable_slots_service = CalendarContainer.bookable_slots_service
    appointment_type_service = CalendarContainer.appointment_type_service

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


def get_container() -> AppContainer:
    """The wired container, or a clear error if the app has not started yet.

    ``container`` is ``AppContainer | None`` because ``DICoreConfig.ready()`` is what
    assigns it (see ``di_core/apps.py``), so reading the global directly is only
    type-correct after narrowing. Most call sites never had to: they sit in functions
    with no annotations, whose bodies mypy skips by default. The ones that *are*
    annotated -- test fixtures declaring a return type -- had no way to say "startup has
    run" other than an assert each.

    Prefer this over importing the global in any annotated code.
    """
    if container is None:
        raise RuntimeError(
            "The DI container is not wired yet. It is set by DICoreConfig.ready(); "
            "this ran before django.setup() completed."
        )
    return container
