"""GraphQL mutations for calendar integration webhook management."""

import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, cast

from django.core.exceptions import PermissionDenied
from django.db import transaction

import strawberry
from allauth.socialaccount.models import SocialAccount
from dependency_injector.wiring import Provide, inject
from graphql import GraphQLError
from vinta_billing.exceptions import OverLimitError

from calendar_integration.constants import ExternalEventChangeRequestStatus
from calendar_integration.exceptions import (
    AppointmentTypeError,
    AppointmentTypeValidationError,
    BookingPolicyViolationError,
    ChangeRequestIneligibleError,
    ChangeRequestNotPendingError,
    EventManagementError,
    InvalidTokenError,
    NoAvailableTimeWindowsError,
    PermissionServiceInitializationError,
    TokenAlreadyUsedError,
    TokenExpiredError,
    TokenRevokedError,
)
from calendar_integration.graphql import (
    AppointmentTypeGraphQLType,
    ApproveExternalEventChangeRequestResult,
    BookingCodeErrorCode,
    BookingCodeResult,
    CalendarEventGraphQLType,
    CalendarWebhookSubscriptionGraphQLType,
    CodeEventResult,
    RejectExternalEventChangeRequestResult,
)
from calendar_integration.models import (
    AppointmentType,
    Calendar,
    CalendarEvent,
    CalendarOwnership,
    EventManagementPermissions,
    ExternalEventChangeRequest,
)
from calendar_integration.services.dataclasses import (
    AppointmentTypeEventInputData,
    AppointmentTypeInputData,
    AppointmentTypeSlotInputData,
    AppointmentTypeSlotSelectionInputData,
    CalendarEventInputData,
    EventAttendanceInputData,
    EventExternalAttendanceInputData,
    ExternalAttendeeInputData,
    ResourceAllocationInputData,
)
from calendar_integration.services.external_event_change_request_service import (
    ExternalEventChangeRequestService,
)
from calendar_integration.services.webhook_analytics_service import WebhookAnalyticsService
from organizations.models import Organization, OrganizationMembership
from public_api.extensions import raise_over_limit_graphql_error
from public_api.permissions import IsAuthenticated, OrganizationResourceAccess


if TYPE_CHECKING:
    from calendar_integration.services.appointment_type_service import AppointmentTypeService
    from calendar_integration.services.calendar_permission_service import CalendarPermissionService
    from calendar_integration.services.calendar_service import CalendarService


@dataclass
class WebhookMutationDependencies:
    """Dependencies for webhook mutations."""

    calendar_service: "CalendarService"


@inject
def get_webhook_mutation_dependencies(
    calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
) -> WebhookMutationDependencies:
    """Get webhook mutation dependencies from DI container."""
    required_dependencies = [calendar_service]
    if any(dep is None for dep in required_dependencies):
        raise GraphQLError(
            f"Missing required dependency {', '.join([str(dep) for dep in required_dependencies if dep is None])}"
        )

    return WebhookMutationDependencies(
        calendar_service=cast("CalendarService", calendar_service),
    )


@strawberry.type
class WebhookSubscriptionResult:
    """Result type for webhook subscription operations."""

    success: bool
    subscription: CalendarWebhookSubscriptionGraphQLType | None = None
    error_message: str | None = None


@strawberry.type
class WebhookDeleteResult:
    """Result type for webhook deletion operations."""

    success: bool
    error_message: str | None = None


@strawberry.type
class WebhookCleanupResult:
    """Result type for webhook cleanup operations."""

    success: bool
    deleted_count: int
    error_message: str | None = None


@strawberry.input
class CreateWebhookSubscriptionInput:
    """Input type for creating webhook subscriptions."""

    organization_id: int
    calendar_id: int


@strawberry.input
class DeleteWebhookSubscriptionInput:
    """Input type for deleting webhook subscriptions."""

    organization_id: int
    subscription_id: int


@strawberry.input
class RefreshWebhookSubscriptionInput:
    """Input type for refreshing webhook subscriptions."""

    organization_id: int
    subscription_id: int


@strawberry.input
class CleanupWebhookEventsInput:
    """Input type for cleaning up old webhook events."""

    organization_id: int
    days_to_keep: int = 30


@strawberry.type
class CalendarWebhookMutations:
    """Calendar webhook GraphQL mutations."""

    @strawberry.mutation
    def create_webhook_subscription(
        self,
        input: CreateWebhookSubscriptionInput,  # noqa: A002
    ) -> WebhookSubscriptionResult:
        """Create a new webhook subscription for a calendar."""
        deps = get_webhook_mutation_dependencies()

        try:
            organization = Organization.objects.get(id=input.organization_id)
        except Organization.DoesNotExist:
            return WebhookSubscriptionResult(success=False, error_message="Organization not found")

        # Set organization context on service
        deps.calendar_service.organization = organization

        try:
            # Get the calendar first
            try:
                calendar = Calendar.objects.filter_by_organization(organization).get(
                    id=input.calendar_id,
                )
            except Calendar.DoesNotExist:
                return WebhookSubscriptionResult(success=False, error_message="Calendar not found")

            subscription = deps.calendar_service.create_calendar_webhook_subscription(
                calendar=calendar
            )
            return WebhookSubscriptionResult(success=True, subscription=subscription)  # type: ignore
        except (ValueError, AttributeError, TypeError) as e:
            return WebhookSubscriptionResult(
                success=False, error_message=f"Failed to create subscription: {e!s}"
            )

    @strawberry.mutation
    def delete_webhook_subscription(
        self,
        input: DeleteWebhookSubscriptionInput,  # noqa: A002
    ) -> WebhookDeleteResult:
        """Delete a webhook subscription."""
        deps = get_webhook_mutation_dependencies()

        try:
            organization = Organization.objects.get(id=input.organization_id)
        except Organization.DoesNotExist:
            return WebhookDeleteResult(success=False, error_message="Organization not found")

        # Set organization context on service
        deps.calendar_service.organization = organization

        try:
            success = deps.calendar_service.delete_webhook_subscription(
                subscription_id=input.subscription_id
            )
            if success:
                return WebhookDeleteResult(success=True)
            else:
                return WebhookDeleteResult(success=False, error_message="Subscription not found")
        except (ValueError, AttributeError, TypeError) as e:
            return WebhookDeleteResult(
                success=False, error_message=f"Failed to delete subscription: {e!s}"
            )

    @strawberry.mutation
    def refresh_webhook_subscription(
        self,
        input: RefreshWebhookSubscriptionInput,  # noqa: A002
    ) -> WebhookSubscriptionResult:
        """Refresh/renew a webhook subscription."""
        deps = get_webhook_mutation_dependencies()

        try:
            organization = Organization.objects.get(id=input.organization_id)
        except Organization.DoesNotExist:
            return WebhookSubscriptionResult(success=False, error_message="Organization not found")

        # Set organization context on service
        deps.calendar_service.organization = organization

        try:
            subscription = deps.calendar_service.refresh_webhook_subscription(
                subscription_id=input.subscription_id
            )
            if subscription:
                return WebhookSubscriptionResult(success=True, subscription=subscription)  # type: ignore
            else:
                return WebhookSubscriptionResult(
                    success=False, error_message="Subscription not found"
                )
        except (ValueError, AttributeError, TypeError) as e:
            return WebhookSubscriptionResult(
                success=False, error_message=f"Failed to refresh subscription: {e!s}"
            )

    @strawberry.mutation
    def cleanup_webhook_events(
        self,
        input: CleanupWebhookEventsInput,  # noqa: A002
    ) -> WebhookCleanupResult:
        """Clean up old webhook events."""
        try:
            organization = Organization.objects.get(id=input.organization_id)
        except Organization.DoesNotExist:
            return WebhookCleanupResult(
                success=False, deleted_count=0, error_message="Organization not found"
            )

        try:
            analytics_service = WebhookAnalyticsService(organization)
            deleted_count = analytics_service.cleanup_old_webhook_events(
                days_to_keep=input.days_to_keep
            )
            return WebhookCleanupResult(success=True, deleted_count=deleted_count)
        except (ValueError, AttributeError, TypeError) as e:
            return WebhookCleanupResult(
                success=False,
                deleted_count=0,
                error_message=f"Failed to cleanup events: {e!s}",
            )


# ---------------------------------------------------------------------------
# AppointmentType mutations
# ---------------------------------------------------------------------------


@dataclass
class AppointmentTypeMutationDependencies:
    """Dependencies for AppointmentType mutations."""

    appointment_type_service: "AppointmentTypeService"
    calendar_service: "CalendarService"


@inject
def get_appointment_type_mutation_dependencies(
    appointment_type_service: Annotated[
        "AppointmentTypeService | None", Provide["appointment_type_service"]
    ] = None,
    calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
) -> AppointmentTypeMutationDependencies:
    required = [appointment_type_service, calendar_service]
    if any(dep is None for dep in required):
        raise GraphQLError(
            f"Missing required dependency {', '.join([str(d) for d in required if d is None])}"
        )
    return AppointmentTypeMutationDependencies(
        appointment_type_service=cast("AppointmentTypeService", appointment_type_service),
        calendar_service=cast("CalendarService", calendar_service),
    )


