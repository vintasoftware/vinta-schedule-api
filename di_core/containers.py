from dependency_injector import providers

from audit_integration.containers import AuditContainer
from audit_integration.repositories import OrganizationAuditRepository
from audit_integration.services import OrganizationAuditService
from calendar_integration.containers import CalendarContainer
from calendar_integration.services.appointment_type_service import AppointmentTypeService
from calendar_integration.services.bookable_slots_service import BookableSlotsService
from calendar_integration.services.booking_policy_permission_service import (
    BookingPolicyPermissionService,
)
from calendar_integration.services.booking_policy_service import BookingPolicyService
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.calendar_side_effects_service import CalendarSideEffectsService
from calendar_integration.services.external_client_identifier_service import (
    ExternalClientIdentifierService,
)
from calendar_integration.services.external_event_change_request_service import (
    ExternalEventChangeRequestService,
)
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
    #: The audit log's system of record. Every audit record is written here
    #: first, and this is what `AuditService` reads from unless a caller names
    #: another repository.
    audit_repository = providers.Singleton(OrganizationAuditRepository)

    #: Extra audit backends, keyed by the alias callers pass as
    #: `repository="..."` to the `AuditService` read methods and as the target
    #: of `sync_repository`. Empty by default: the ORM repository is the only
    #: one this project runs. Add an entry (a search index, a warehouse loader,
    #: an archive) and every record starts being replicated there, best effort,
    #: on top of the main write -- with `vinta_audit_logs.tasks.sync_audit_repository`
    #: available to backfill whatever replication missed.
    #:
    #: Do NOT register "main" here; the key belongs to `audit_repository` and
    #: `AuditService` drops it.
    audit_additional_repositories = providers.Dict({})

    #: `AuditService` takes its repositories as constructor arguments rather
    #: than resolving them through `@inject`: `vinta_audit_logs` is meant to be
    #: installed by projects that may not use dependency_injector at all, so the
    #: wiring lives here instead of in the package.
    audit_service = providers.Factory(
        OrganizationAuditService,
        repository=audit_repository,
        additional_repositories=audit_additional_repositories,
    )

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

    appointment_type_service = providers.Factory(
        AppointmentTypeService,
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