@strawberry.input
class AppointmentTypeSlotInput:
    name: str
    calendar_ids: list[int]
    required_count: int = 1
    description: str = ""
    order: int = 0


@strawberry.input
class AppointmentTypeInput:
    organization_id: int
    name: str
    description: str = ""
    slots: list[AppointmentTypeSlotInput] = strawberry.field(default_factory=list)
    is_private: bool = True
    #: Exact length every booking through the appointment type must span. Required to
    #: create the appointment type with ``is_private=False``: a codeless public booking
    #: presents no code, so the appointment type is the only place its length can come
    #: from. Omitted leaves the appointment type unpinned, which only private appointment types may be.
    duration_seconds: int | None = None


@strawberry.input
class UpdateAppointmentTypeInput:
    organization_id: int
    appointment_type_id: int
    name: str
    description: str = ""
    slots: list[AppointmentTypeSlotInput] = strawberry.field(default_factory=list)
    is_private: bool | None = None
    #: See ``AppointmentTypeInput.duration_seconds``. Omitted leaves whatever the
    #: appointment type already has, so flipping ``is_private`` to False in the same call
    #: succeeds only if the appointment type already carries a duration or is given one here.
    duration_seconds: int | None = None


@strawberry.input
class DeleteAppointmentTypeInput:
    organization_id: int
    appointment_type_id: int


@strawberry.input
class AppointmentTypeSlotSelectionInput:
    slot_id: int
    calendar_ids: list[int]


@strawberry.input
class ExternalAttendeeInput:
    email: str
    name: str = ""
    id: int | None = None  # noqa: A003


@strawberry.input
class EventExternalAttendanceInput:
    external_attendee: ExternalAttendeeInput


@strawberry.input
class EventAttendanceInput:
    user_id: int


@strawberry.input
class AppointmentTypeEventInput:
    organization_id: int
    appointment_type_id: int
    title: str
    description: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str
    slot_selections: list[AppointmentTypeSlotSelectionInput]
    attendances: list[EventAttendanceInput] = strawberry.field(default_factory=list)
    external_attendances: list[EventExternalAttendanceInput] = strawberry.field(
        default_factory=list
    )


@strawberry.type
class AppointmentTypeResult:
    success: bool
    appointment_type: AppointmentTypeGraphQLType | None = None
    error_message: str | None = None


@strawberry.type
class DeleteAppointmentTypeResult:
    success: bool
    error_message: str | None = None


@strawberry.type
class AppointmentTypeEventResult:
    success: bool
    event: CalendarEventGraphQLType | None = None
    error_message: str | None = None


def _to_slot_input_data(
    slots: list[AppointmentTypeSlotInput],
) -> list[AppointmentTypeSlotInputData]:
    return [
        AppointmentTypeSlotInputData(
            name=s.name,
            calendar_ids=list(s.calendar_ids),
            required_count=s.required_count,
            description=s.description,
            order=s.order,
        )
        for s in slots
    ]


def _appointment_type_duration_from_seconds(
    duration_seconds: int | None,
) -> datetime.timedelta | None:
    """Convert an appointment type mutation's ``duration_seconds`` to the service's timedelta.

    Seconds is the unit every other duration-carrying field in this schema takes
    at the boundary, so the appointment type inputs match rather than introducing a Duration
    scalar for two fields.

    ``None`` passes straight through: it is the "omitted, leave unchanged"
    sentinel ``AppointmentTypeInputData.duration`` uses, which also means there is
    no way to clear a duration here -- deliberately, since clearing one on a
    publicly schedulable appointment type would fail open.
    """
    if duration_seconds is None:
        return None
    if duration_seconds <= 0:
        raise AppointmentTypeValidationError("duration_seconds must be greater than zero.")
    return datetime.timedelta(seconds=duration_seconds)


def _client_ip_from_request(request: object) -> str:
    """Extract the client IP address from a Django request for audit logging.

    Prefers the first entry of ``X-Forwarded-For`` (set by load balancers /
    proxies); falls back to ``REMOTE_ADDR``.  Robust to a missing ``META``
    attribute (returns ``""`` rather than raising).
    """
    forwarded_for = getattr(request, "META", {}).get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return getattr(request, "META", {}).get("REMOTE_ADDR", "")


# ---------------------------------------------------------------------------
# Booking-code creation mutations
# ---------------------------------------------------------------------------


@dataclass
class BookingCodeMutationDependencies:
    """Dependencies for booking-code mint and with-code mutations."""

    calendar_permission_service: "CalendarPermissionService"
    calendar_service: "CalendarService"


@inject
def get_booking_code_mutation_dependencies(
    calendar_permission_service: Annotated[
        "CalendarPermissionService | None", Provide["calendar_permission_service"]
    ] = None,
    calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
) -> BookingCodeMutationDependencies:
    """Get booking-code mutation dependencies from DI container."""
    if calendar_permission_service is None or calendar_service is None:
        raise GraphQLError("Internal server error.")
    return BookingCodeMutationDependencies(
        calendar_permission_service=cast("CalendarPermissionService", calendar_permission_service),
        calendar_service=cast("CalendarService", calendar_service),
    )


@dataclass
class AppointmentTypeBookingCodeMutationDependencies:
    """Dependencies for the unauthenticated appointment-type-booking-code mutations."""

    calendar_permission_service: "CalendarPermissionService"
    calendar_service: "CalendarService"
    appointment_type_service: "AppointmentTypeService"


@inject
def get_appointment_type_booking_code_mutation_dependencies(
    calendar_permission_service: Annotated[
        "CalendarPermissionService | None", Provide["calendar_permission_service"]
    ] = None,
    calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
    appointment_type_service: Annotated[
        "AppointmentTypeService | None", Provide["appointment_type_service"]
    ] = None,
) -> AppointmentTypeBookingCodeMutationDependencies:
    """Get appointment-type-booking-code mutation dependencies from DI container.

    The DI container wires ``appointment_type_service.calendar_service`` to the
    same ``CalendarService`` factory instance that is returned as
    ``calendar_service`` here, so initialising ``calendar_service`` with a
    booking code automatically propagates to ``appointment_type_service``.
    """
    if (
        calendar_permission_service is None
        or calendar_service is None
        or appointment_type_service is None
    ):
        raise GraphQLError("Internal server error.")
    return AppointmentTypeBookingCodeMutationDependencies(
        calendar_permission_service=cast("CalendarPermissionService", calendar_permission_service),
        calendar_service=cast("CalendarService", calendar_service),
        appointment_type_service=cast("AppointmentTypeService", appointment_type_service),
    )


@strawberry.input
class CreateBookingCodeInput:
    """Input for minting a single-use calendar booking code."""

    organization_id: int
    calendar_id: int
    expires_at: datetime.datetime | None = None


@strawberry.input
class CreateAppointmentTypeBookingCodeInput:
    """Input for minting a single-use appointment-type booking code."""

    organization_id: int
    appointment_type_id: int
    expires_at: datetime.datetime | None = None


@strawberry.input
class CreateEventCodeInput:
    """Input for minting a single-use reschedule or cancel code scoped to a calendar + event."""

    organization_id: int
    calendar_id: int
    event_id: int
    expires_at: datetime.datetime | None = None


@strawberry.input
class CreateAppointmentTypeEventCodeInput:
    """Input for minting a single-use reschedule or cancel code scoped to an appointment type + event."""

    organization_id: int
    appointment_type_id: int
    event_id: int
    expires_at: datetime.datetime | None = None


@strawberry.input
class RevokeBookingCodeInput:
    """Input for revoking a single-use booking code."""

    organization_id: int
    id: int  # noqa: A002


# ---------------------------------------------------------------------------
# With-code booking inputs
# ---------------------------------------------------------------------------


@strawberry.input
class ExternalAttendeeCodeInput:
    """External attendee input for unauthenticated code-bearing booking mutations."""

    email: str
    name: str = ""


@strawberry.input
class CodeSlotSelectionInput:
    """Per-slot calendar selection for the unauthenticated appointment-type-booking mutation."""

    slot_id: int
    calendar_ids: list[int]


@strawberry.input
class CreateEventWithCodeInput:
    """Input for the unauthenticated createCalendarEventWithCode mutation."""

    code: str
    title: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str
    external_attendee: ExternalAttendeeCodeInput
    description: str = ""


@strawberry.input
class CreateAppointmentTypeEventWithCodeInput:
    """Input for the unauthenticated createAppointmentTypeEventWithCode mutation."""

    code: str
    title: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str
    slot_selections: list[CodeSlotSelectionInput]
    external_attendee: ExternalAttendeeCodeInput
    description: str = ""


@strawberry.input
class RescheduleWithCodeInput:
    """Input for the unauthenticated rescheduleCalendarEventWithCode mutation."""

    code: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str


@strawberry.input
class RescheduleAppointmentTypeWithCodeInput:
    """Input for the unauthenticated rescheduleAppointmentTypeEventWithCode mutation.

    Slot selections are NOT included: v1 keeps existing appointment type/calendar selections
    and changes ONLY the event times.  Full slot re-selection is deferred to a
    future version; how it should interact with the existing selections is
    still an open question.
    """

    code: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str


@strawberry.input
class CancelWithCodeInput:
    """Input for the unauthenticated cancelEventWithCode mutation."""

    code: str


@strawberry.type
class AppointmentTypeMutations:
    """GraphQL mutations for AppointmentType CRUD and appointment-type event booking."""

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_appointment_type(
        self,
        info: strawberry.Info,
        input: AppointmentTypeInput,  # noqa: A002
    ) -> AppointmentTypeResult:
        organization = info.context.request.public_api_organization
        if organization is None:
            return AppointmentTypeResult(success=False, error_message="Organization not found")
        if input.organization_id != organization.id:
            return AppointmentTypeResult(success=False, error_message="Organization not found")
        deps = get_appointment_type_mutation_dependencies()
        deps.appointment_type_service.initialize(organization=organization)
        # create_appointment_type raises OverLimitError when the organization is at its
        # appointment_types limit. raise_over_limit_graphql_error renders the same
        # body as the REST 402 response and rolls back the request transaction
        # (graphql-core swallows resolver exceptions and always returns 200).
        try:
            appointment_type = deps.appointment_type_service.create_appointment_type(
                AppointmentTypeInputData(
                    name=input.name,
                    description=input.description,
                    slots=_to_slot_input_data(input.slots),
                    accepts_public_scheduling=not input.is_private,
                    # Raises AppointmentTypeValidationError on a non-positive
                    # value, caught below like any other appointment type error.
                    duration=_appointment_type_duration_from_seconds(input.duration_seconds),
                )
            )
        except OverLimitError as exc:
            raise_over_limit_graphql_error(exc)
        except AppointmentTypeError as e:
            return AppointmentTypeResult(success=False, error_message=str(e))
        return AppointmentTypeResult(success=True, appointment_type=appointment_type)  # type: ignore[arg-type]

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def update_appointment_type(
        self,
        info: strawberry.Info,
        input: UpdateAppointmentTypeInput,  # noqa: A002
    ) -> AppointmentTypeResult:
        organization = info.context.request.public_api_organization
        if organization is None:
            return AppointmentTypeResult(success=False, error_message="Organization not found")
        if input.organization_id != organization.id:
            return AppointmentTypeResult(success=False, error_message="Organization not found")
        deps = get_appointment_type_mutation_dependencies()
        deps.appointment_type_service.initialize(organization=organization)
        try:
            accepts_public_scheduling = None if input.is_private is None else not input.is_private
            appointment_type = deps.appointment_type_service.update_appointment_type(
                appointment_type_id=input.appointment_type_id,
                data=AppointmentTypeInputData(
                    name=input.name,
                    description=input.description,
                    slots=_to_slot_input_data(input.slots),
                    accepts_public_scheduling=accepts_public_scheduling,
                    duration=_appointment_type_duration_from_seconds(input.duration_seconds),
                ),
            )
        except AppointmentType.DoesNotExist:
            return AppointmentTypeResult(success=False, error_message="AppointmentType not found")
        except AppointmentTypeError as e:
            return AppointmentTypeResult(success=False, error_message=str(e))
        return AppointmentTypeResult(success=True, appointment_type=appointment_type)  # type: ignore[arg-type]

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def delete_appointment_type(
        self,
        info: strawberry.Info,
        input: DeleteAppointmentTypeInput,  # noqa: A002
    ) -> DeleteAppointmentTypeResult:
        organization = info.context.request.public_api_organization
        if organization is None:
            return DeleteAppointmentTypeResult(
                success=False, error_message="Organization not found"
            )
        if input.organization_id != organization.id:
            return DeleteAppointmentTypeResult(
                success=False, error_message="Organization not found"
            )
        deps = get_appointment_type_mutation_dependencies()
        deps.appointment_type_service.initialize(organization=organization)
        try:
            deps.appointment_type_service.delete_appointment_type(
                appointment_type_id=input.appointment_type_id
            )
        except AppointmentType.DoesNotExist:
            return DeleteAppointmentTypeResult(
                success=False, error_message="AppointmentType not found"
            )
        except AppointmentTypeError as e:
            return DeleteAppointmentTypeResult(success=False, error_message=str(e))
        return DeleteAppointmentTypeResult(success=True)

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_appointment_type_event(
        self,
        info: strawberry.Info,
        input: AppointmentTypeEventInput,  # noqa: A002
    ) -> AppointmentTypeEventResult:
        organization = info.context.request.public_api_organization
        if organization is None:
            return AppointmentTypeEventResult(success=False, error_message="Organization not found")
        if input.organization_id != organization.id:
            return AppointmentTypeEventResult(success=False, error_message="Organization not found")
        deps = get_appointment_type_mutation_dependencies()
        deps.calendar_service.initialize_without_provider(organization=organization)
        deps.appointment_type_service.initialize(organization=organization)
        data = AppointmentTypeEventInputData(
            title=input.title,
            description=input.description,
            start_time=input.start_time,
            end_time=input.end_time,
            timezone=input.timezone,
            appointment_type_id=input.appointment_type_id,
            slot_selections=[
                AppointmentTypeSlotSelectionInputData(
                    slot_id=s.slot_id, calendar_ids=list(s.calendar_ids)
                )
                for s in input.slot_selections
            ],
            attendances=[EventAttendanceInputData(user_id=a.user_id) for a in input.attendances],
            external_attendances=[
                EventExternalAttendanceInputData(
                    external_attendee=ExternalAttendeeInputData(
                        email=e.external_attendee.email,
                        name=e.external_attendee.name,
                        id=e.external_attendee.id,
                    )
                )
                for e in input.external_attendances
            ],
        )
        try:
            event = deps.appointment_type_service.create_appointment_type_event(data)
        except AppointmentType.DoesNotExist:
            return AppointmentTypeEventResult(
                success=False, error_message="AppointmentType not found"
            )
        except PermissionDenied as e:
            return AppointmentTypeEventResult(success=False, error_message=str(e))
        except PermissionServiceInitializationError:
            return AppointmentTypeEventResult(
                success=False,
                error_message=(
                    "This appointment type does not accept public scheduling. "
                    "A token or scheduling code is required."
                ),
            )
        except BookingPolicyViolationError as e:
            return AppointmentTypeEventResult(
                success=False,
                error_message=(
                    str(e)
                    or "The requested time slot is not available under the current booking policy."
                ),
            )
        except AppointmentTypeError as e:
            return AppointmentTypeEventResult(success=False, error_message=str(e))
        return AppointmentTypeEventResult(success=True, event=event)  # type: ignore[arg-type]

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_calendar_booking_code(
        self,
        info: strawberry.Info,
        input: CreateBookingCodeInput,  # noqa: A002
    ) -> BookingCodeResult:
        """Mint a single-use booking code scoped to a calendar.

        The token grants CREATE permission, allowing the code-bearer to book
        an event on the bound calendar (or bundle calendar).  The code is
        returned once in plaintext — only its hash is persisted.
        """
        org = info.context.request.public_api_organization
        if org is None:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Organization not found.",
            )

        if input.organization_id != org.id:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Organization not found.",
            )

        # Verify the calendar belongs to the authenticated org.
        try:
            Calendar.objects.filter_by_organization(org.id).get(id=input.calendar_id)
        except Calendar.DoesNotExist:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Calendar not found.",
            )

        minted_by = getattr(info.context.request, "public_api_system_user", None)
        deps = get_booking_code_mutation_dependencies()
        token, plaintext_code = deps.calendar_permission_service.create_booking_token(
            organization_id=org.id,
            permissions=[EventManagementPermissions.CREATE],
            expires_at=input.expires_at,
            minted_by=minted_by,
            calendar_id=input.calendar_id,
        )
        return BookingCodeResult(success=True, code=plaintext_code, id=token.pk)

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_appointment_type_booking_code(
        self,
        info: strawberry.Info,
        input: CreateAppointmentTypeBookingCodeInput,  # noqa: A002
    ) -> BookingCodeResult:
        """Mint a single-use booking code scoped to an appointment type.

        The token grants CREATE permission, allowing the code-bearer to book
        an event against the bound appointment type.  The code is returned once
        in plaintext — only its hash is persisted.
        """
        org = info.context.request.public_api_organization
        if org is None:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Organization not found.",
            )

        if input.organization_id != org.id:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Organization not found.",
            )

        # Verify the appointment type belongs to the authenticated org.
        try:
            AppointmentType.objects.filter_by_organization(org.id).get(id=input.appointment_type_id)
        except AppointmentType.DoesNotExist:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Appointment type not found.",
            )

        minted_by = getattr(info.context.request, "public_api_system_user", None)
        deps = get_booking_code_mutation_dependencies()
        token, plaintext_code = deps.calendar_permission_service.create_booking_token(
            organization_id=org.id,
            permissions=[EventManagementPermissions.CREATE],
            expires_at=input.expires_at,
            minted_by=minted_by,
            appointment_type_id=input.appointment_type_id,
        )
        return BookingCodeResult(success=True, code=plaintext_code, id=token.pk)

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_calendar_reschedule_booking_code(
        self,
        info: strawberry.Info,
        input: CreateEventCodeInput,  # noqa: A002
    ) -> BookingCodeResult:
        """Mint a single-use reschedule code bound to a specific event on a calendar.

        The token grants RESCHEDULE permission for the bound event only.  The
        code is returned once in plaintext — only its hash is persisted.
        """
        org = info.context.request.public_api_organization
        if org is None:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Not found.",
            )

        if input.organization_id != org.id:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Not found.",
            )

        # Verify the calendar belongs to the authenticated org.
        try:
            Calendar.objects.filter_by_organization(org.id).get(id=input.calendar_id)
        except Calendar.DoesNotExist:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Not found.",
            )

        # Verify the event belongs to this org AND to the named calendar, and is not an appointment-type event.
        try:
            CalendarEvent.objects.filter_by_organization(org.id).get(
                id=input.event_id,
                calendar_fk_id=input.calendar_id,
                appointment_type_fk_id__isnull=True,
            )
        except CalendarEvent.DoesNotExist:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Not found.",
            )

        minted_by = getattr(info.context.request, "public_api_system_user", None)
        deps = get_booking_code_mutation_dependencies()
        token, plaintext_code = deps.calendar_permission_service.create_booking_token(
            organization_id=org.id,
            permissions=[EventManagementPermissions.RESCHEDULE],
            expires_at=input.expires_at,
            minted_by=minted_by,
            calendar_id=input.calendar_id,
            event_id=input.event_id,
        )
        return BookingCodeResult(success=True, code=plaintext_code, id=token.pk)

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_appointment_type_reschedule_booking_code(
        self,
        info: strawberry.Info,
        input: CreateAppointmentTypeEventCodeInput,  # noqa: A002
    ) -> BookingCodeResult:
        """Mint a single-use reschedule code bound to a specific event on an appointment type.

        The token grants RESCHEDULE permission for the bound event only.  The
        code is returned once in plaintext — only its hash is persisted.
        """
        org = info.context.request.public_api_organization
        if org is None:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Not found.",
            )

        if input.organization_id != org.id:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Not found.",
            )

        # Verify the appointment type belongs to the authenticated org.
        try:
            AppointmentType.objects.filter_by_organization(org.id).get(id=input.appointment_type_id)
        except AppointmentType.DoesNotExist:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Not found.",
            )

        # Verify the event belongs to this org AND to the named appointment type.
        try:
            CalendarEvent.objects.filter_by_organization(org.id).get(
                id=input.event_id,
                appointment_type_fk_id=input.appointment_type_id,
            )
        except CalendarEvent.DoesNotExist:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Not found.",
            )

        minted_by = getattr(info.context.request, "public_api_system_user", None)
        deps = get_booking_code_mutation_dependencies()
        token, plaintext_code = deps.calendar_permission_service.create_booking_token(
            organization_id=org.id,
            permissions=[EventManagementPermissions.RESCHEDULE],
            expires_at=input.expires_at,
            minted_by=minted_by,
            appointment_type_id=input.appointment_type_id,
            event_id=input.event_id,
        )
        return BookingCodeResult(success=True, code=plaintext_code, id=token.pk)

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_calendar_cancellation_booking_code(
        self,
        info: strawberry.Info,
        input: CreateEventCodeInput,  # noqa: A002
    ) -> BookingCodeResult:
        """Mint a single-use cancellation code bound to a specific event on a calendar.

        The token grants CANCEL permission for the bound event only.  The code
        is returned once in plaintext — only its hash is persisted.
        """
        org = info.context.request.public_api_organization
        if org is None:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Not found.",
            )

        if input.organization_id != org.id:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Not found.",
            )

        # Verify the calendar belongs to the authenticated org.
        try:
            Calendar.objects.filter_by_organization(org.id).get(id=input.calendar_id)
        except Calendar.DoesNotExist:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Not found.",
            )

        # Verify the event belongs to this org AND to the named calendar, and is not an appointment-type event.
        try:
            CalendarEvent.objects.filter_by_organization(org.id).get(
                id=input.event_id,
                calendar_fk_id=input.calendar_id,
                appointment_type_fk_id__isnull=True,
            )
        except CalendarEvent.DoesNotExist:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Not found.",
            )

        minted_by = getattr(info.context.request, "public_api_system_user", None)
        deps = get_booking_code_mutation_dependencies()
        token, plaintext_code = deps.calendar_permission_service.create_booking_token(
            organization_id=org.id,
            permissions=[EventManagementPermissions.CANCEL],
            expires_at=input.expires_at,
            minted_by=minted_by,
            calendar_id=input.calendar_id,
            event_id=input.event_id,
        )
        return BookingCodeResult(success=True, code=plaintext_code, id=token.pk)

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_appointment_type_cancellation_booking_code(
        self,
        info: strawberry.Info,
        input: CreateAppointmentTypeEventCodeInput,  # noqa: A002
    ) -> BookingCodeResult:
        """Mint a single-use cancellation code bound to a specific event on an appointment type.

        The token grants CANCEL permission for the bound event only.  The code
        is returned once in plaintext — only its hash is persisted.
        """
        org = info.context.request.public_api_organization
        if org is None:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Not found.",
            )

        if input.organization_id != org.id:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Not found.",
            )

        # Verify the appointment type belongs to the authenticated org.
        try:
            AppointmentType.objects.filter_by_organization(org.id).get(id=input.appointment_type_id)
        except AppointmentType.DoesNotExist:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Not found.",
            )

        # Verify the event belongs to this org AND to the named appointment type.
        try:
            CalendarEvent.objects.filter_by_organization(org.id).get(
                id=input.event_id,
                appointment_type_fk_id=input.appointment_type_id,
            )
        except CalendarEvent.DoesNotExist:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Not found.",
            )

        minted_by = getattr(info.context.request, "public_api_system_user", None)
        deps = get_booking_code_mutation_dependencies()
        token, plaintext_code = deps.calendar_permission_service.create_booking_token(
            organization_id=org.id,
            permissions=[EventManagementPermissions.CANCEL],
            expires_at=input.expires_at,
            minted_by=minted_by,
            appointment_type_id=input.appointment_type_id,
            event_id=input.event_id,
        )
        return BookingCodeResult(success=True, code=plaintext_code, id=token.pk)

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def revoke_booking_code(
        self,
        info: strawberry.Info,
        input: RevokeBookingCodeInput,  # noqa: A002
    ) -> BookingCodeResult:
        """Revoke a single-use booking code by its opaque id.

        The code becomes invalid immediately and cannot be used for any
        subsequent operations (reads or writes). Revoke is idempotent:
        revoking an already-revoked code returns success without error.
        """
        org = info.context.request.public_api_organization
        if org is None:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Not found.",
            )

        if input.organization_id != org.id:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Not found.",
            )

        actor_system_user = getattr(info.context.request, "public_api_system_user", None)
        deps = get_booking_code_mutation_dependencies()
        try:
            deps.calendar_permission_service.revoke_token(
                organization_id=org.id,
                token_id=input.id,
                actor_system_user=actor_system_user,
            )
        except InvalidTokenError:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Not found.",
            )

        return BookingCodeResult(success=True)

    @strawberry.mutation
    def create_calendar_event_with_code(
        self,
        info: strawberry.Info,
        input: CreateEventWithCodeInput,  # noqa: A002
    ) -> CodeEventResult:
        """Book a single-calendar event using a single-use booking code.

        This is an unauthenticated mutation: no org token is required.  The org
        context, permissions, and calendar scope are all derived from the booking
        code.  On success the code is atomically consumed so it cannot be replayed.
        On a failed create (slot unavailable, invalid time range, etc.) the code is
        NOT consumed and the patient may retry with a different slot.
        """
        deps = get_booking_code_mutation_dependencies()

        # --- Step 1: resolve and validate the code ---
        try:
            token = deps.calendar_permission_service.resolve_code(input.code)
        except InvalidTokenError:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Invalid or unknown booking code.",
            )
        except TokenExpiredError:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.EXPIRED,
                error_message="This booking code has expired.",
            )
        except TokenAlreadyUsedError:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.ALREADY_USED,
                error_message="This booking code has already been used.",
            )
        except TokenRevokedError:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.REVOKED,
                error_message="This booking code has been revoked.",
            )

        # --- Step 2: check permission ---
        token_permissions = {p.permission for p in token.permissions.all()}
        if EventManagementPermissions.CREATE not in token_permissions:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message="This code does not permit booking.",
            )

        # --- Step 3: scope check — must be single-calendar (not appointment type) ---
        if token.calendar is None:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message="This code is not scoped to a single calendar.",
            )

        # --- Step 4: resolve org ---
        try:
            org = Organization.objects.get(id=token.organization_id)
        except Organization.DoesNotExist:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Invalid or unknown booking code.",
            )

        # --- Step 5: extract client IP for audit ---
        source_ip = _client_ip_from_request(info.context.request)

        # --- Step 6: build event data ---
        event_data = CalendarEventInputData(
            title=input.title,
            description=input.description or "",
            start_time=input.start_time,
            end_time=input.end_time,
            timezone=input.timezone,
            external_attendances=[
                EventExternalAttendanceInputData(
                    external_attendee=ExternalAttendeeInputData(
                        email=input.external_attendee.email,
                        name=input.external_attendee.name or "",
                    )
                )
            ],
        )

        # --- Step 7: atomic create + consume ---
        # Create FIRST, then consume — so on a race the loser's consume_code raises under
        # the row lock and the whole transaction (including the just-created event) rolls
        # back, leaving exactly one event and the code consumed once.
        try:
            with transaction.atomic():
                deps.calendar_service.initialize_without_provider(
                    user_or_token=input.code, organization=org
                )
                event = deps.calendar_service.create_event(token.calendar.id, event_data)
                deps.calendar_permission_service.consume_code(token, source_ip)
        except (TokenAlreadyUsedError, TokenExpiredError, TokenRevokedError) as e:
            # Concurrent consumer won the race, or state changed between resolve and consume.
            error_code = BookingCodeErrorCode.ALREADY_USED
            error_message = "This booking code has already been used."
            if isinstance(e, TokenExpiredError):
                error_code = BookingCodeErrorCode.EXPIRED
                error_message = "This booking code has expired."
            elif isinstance(e, TokenRevokedError):
                error_code = BookingCodeErrorCode.REVOKED
                error_message = "This booking code has been revoked."
            return CodeEventResult(
                success=False,
                error_code=error_code,
                error_message=error_message,
            )
        except PermissionDenied:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message="This code does not permit booking on this calendar.",
            )
        except BookingPolicyViolationError:
            # Policy violated — code NOT consumed (txn rolled back), patient may retry.
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.SLOT_UNAVAILABLE,
                error_message=(
                    "The requested time slot is not available under the current booking policy."
                ),
            )
        except (NoAvailableTimeWindowsError, EventManagementError):
            # Slot taken / invalid times — code NOT consumed (txn rolled back), patient may retry.
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.SLOT_UNAVAILABLE,
                error_message="The requested time slot is not available.",
            )
        # create_event raises OverLimitError at the organization's postpaid
        # event_occurrences allowance (no payment method on file). Unlike the domain
        # errors above, this is not a booking-code-specific outcome the patient can
        # retry around, so it is rendered via the shared over-limit GraphQL contract
        # (raise_over_limit_graphql_error, which also rolls back the request
        # transaction -- see its docstring) rather than a CodeEventResult error_code.
        except OverLimitError as exc:
            raise_over_limit_graphql_error(exc)

        return CodeEventResult(success=True, event=event)  # type: ignore[arg-type]

    @strawberry.mutation
    def create_appointment_type_event_with_code(
        self,
        info: strawberry.Info,
        input: CreateAppointmentTypeEventWithCodeInput,  # noqa: A002
    ) -> CodeEventResult:
        """Book an appointment-type calendar event using a single-use appointment type booking code.

        This is an unauthenticated mutation: no org token is required.  The org
        context, permissions, and appointment type scope are all derived from the booking
        code.  On success the code is atomically consumed so it cannot be
        replayed.  On a failed create (slot unavailable, invalid selection, etc.)
        the code is NOT consumed and the patient may retry.

        The appointment_type_id is taken STRICTLY from the token — the client cannot
        override it via the input.
        """
        deps = get_appointment_type_booking_code_mutation_dependencies()

        # --- Step 1: resolve and validate the code ---
        try:
            token = deps.calendar_permission_service.resolve_code(input.code)
        except InvalidTokenError:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Invalid or unknown booking code.",
            )
        except TokenExpiredError:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.EXPIRED,
                error_message="This booking code has expired.",
            )
        except TokenAlreadyUsedError:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.ALREADY_USED,
                error_message="This booking code has already been used.",
            )
        except TokenRevokedError:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.REVOKED,
                error_message="This booking code has been revoked.",
            )

        # --- Step 2: check permission ---
        token_permissions = {p.permission for p in token.permissions.all()}
        if EventManagementPermissions.CREATE not in token_permissions:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message="This code does not permit booking.",
            )

        # --- Step 3: scope check — must be appointment-type-scoped (not single-calendar) ---
        if token.appointment_type is None:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message=(
                    "This code is not scoped to an appointment type. "
                    "Use createCalendarEventWithCode for single-calendar codes."
                ),
            )

        # --- Step 4: resolve org ---
        try:
            org = Organization.objects.get(id=token.organization_id)
        except Organization.DoesNotExist:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Invalid or unknown booking code.",
            )

        # --- Step 5: extract client IP for audit ---
        source_ip = _client_ip_from_request(info.context.request)

        # --- Step 6: build appointment type event data ---
        # appointment_type_id comes from the token — not from client input — to enforce scope.
        appointment_type_event_data = AppointmentTypeEventInputData(
            appointment_type_id=token.appointment_type.id,
            title=input.title,
            description=input.description or "",
            start_time=input.start_time,
            end_time=input.end_time,
            timezone=input.timezone,
            slot_selections=[
                AppointmentTypeSlotSelectionInputData(
                    slot_id=s.slot_id,
                    calendar_ids=list(s.calendar_ids),
                )
                for s in input.slot_selections
            ],
            external_attendances=[
                EventExternalAttendanceInputData(
                    external_attendee=ExternalAttendeeInputData(
                        email=input.external_attendee.email,
                        name=input.external_attendee.name or "",
                    )
                )
            ],
        )

        # --- Step 7: atomic create + consume ---
        # ``deps.calendar_service`` is the authoritative, code-initialized instance.
        # Explicitly wire it into ``deps.appointment_type_service`` so that the event
        # is created on the same CalendarService instance that carries the booking
        # code's token — this is necessary because the DI container's Factory
        # provider gives AppointmentTypeService its OWN CalendarService instance via its
        # @inject __init__.  Without this explicit wiring the primary-calendar create
        # would use an uninitialized instance and the permission / availability checks
        # would fail.
        try:
            with transaction.atomic():
                deps.calendar_service.initialize_without_provider(
                    user_or_token=input.code, organization=org
                )
                deps.appointment_type_service.calendar_service = deps.calendar_service
                # Share the token-initialized permission service so the appointment-type-level
                # ``can_perform_appointment_type_scheduling`` gate can read the appointment-type-scoped token.
                # Without this the appointment type service would hold a separate, uninitialized
                # CalendarPermissionService instance and deny private-appointment-type bookings
                # even when a valid appointment-type-scoped code was provided.
                deps.appointment_type_service.calendar_permission_service = (
                    deps.calendar_service.calendar_permission_service
                )
                deps.appointment_type_service.initialize(organization=org)
                event = deps.appointment_type_service.create_appointment_type_event(
                    appointment_type_event_data
                )
                deps.calendar_permission_service.consume_code(token, source_ip)
        except (TokenAlreadyUsedError, TokenExpiredError, TokenRevokedError) as e:
            # Concurrent consumer won the race, or state changed between resolve and consume.
            error_code = BookingCodeErrorCode.ALREADY_USED
            error_message = "This booking code has already been used."
            if isinstance(e, TokenExpiredError):
                error_code = BookingCodeErrorCode.EXPIRED
                error_message = "This booking code has expired."
            elif isinstance(e, TokenRevokedError):
                error_code = BookingCodeErrorCode.REVOKED
                error_message = "This booking code has been revoked."
            return CodeEventResult(
                success=False,
                error_code=error_code,
                error_message=error_message,
            )
        except PermissionDenied:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message="This code does not permit booking on this calendar.",
            )
        except BookingPolicyViolationError:
            # Policy violated — code NOT consumed (txn rolled back), patient may retry.
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.SLOT_UNAVAILABLE,
                error_message=(
                    "The requested time slot is not available under the current booking policy."
                ),
            )
        except (EventManagementError, AppointmentTypeError):
            # Slot taken / invalid selection / invalid times — code NOT consumed (txn rolled
            # back), patient may retry with a different slot.
            # Note: NoAvailableTimeWindowsError is a subclass of EventManagementError and
            # is therefore already covered by the EventManagementError branch.
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.SLOT_UNAVAILABLE,
                error_message="The requested time slot is not available.",
            )
        # create_appointment_type_event raises OverLimitError at the organization's postpaid
        # event_occurrences allowance (no payment method on file). Rendered via
        # raise_over_limit_graphql_error (which also rolls back the request
        # transaction -- see its docstring), like the single-calendar booking-code
        # path above.
        except OverLimitError as exc:
            raise_over_limit_graphql_error(exc)

        return CodeEventResult(success=True, event=event)  # type: ignore[arg-type]

    @strawberry.mutation
    def reschedule_calendar_event_with_code(
        self,
        info: strawberry.Info,
        input: RescheduleWithCodeInput,  # noqa: A002
    ) -> CodeEventResult:
        """Reschedule an event bound to a single-use RESCHEDULE booking code.

        This is an unauthenticated mutation: no org token is required.  The org
        context, calendar scope, and the specific event to reschedule are all
        derived from the booking code.  On success the code is atomically consumed
        so it cannot be replayed.  On a failed reschedule (slot outside availability,
        etc.) the code is NOT consumed and the patient may retry with a different slot.

        Only the start/end/timezone fields change — title, description, attendees,
        and resource allocations are preserved exactly from the existing event so
        that the permission check requires exactly {RESCHEDULE} and no other
        permission.
        """
        deps = get_booking_code_mutation_dependencies()

        # --- Step 1: resolve and validate the code ---
        try:
            token = deps.calendar_permission_service.resolve_code(input.code)
        except InvalidTokenError:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Invalid or unknown booking code.",
            )
        except TokenExpiredError:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.EXPIRED,
                error_message="This booking code has expired.",
            )
        except TokenAlreadyUsedError:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.ALREADY_USED,
                error_message="This booking code has already been used.",
            )
        except TokenRevokedError:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.REVOKED,
                error_message="This booking code has been revoked.",
            )

        # --- Step 2: check permission — must hold RESCHEDULE ---
        token_permissions = {p.permission for p in token.permissions.all()}
        if EventManagementPermissions.RESCHEDULE not in token_permissions:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message="This code does not permit rescheduling.",
            )

        # --- Step 3: scope check — must be event-scoped and single-calendar (not appointment type) ---
        if token.event is None:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message="This code is not bound to a specific event.",
            )

        # An appointment-type-reschedule code has appointment type set; route to the appointment type path instead.
        if token.appointment_type is not None:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message=(
                    "This code is scoped to an appointment type. "
                    "Use rescheduleAppointmentTypeEventWithCode for appointment-type-scoped codes."
                ),
            )

        # --- Step 4: resolve org ---
        try:
            org = Organization.objects.get(id=token.organization_id)
        except Organization.DoesNotExist:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Invalid or unknown booking code.",
            )

        # --- Step 5: extract client IP for audit ---
        source_ip = _client_ip_from_request(info.context.request)

        # --- Step 6: resolve the bound event and its calendar from the token ---
        # calendar_id and event_id come strictly from the token — not from client input —
        # so the code can only ever affect the exact event it was minted for.
        event_id: int = token.event_fk_id  # type: ignore[assignment]
        calendar_id: int = token.event.calendar_fk_id  # type: ignore[assignment]

        # Load the existing event to snapshot its current details (title, description,
        # attendances, external_attendances, resource_allocations).  We build the
        # CalendarEventInputData by COPYING all preserved fields and overriding only
        # the time fields so that _determine_required_update_permissions yields exactly
        # {RESCHEDULE}.
        try:
            existing_event = (
                CalendarEvent.objects.filter_by_organization(org.id)
                .select_related("calendar")
                .prefetch_related(
                    "attendances",
                    "resource_allocations",
                )
                .get(id=event_id, calendar_fk_id=calendar_id)
            )
        except CalendarEvent.DoesNotExist:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Invalid or unknown booking code.",
            )

        # --- Step 7: build the preserved-details event data ---
        # Preserve internal attendances.
        preserved_attendances = [
            EventAttendanceInputData(user_id=attendance.membership_user_id)
            for attendance in existing_event.attendances.all()
            if attendance.membership_user_id is not None
        ]

        # Preserve external attendances — include the ExternalAttendee id so that
        # serialize_event_data_input can correlate the status correctly and produce
        # a CalendarEventData matching the old event's external_attendees by email,
        # ensuring _check_attendances_update_necessary_permissions sees no change.
        preserved_external_attendances = [
            EventExternalAttendanceInputData(
                external_attendee=ExternalAttendeeInputData(
                    email=ea.external_attendee_fk.email,  # type: ignore[union-attr]
                    name=ea.external_attendee_fk.name or "",  # type: ignore[union-attr]
                    id=ea.external_attendee_fk_id,  # type: ignore[union-attr]
                )
            )
            for ea in existing_event.external_attendances.select_related("external_attendee")
        ]

        # Preserve resource allocations (skip any with a null calendar_fk_id, mirroring
        # the recurring-event transfer guard in calendar_event_service.py).
        preserved_resource_allocations = [
            ResourceAllocationInputData(resource_id=ra.calendar_fk_id)  # type: ignore[arg-type]
            for ra in existing_event.resource_allocations.all()
            if ra.calendar_fk_id
        ]

        event_data = CalendarEventInputData(
            title=existing_event.title,
            description=existing_event.description or "",
            start_time=input.start_time,
            end_time=input.end_time,
            timezone=input.timezone,
            attendances=preserved_attendances,
            external_attendances=preserved_external_attendances,
            resource_allocations=preserved_resource_allocations,
        )

        # --- Step 7b: availability pre-check (code-path only) ---
        # For calendars that manage availability windows, verify the requested slot falls
        # inside a declared window BEFORE entering the atomic block.  This keeps the check
        # scoped to the reschedule-with-code path (REST/bundle updates are unaffected) and
        # ensures the code is never consumed on an out-of-window attempt.
        if existing_event.calendar.manage_available_windows:
            deps.calendar_service.initialize_without_provider(organization=org)
            available_windows = deps.calendar_service.get_availability_windows_in_range(
                existing_event.calendar,
                input.start_time,
                input.end_time,
            )
            if not available_windows:
                return CodeEventResult(
                    success=False,
                    error_code=BookingCodeErrorCode.SLOT_UNAVAILABLE,
                    error_message="The requested time slot is not available.",
                )

        # --- Step 8: atomic update + consume ---
        # Update FIRST, then consume — so on a race the loser's consume_code raises under
        # the row lock and the whole transaction (including the just-updated event) rolls
        # back, leaving exactly one update and the code consumed once.
        try:
            with transaction.atomic():
                deps.calendar_service.initialize_without_provider(
                    user_or_token=input.code, organization=org
                )
                event = deps.calendar_service.update_event(calendar_id, event_id, event_data)
                deps.calendar_permission_service.consume_code(token, source_ip)
        except (TokenAlreadyUsedError, TokenExpiredError, TokenRevokedError) as e:
            # Concurrent consumer won the race, or state changed between resolve and consume.
            error_code = BookingCodeErrorCode.ALREADY_USED
            error_message = "This booking code has already been used."
            if isinstance(e, TokenExpiredError):
                error_code = BookingCodeErrorCode.EXPIRED
                error_message = "This booking code has expired."
            elif isinstance(e, TokenRevokedError):
                error_code = BookingCodeErrorCode.REVOKED
                error_message = "This booking code has been revoked."
            return CodeEventResult(
                success=False,
                error_code=error_code,
                error_message=error_message,
            )
        except PermissionDenied:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message="This code does not permit rescheduling this event.",
            )
        except (EventManagementError, AppointmentTypeError):
            # Slot outside availability / invalid times — code NOT consumed (txn rolled
            # back), patient may retry with a different slot.
            # Note: NoAvailableTimeWindowsError is a subclass of EventManagementError and
            # is therefore already covered.
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.SLOT_UNAVAILABLE,
                error_message="The requested time slot is not available.",
            )

        return CodeEventResult(success=True, event=event)  # type: ignore[arg-type]

    @strawberry.mutation
    def reschedule_appointment_type_event_with_code(
        self,
        info: strawberry.Info,
        input: RescheduleAppointmentTypeWithCodeInput,  # noqa: A002
    ) -> CodeEventResult:
        """Reschedule an appointment-type event bound to a single-use APPOINTMENT_TYPE RESCHEDULE code.

        This is an unauthenticated mutation: no org token is required.  The org
        context, appointment-type scope, and the specific appointment-type event to reschedule
        are all derived from the booking code.  On success the code is atomically
        consumed so it cannot be replayed.  On a failed reschedule (slot outside
        availability, etc.) the code is NOT consumed and the patient may retry.

        Only the start/end/timezone fields change — title, description, attendees,
        resource allocations, and the appointment type's calendar selections are preserved
        exactly from the existing event (time-only v1; full slot re-selection is
        deferred to a future version, and how it should interact with the existing
        selections is still an open question).  The event id is preserved so that
        external integrations (e.g. Building Blocks) continue to reference the
        same event.
        """
        deps = get_appointment_type_booking_code_mutation_dependencies()

        # --- Step 1: resolve and validate the code ---
        try:
            token = deps.calendar_permission_service.resolve_code(input.code)
        except InvalidTokenError:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Invalid or unknown booking code.",
            )
        except TokenExpiredError:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.EXPIRED,
                error_message="This booking code has expired.",
            )
        except TokenAlreadyUsedError:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.ALREADY_USED,
                error_message="This booking code has already been used.",
            )
        except TokenRevokedError:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.REVOKED,
                error_message="This booking code has been revoked.",
            )

        # --- Step 2: check permission — must hold RESCHEDULE ---
        token_permissions = {p.permission for p in token.permissions.all()}
        if EventManagementPermissions.RESCHEDULE not in token_permissions:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message="This code does not permit rescheduling.",
            )

        # --- Step 3: scope check — must be event-scoped AND appointment-type-scoped ---
        if token.event is None:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message="This code is not bound to a specific event.",
            )

        # A single-calendar reschedule code has no appointment type; route to the single-calendar path.
        if token.appointment_type is None:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message=(
                    "This code is not scoped to an appointment type. "
                    "Use rescheduleCalendarEventWithCode for single-calendar codes."
                ),
            )

        # --- Step 4: resolve org ---
        try:
            org = Organization.objects.get(id=token.organization_id)
        except Organization.DoesNotExist:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Invalid or unknown booking code.",
            )

        # --- Step 5: extract client IP for audit ---
        source_ip = _client_ip_from_request(info.context.request)

        # --- Step 6: event_id from token (not client input) ---
        # calendar_id and event_id come strictly from the token so the code can
        # only ever affect the exact appointment-type event it was minted for.
        event_id: int = token.event_fk_id  # type: ignore[assignment]

        # --- Step 7: availability pre-check (code-path only) ---
        # For the primary calendar of the bound appointment-type event: if it manages
        # availability windows, verify the new times fall within a declared window
        # BEFORE entering the atomic block.  This keeps the code alive on failure.
        try:
            bound_event = (
                CalendarEvent.objects.filter_by_organization(org.id)
                .select_related("calendar")
                .get(id=event_id)
            )
        except CalendarEvent.DoesNotExist:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Invalid or unknown booking code.",
            )

        primary_calendar = bound_event.calendar
        if primary_calendar is not None and primary_calendar.manage_available_windows:
            deps.calendar_service.initialize_without_provider(organization=org)
            available_windows = deps.calendar_service.get_availability_windows_in_range(
                primary_calendar,
                input.start_time,
                input.end_time,
            )
            if not available_windows:
                return CodeEventResult(
                    success=False,
                    error_code=BookingCodeErrorCode.SLOT_UNAVAILABLE,
                    error_message="The requested time slot is not available.",
                )

        # --- Step 8: atomic update + consume ---
        # Update FIRST, then consume — so on a race the loser's consume_code raises under
        # the row lock and the whole transaction (including the just-updated event) rolls
        # back, leaving exactly one update and the code consumed once.
        # ``deps.calendar_service`` is connected to ``deps.appointment_type_service``
        # so that the update runs on the same code-initialized CalendarService instance.
        try:
            with transaction.atomic():
                deps.calendar_service.initialize_without_provider(
                    user_or_token=input.code, organization=org
                )
                deps.appointment_type_service.calendar_service = deps.calendar_service
                deps.appointment_type_service.initialize(organization=org)
                event = deps.appointment_type_service.reschedule_appointment_type_event(
                    event_id=event_id,
                    start_time=input.start_time,
                    end_time=input.end_time,
                    tz=input.timezone,
                )
                deps.calendar_permission_service.consume_code(token, source_ip)
        except (TokenAlreadyUsedError, TokenExpiredError, TokenRevokedError) as e:
            # Concurrent consumer won the race, or state changed between resolve and consume.
            error_code = BookingCodeErrorCode.ALREADY_USED
            error_message = "This booking code has already been used."
            if isinstance(e, TokenExpiredError):
                error_code = BookingCodeErrorCode.EXPIRED
                error_message = "This booking code has expired."
            elif isinstance(e, TokenRevokedError):
                error_code = BookingCodeErrorCode.REVOKED
                error_message = "This booking code has been revoked."
            return CodeEventResult(
                success=False,
                error_code=error_code,
                error_message=error_message,
            )
        except PermissionDenied:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message="This code does not permit rescheduling this event.",
            )
        except (EventManagementError, AppointmentTypeError):
            # Slot outside availability / invalid times — code NOT consumed (txn rolled
            # back), patient may retry with a different slot.
            # Note: NoAvailableTimeWindowsError is a subclass of EventManagementError and
            # is therefore already covered.
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.SLOT_UNAVAILABLE,
                error_message="The requested time slot is not available.",
            )

        return CodeEventResult(success=True, event=event)  # type: ignore[arg-type]

    @strawberry.mutation
    def cancel_event_with_code(
        self,
        info: strawberry.Info,
        input: CancelWithCodeInput,  # noqa: A002
    ) -> CodeEventResult:
        """Cancel an event bound to a single-use CANCEL booking code.

        This is an unauthenticated mutation: no org token is required.  The org
        context, scope (single-calendar or appointment type), and the specific event to cancel
        are all derived from the booking code.  On success the code is atomically
        consumed so it cannot be replayed.

        Handles both a calendar-bound (non-appointment-type) cancel code and an appointment-type-bound
        (appointment-type event) cancel code via the SAME mutation.  The routing is determined
        by whether ``token.appointment_type`` is set.

        For appointment-type events the non-primary ``BlockedTime`` rows (linked only by the
        string ``external_id`` convention) are explicitly deleted before the primary
        event is removed, so no orphaned busy-markers remain.
        """
        deps = get_appointment_type_booking_code_mutation_dependencies()

        # --- Step 1: resolve and validate the code ---
        try:
            token = deps.calendar_permission_service.resolve_code(input.code)
        except InvalidTokenError:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Invalid or unknown booking code.",
            )
        except TokenExpiredError:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.EXPIRED,
                error_message="This booking code has expired.",
            )
        except TokenAlreadyUsedError:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.ALREADY_USED,
                error_message="This booking code has already been used.",
            )
        except TokenRevokedError:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.REVOKED,
                error_message="This booking code has been revoked.",
            )

        # --- Step 2: check permission — must hold CANCEL ---
        token_permissions = {p.permission for p in token.permissions.all()}
        if EventManagementPermissions.CANCEL not in token_permissions:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message="This code does not permit cancellation.",
            )

        # --- Step 3: scope check — must be event-scoped ---
        if token.event is None:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message="This code is not bound to a specific event.",
            )

        # --- Step 4: resolve org ---
        try:
            org = Organization.objects.get(id=token.organization_id)
        except Organization.DoesNotExist:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Invalid or unknown booking code.",
            )

        # --- Step 5: extract client IP for audit ---
        source_ip = _client_ip_from_request(info.context.request)

        # --- Step 6: capture event_id and calendar_id BEFORE the atomic block ---
        # The event will be deleted inside the transaction; capture its id and calendar
        # now so we can refer to them without querying a deleted row.
        event_id: int = token.event_fk_id  # type: ignore[assignment]
        # For single-calendar path only; appointment type path uses cancel_appointment_type_event.
        single_calendar_id: int | None = (
            None if token.appointment_type is not None else token.event.calendar_fk_id
        )

        # --- Step 7: atomic consume + delete ---
        # Consume FIRST via SELECT FOR UPDATE so concurrent replays fail under the row
        # lock before any delete attempt.  The event FK on the token has on_delete=CASCADE,
        # so deleting the event would cascade-delete the token — making a post-delete
        # consume impossible.  Consuming first keeps the row alive long enough to lock it,
        # then the cascade removes the already-consumed token row when the event is deleted.
        # If the delete step raises (unexpected), the whole transaction.atomic() block rolls
        # back, including the consume, so the code remains available for retry.
        try:
            with transaction.atomic():
                deps.calendar_permission_service.consume_code(token, source_ip)
                deps.calendar_service.initialize_without_provider(
                    user_or_token=input.code, organization=org
                )
                if token.appointment_type is not None:
                    # Appointment-type-cancel path: wire the same CalendarService instance so
                    # that permission checks run against the code's token.
                    deps.appointment_type_service.calendar_service = deps.calendar_service
                    deps.appointment_type_service.initialize(organization=org)
                    deps.appointment_type_service.cancel_appointment_type_event(
                        event_id=event_id,
                        delete_series=False,
                    )
                else:
                    # Single-calendar cancel path.
                    deps.calendar_service.delete_event(
                        calendar_id=single_calendar_id,  # type: ignore[arg-type]
                        event_id=event_id,
                        delete_series=False,
                    )
        except InvalidTokenError:
            # consume_code re-fetched under SELECT FOR UPDATE and found no row
            # (e.g. token was deleted between resolve_code and the lock).  This is
            # NOT a genuine authorization failure — surface it as INVALID_CODE.
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Invalid or unknown booking code.",
            )
        except (TokenAlreadyUsedError, TokenExpiredError, TokenRevokedError) as e:
            # Concurrent consumer won the race, or state changed between resolve and consume.
            error_code = BookingCodeErrorCode.ALREADY_USED
            error_message = "This booking code has already been used."
            if isinstance(e, TokenExpiredError):
                error_code = BookingCodeErrorCode.EXPIRED
                error_message = "This booking code has expired."
            elif isinstance(e, TokenRevokedError):
                error_code = BookingCodeErrorCode.REVOKED
                error_message = "This booking code has been revoked."
            return CodeEventResult(
                success=False,
                error_code=error_code,
                error_message=error_message,
            )
        except CalendarEvent.DoesNotExist:
            # The event was concurrently deleted between resolve_code and the delete call.
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Invalid or unknown booking code.",
            )
        except AppointmentTypeValidationError:
            # Appointment-type-path: the bound event is not actually an appointment-type event (scope mismatch),
            # or the cancel_appointment_type_event preconditions failed for a structural reason.
            # This is a permission/scope issue, not a slot-availability issue.
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message="This code does not permit cancellation of this event.",
            )
        except PermissionDenied:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message="This code does not permit cancellation of this event.",
            )
        except (EventManagementError, AppointmentTypeError):
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.SLOT_UNAVAILABLE,
                error_message="The event could not be cancelled.",
            )

        # The event is deleted; return success without attempting to include it.
        return CodeEventResult(success=True)


# ---------------------------------------------------------------------------
# ExternalEventChangeRequest mutations
# ---------------------------------------------------------------------------


@dataclass
class ExternalEventChangeRequestMutationDependencies:
    """Dependencies for ExternalEventChangeRequest mutations."""

    external_event_change_request_service: ExternalEventChangeRequestService
    calendar_service: "CalendarService"


@inject
def get_external_event_change_request_mutation_dependencies(
    external_event_change_request_service: Annotated[
        ExternalEventChangeRequestService | None,
        Provide["external_event_change_request_service"],
    ] = None,
    calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
) -> ExternalEventChangeRequestMutationDependencies:
    """Get ExternalEventChangeRequest mutation dependencies from DI container."""
    required = [external_event_change_request_service, calendar_service]
    if any(dep is None for dep in required):
        raise GraphQLError(
            f"Missing required dependency {', '.join([str(d) for d in required if d is None])}"
        )
    return ExternalEventChangeRequestMutationDependencies(
        external_event_change_request_service=cast(
            ExternalEventChangeRequestService, external_event_change_request_service
        ),
        calendar_service=cast("CalendarService", calendar_service),
    )


def _resolve_acting_membership_from_info(
    info: strawberry.Info, org: Organization
) -> OrganizationMembership:
    """Resolve the acting OrganizationMembership from the public-API request context.

    For scoped tokens (``scoped_to_membership_user_id`` is set), the
    membership is the one the token was scoped to.  For org-wide tokens
    (``scoped_to_membership_user_id`` is None), there is no user-level
    membership identity — raise ``GraphQLError`` so callers get a clean
    "membership required" error.

    Args:
        info: Strawberry GraphQL execution info carrying the request context.
        org: The organization the token belongs to (already resolved from
            ``request.public_api_organization``).

    Returns:
        The active ``OrganizationMembership`` for the scoped token's owner.

    Raises:
        GraphQLError: When the token is org-wide (no acting membership) or
            the membership is no longer active.
    """
    request = info.context.request
    system_user = getattr(request, "public_api_system_user", None)
    if system_user is None or system_user.scoped_to_membership_user_id is None:
        raise GraphQLError(
            "This operation requires a provider-scoped token with an associated membership. "
            "Org-wide tokens cannot approve or reject change requests."
        )
    try:
        return OrganizationMembership.objects.get(
            organization_id=org.id,
            user_id=system_user.scoped_to_membership_user_id,
            is_active=True,
        )
    except OrganizationMembership.DoesNotExist as exc:
        raise GraphQLError(
            "The token's scoped membership is no longer active in this organization."
        ) from exc


@strawberry.type
class ExternalEventChangeRequestMutations:
    """GraphQL mutations for approving and rejecting external event change requests."""

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def approve_external_event_change_request(
        self,
        info: strawberry.Info,
        id: int,  # noqa: A002
    ) -> ApproveExternalEventChangeRequestResult:
        """Approve a PENDING external event change request.

        Applies the proposed change locally (update: writes proposed field values;
        delete: removes the local event) and marks the request APPROVED.

        The acting membership is resolved from the token's ``scoped_to_membership``.
        Only provider-scoped tokens (with an associated membership) can resolve
        change requests — org-wide tokens are rejected.

        The token's OrganizationResourceAccess must include the
        EXTERNAL_EVENT_CHANGE_REQUEST resource.

        Returns:
            ApproveExternalEventChangeRequestResult with the updated change request
            on success; error_message set on failure.

        GraphQL errors:
        - Org-wide token (no acting membership) → GraphQLError.
        - Caller not eligible to resolve this request → GraphQLError (403 semantics).
        - Request is no longer PENDING → GraphQLError (409 semantics).
        """
        org = info.context.request.public_api_organization
        if not org:
            raise GraphQLError("Organization not found in request context")

        acting_membership = _resolve_acting_membership_from_info(info, org)
        deps = get_external_event_change_request_mutation_dependencies()

        try:
            change_request = ExternalEventChangeRequest.objects.filter_by_organization(org.id).get(
                id=id
            )
        except ExternalEventChangeRequest.DoesNotExist:
            return ApproveExternalEventChangeRequestResult(
                success=False,
                error_message="Change request not found.",
            )

        try:
            updated = deps.external_event_change_request_service.approve(
                change_request,
                membership=acting_membership,
            )
        except ChangeRequestIneligibleError as exc:
            raise GraphQLError(
                str(exc) or "You are not eligible to resolve this change request."
            ) from exc
        except ChangeRequestNotPendingError as exc:
            raise GraphQLError(str(exc) or "This change request is no longer pending.") from exc

        return ApproveExternalEventChangeRequestResult(
            success=True,
            change_request=updated,  # type: ignore[arg-type]
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def reject_external_event_change_request(
        self,
        info: strawberry.Info,
        id: int,  # noqa: A002
    ) -> RejectExternalEventChangeRequestResult:
        """Reject a PENDING external event change request, re-converging the provider.

        Pushes the retained local values back to the external provider (update:
        calls ``update_event`` with retained values; delete: re-creates the event
        on the provider via ``create_event`` and rebinds the local external id) and
        marks the request REJECTED.

        Authentication for the outbound provider write is established using the
        calendar owner's social account credentials — the same pattern used by the
        REST reject action. The calendar's primary ownership row is resolved
        to find the owner's SocialAccount for the calendar's provider.

        The acting membership is resolved from the token's ``scoped_to_membership``.
        Only provider-scoped tokens (with an associated membership) can resolve
        change requests — org-wide tokens are rejected.

        The token's OrganizationResourceAccess must include the
        EXTERNAL_EVENT_CHANGE_REQUEST resource.

        Returns:
            RejectExternalEventChangeRequestResult with the updated change request
            on success; error_message set on failure.

        GraphQL errors:
        - Org-wide token (no acting membership) → GraphQLError.
        - Caller not eligible to resolve this request → GraphQLError (403 semantics).
        - Request is no longer PENDING → GraphQLError (409 semantics).
        - No calendar / owner / social account found → GraphQLError (400 semantics).
        """
        org = info.context.request.public_api_organization
        if not org:
            raise GraphQLError("Organization not found in request context")

        acting_membership = _resolve_acting_membership_from_info(info, org)
        deps = get_external_event_change_request_mutation_dependencies()

        try:
            change_request = ExternalEventChangeRequest.objects.filter_by_organization(org.id).get(
                id=id
            )
        except ExternalEventChangeRequest.DoesNotExist:
            return RejectExternalEventChangeRequestResult(
                success=False,
                error_message="Change request not found.",
            )

        # Guard 1: non-PENDING → GraphQLError immediately, before any outbound-auth work.
        if change_request.status != ExternalEventChangeRequestStatus.PENDING:
            raise GraphQLError("This change request is no longer pending.")

        # Guard 2: event was deleted → ineligible to reject.
        event = change_request.event
        if event is None:
            raise GraphQLError("Cannot reject a change request with no associated event.")

        calendar = event.calendar
        if calendar is None:
            raise GraphQLError("Event has no associated calendar; cannot authenticate provider.")

        # Resolve the calendar owner and authenticate the CalendarService using the
        # owner's social account credentials (matching the REST reject pattern).
        # The calendar's primary ownership row determines which social account to use.
        ownership = (
            CalendarOwnership.objects.filter_by_organization(calendar.organization_id)
            .filter(
                calendar=calendar,
                membership_user_id__isnull=False,
            )
            .order_by("-is_default", "id")
            .first()
        )
        if not ownership:
            raise GraphQLError("Calendar has no owner; cannot authenticate with provider.")

        owner_social_account = SocialAccount.objects.filter(
            user_id=ownership.membership_user_id, provider=calendar.provider
        ).first()
        if not owner_social_account:
            raise GraphQLError(
                f"Calendar owner has no linked {calendar.provider} account; "
                "cannot push the undo to the provider."
            )

        # Authenticate the CalendarService and resolve the write adapter.
        # Both of these raise OverLimitError when the organization lacks the
        # relevant external-calendar entitlement -- authenticate() on the *authenticated
        # account's* provider, _get_write_adapter_for_calendar() on the *calendar's*
        # (they can differ; see that method's docstring). Rendered via
        # raise_over_limit_graphql_error (which also rolls back the request transaction
        # -- see that function's docstring for why that matters under ATOMIC_REQUESTS).
        try:
            deps.calendar_service.authenticate(
                account=owner_social_account,
                organization=org,
            )
            write_adapter = deps.calendar_service._get_write_adapter_for_calendar(calendar)
        except OverLimitError as exc:
            raise_over_limit_graphql_error(exc)
        if write_adapter is None:
            raise GraphQLError("Could not resolve a write adapter for the calendar's provider.")

        try:
            updated = deps.external_event_change_request_service.reject(
                change_request,
                membership=acting_membership,
                write_adapter=write_adapter,
            )
        except ChangeRequestIneligibleError as exc:
            raise GraphQLError(
                str(exc) or "You are not eligible to resolve this change request."
            ) from exc
        except ChangeRequestNotPendingError as exc:
            raise GraphQLError(str(exc) or "This change request is no longer pending.") from exc

        return RejectExternalEventChangeRequestResult(
            success=True,
            change_request=updated,  # type: ignore[arg-type]
        )
