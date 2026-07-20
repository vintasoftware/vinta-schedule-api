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

from calendar_integration.constants import ExternalEventChangeRequestStatus
from calendar_integration.exceptions import (
    BookingPolicyViolationError,
    CalendarGroupError,
    CalendarGroupValidationError,
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
    ApproveExternalEventChangeRequestResult,
    BookingCodeErrorCode,
    BookingCodeResult,
    CalendarEventGraphQLType,
    CalendarGroupGraphQLType,
    CalendarWebhookSubscriptionGraphQLType,
    CodeEventResult,
    RejectExternalEventChangeRequestResult,
)
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarGroup,
    CalendarOwnership,
    EventManagementPermissions,
    ExternalEventChangeRequest,
)
from calendar_integration.services.dataclasses import (
    CalendarEventInputData,
    CalendarGroupEventInputData,
    CalendarGroupInputData,
    CalendarGroupSlotInputData,
    CalendarGroupSlotSelectionInputData,
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
from payments.exceptions import OverLimitError
from public_api.extensions import raise_over_limit_graphql_error
from public_api.permissions import IsAuthenticated, OrganizationResourceAccess


if TYPE_CHECKING:
    from calendar_integration.services.calendar_group_service import CalendarGroupService
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
            from calendar_integration.models import Calendar

            try:
                calendar = Calendar.objects.get(id=input.calendar_id, organization=organization)
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
# CalendarGroup mutations
# ---------------------------------------------------------------------------


@dataclass
class CalendarGroupMutationDependencies:
    """Dependencies for CalendarGroup mutations."""

    calendar_group_service: "CalendarGroupService"
    calendar_service: "CalendarService"


@inject
def get_calendar_group_mutation_dependencies(
    calendar_group_service: Annotated[
        "CalendarGroupService | None", Provide["calendar_group_service"]
    ] = None,
    calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
) -> CalendarGroupMutationDependencies:
    required = [calendar_group_service, calendar_service]
    if any(dep is None for dep in required):
        raise GraphQLError(
            f"Missing required dependency {', '.join([str(d) for d in required if d is None])}"
        )
    return CalendarGroupMutationDependencies(
        calendar_group_service=cast("CalendarGroupService", calendar_group_service),
        calendar_service=cast("CalendarService", calendar_service),
    )


@strawberry.input
class CalendarGroupSlotInput:
    name: str
    calendar_ids: list[int]
    required_count: int = 1
    description: str = ""
    order: int = 0


@strawberry.input
class CalendarGroupInput:
    organization_id: int
    name: str
    description: str = ""
    slots: list[CalendarGroupSlotInput] = strawberry.field(default_factory=list)
    is_private: bool = True


@strawberry.input
class UpdateCalendarGroupInput:
    organization_id: int
    group_id: int
    name: str
    description: str = ""
    slots: list[CalendarGroupSlotInput] = strawberry.field(default_factory=list)
    is_private: bool | None = None


@strawberry.input
class DeleteCalendarGroupInput:
    organization_id: int
    group_id: int


@strawberry.input
class CalendarGroupSlotSelectionInput:
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
class CalendarGroupEventInput:
    organization_id: int
    group_id: int
    title: str
    description: str
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str
    slot_selections: list[CalendarGroupSlotSelectionInput]
    attendances: list[EventAttendanceInput] = strawberry.field(default_factory=list)
    external_attendances: list[EventExternalAttendanceInput] = strawberry.field(
        default_factory=list
    )


@strawberry.type
class CalendarGroupResult:
    success: bool
    group: CalendarGroupGraphQLType | None = None
    error_message: str | None = None


@strawberry.type
class DeleteCalendarGroupResult:
    success: bool
    error_message: str | None = None


@strawberry.type
class CalendarGroupEventResult:
    success: bool
    event: CalendarEventGraphQLType | None = None
    error_message: str | None = None


def _to_slot_input_data(slots: list[CalendarGroupSlotInput]) -> list[CalendarGroupSlotInputData]:
    return [
        CalendarGroupSlotInputData(
            name=s.name,
            calendar_ids=list(s.calendar_ids),
            required_count=s.required_count,
            description=s.description,
            order=s.order,
        )
        for s in slots
    ]


def _load_organization(organization_id: int) -> Organization | None:
    try:
        return Organization.objects.get(id=organization_id)
    except Organization.DoesNotExist:
        return None


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
# Booking-code mint mutations (Phase 1)
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
class GroupBookingCodeMutationDependencies:
    """Dependencies for the unauthenticated group-booking-code mutations."""

    calendar_permission_service: "CalendarPermissionService"
    calendar_service: "CalendarService"
    calendar_group_service: "CalendarGroupService"


@inject
def get_group_booking_code_mutation_dependencies(
    calendar_permission_service: Annotated[
        "CalendarPermissionService | None", Provide["calendar_permission_service"]
    ] = None,
    calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
    calendar_group_service: Annotated[
        "CalendarGroupService | None", Provide["calendar_group_service"]
    ] = None,
) -> GroupBookingCodeMutationDependencies:
    """Get group-booking-code mutation dependencies from DI container.

    The DI container wires ``calendar_group_service.calendar_service`` to the
    same ``CalendarService`` factory instance that is returned as
    ``calendar_service`` here, so initialising ``calendar_service`` with a
    booking code automatically propagates to ``calendar_group_service``.
    """
    if (
        calendar_permission_service is None
        or calendar_service is None
        or calendar_group_service is None
    ):
        raise GraphQLError("Internal server error.")
    return GroupBookingCodeMutationDependencies(
        calendar_permission_service=cast("CalendarPermissionService", calendar_permission_service),
        calendar_service=cast("CalendarService", calendar_service),
        calendar_group_service=cast("CalendarGroupService", calendar_group_service),
    )


@strawberry.input
class CreateBookingCodeInput:
    """Input for minting a single-use calendar booking code."""

    organization_id: int
    calendar_id: int
    expires_at: datetime.datetime | None = None


@strawberry.input
class CreateGroupBookingCodeInput:
    """Input for minting a single-use calendar-group booking code."""

    organization_id: int
    calendar_group_id: int
    expires_at: datetime.datetime | None = None


@strawberry.input
class CreateEventCodeInput:
    """Input for minting a single-use reschedule or cancel code scoped to a calendar + event."""

    organization_id: int
    calendar_id: int
    event_id: int
    expires_at: datetime.datetime | None = None


@strawberry.input
class CreateGroupEventCodeInput:
    """Input for minting a single-use reschedule or cancel code scoped to a calendar group + event."""

    organization_id: int
    calendar_group_id: int
    event_id: int
    expires_at: datetime.datetime | None = None


@strawberry.input
class RevokeBookingCodeInput:
    """Input for revoking a single-use booking code."""

    organization_id: int
    id: int  # noqa: A002


# ---------------------------------------------------------------------------
# With-code booking inputs (Phase 5a)
# ---------------------------------------------------------------------------


@strawberry.input
class ExternalAttendeeCodeInput:
    """External attendee input for unauthenticated code-bearing booking mutations."""

    email: str
    name: str = ""


@strawberry.input
class CodeSlotSelectionInput:
    """Per-slot calendar selection for the unauthenticated group-booking mutation."""

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
class CreateGroupEventWithCodeInput:
    """Input for the unauthenticated createCalendarGroupEventWithCode mutation."""

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
class RescheduleGroupWithCodeInput:
    """Input for the unauthenticated rescheduleCalendarGroupEventWithCode mutation.

    Slot selections are NOT included: v1 keeps existing group/calendar selections
    and changes ONLY the event times.  Full slot re-selection is deferred to a
    future version (see Open Question 3 in the implementation plan).
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
class CalendarGroupMutations:
    """GraphQL mutations for CalendarGroup CRUD and grouped event booking."""

    @strawberry.mutation
    def create_calendar_group(
        self,
        input: CalendarGroupInput,  # noqa: A002
    ) -> CalendarGroupResult:
        organization = _load_organization(input.organization_id)
        if organization is None:
            return CalendarGroupResult(success=False, error_message="Organization not found")
        deps = get_calendar_group_mutation_dependencies()
        deps.calendar_group_service.initialize(organization=organization)
        # Phase 6b: create_group raises OverLimitError at the organization's
        # calendar_groups limit. Rendered identically to the REST 402 body via
        # raise_over_limit_graphql_error (also rolls back the request transaction --
        # graphql-core swallows resolver exceptions and always returns 200).
        try:
            group = deps.calendar_group_service.create_group(
                CalendarGroupInputData(
                    name=input.name,
                    description=input.description,
                    slots=_to_slot_input_data(input.slots),
                    accepts_public_scheduling=not input.is_private,
                )
            )
        except OverLimitError as exc:
            raise_over_limit_graphql_error(exc)
        except CalendarGroupError as e:
            return CalendarGroupResult(success=False, error_message=str(e))
        return CalendarGroupResult(success=True, group=group)  # type: ignore[arg-type]

    @strawberry.mutation
    def update_calendar_group(
        self,
        input: UpdateCalendarGroupInput,  # noqa: A002
    ) -> CalendarGroupResult:
        organization = _load_organization(input.organization_id)
        if organization is None:
            return CalendarGroupResult(success=False, error_message="Organization not found")
        deps = get_calendar_group_mutation_dependencies()
        deps.calendar_group_service.initialize(organization=organization)
        try:
            accepts_public_scheduling = None if input.is_private is None else not input.is_private
            group = deps.calendar_group_service.update_group(
                group_id=input.group_id,
                data=CalendarGroupInputData(
                    name=input.name,
                    description=input.description,
                    slots=_to_slot_input_data(input.slots),
                    accepts_public_scheduling=accepts_public_scheduling,
                ),
            )
        except CalendarGroup.DoesNotExist:
            return CalendarGroupResult(success=False, error_message="Group not found")
        except CalendarGroupError as e:
            return CalendarGroupResult(success=False, error_message=str(e))
        return CalendarGroupResult(success=True, group=group)  # type: ignore[arg-type]

    @strawberry.mutation
    def delete_calendar_group(
        self,
        input: DeleteCalendarGroupInput,  # noqa: A002
    ) -> DeleteCalendarGroupResult:
        organization = _load_organization(input.organization_id)
        if organization is None:
            return DeleteCalendarGroupResult(success=False, error_message="Organization not found")
        deps = get_calendar_group_mutation_dependencies()
        deps.calendar_group_service.initialize(organization=organization)
        try:
            deps.calendar_group_service.delete_group(group_id=input.group_id)
        except CalendarGroup.DoesNotExist:
            return DeleteCalendarGroupResult(success=False, error_message="Group not found")
        except CalendarGroupError as e:
            return DeleteCalendarGroupResult(success=False, error_message=str(e))
        return DeleteCalendarGroupResult(success=True)

    @strawberry.mutation
    def create_calendar_group_event(
        self,
        input: CalendarGroupEventInput,  # noqa: A002
    ) -> CalendarGroupEventResult:
        organization = _load_organization(input.organization_id)
        if organization is None:
            return CalendarGroupEventResult(success=False, error_message="Organization not found")
        deps = get_calendar_group_mutation_dependencies()
        deps.calendar_service.initialize_without_provider(organization=organization)
        deps.calendar_group_service.initialize(organization=organization)
        data = CalendarGroupEventInputData(
            title=input.title,
            description=input.description,
            start_time=input.start_time,
            end_time=input.end_time,
            timezone=input.timezone,
            group_id=input.group_id,
            slot_selections=[
                CalendarGroupSlotSelectionInputData(
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
            event = deps.calendar_group_service.create_grouped_event(data)
        except CalendarGroup.DoesNotExist:
            return CalendarGroupEventResult(success=False, error_message="Group not found")
        except PermissionDenied as e:
            return CalendarGroupEventResult(success=False, error_message=str(e))
        except PermissionServiceInitializationError:
            return CalendarGroupEventResult(
                success=False,
                error_message=(
                    "This group does not accept public scheduling. "
                    "A token or scheduling code is required."
                ),
            )
        except BookingPolicyViolationError as e:
            return CalendarGroupEventResult(
                success=False,
                error_message=(
                    str(e)
                    or "The requested time slot is not available under the current booking policy."
                ),
            )
        except CalendarGroupError as e:
            return CalendarGroupEventResult(success=False, error_message=str(e))
        return CalendarGroupEventResult(success=True, event=event)  # type: ignore[arg-type]

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
    def create_calendar_group_booking_code(
        self,
        info: strawberry.Info,
        input: CreateGroupBookingCodeInput,  # noqa: A002
    ) -> BookingCodeResult:
        """Mint a single-use booking code scoped to a calendar group.

        The token grants CREATE permission, allowing the code-bearer to book
        an event against the bound calendar group.  The code is returned once
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

        # Verify the calendar group belongs to the authenticated org.
        try:
            CalendarGroup.objects.filter_by_organization(org.id).get(id=input.calendar_group_id)
        except CalendarGroup.DoesNotExist:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Calendar group not found.",
            )

        minted_by = getattr(info.context.request, "public_api_system_user", None)
        deps = get_booking_code_mutation_dependencies()
        token, plaintext_code = deps.calendar_permission_service.create_booking_token(
            organization_id=org.id,
            permissions=[EventManagementPermissions.CREATE],
            expires_at=input.expires_at,
            minted_by=minted_by,
            calendar_group_id=input.calendar_group_id,
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

        # Verify the event belongs to this org AND to the named calendar, and is not a grouped event.
        try:
            CalendarEvent.objects.filter_by_organization(org.id).get(
                id=input.event_id,
                calendar_fk_id=input.calendar_id,
                calendar_group_fk_id__isnull=True,
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
    def create_calendar_group_reschedule_booking_code(
        self,
        info: strawberry.Info,
        input: CreateGroupEventCodeInput,  # noqa: A002
    ) -> BookingCodeResult:
        """Mint a single-use reschedule code bound to a specific event on a calendar group.

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

        # Verify the calendar group belongs to the authenticated org.
        try:
            CalendarGroup.objects.filter_by_organization(org.id).get(id=input.calendar_group_id)
        except CalendarGroup.DoesNotExist:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Not found.",
            )

        # Verify the event belongs to this org AND to the named calendar group.
        try:
            CalendarEvent.objects.filter_by_organization(org.id).get(
                id=input.event_id,
                calendar_group_fk_id=input.calendar_group_id,
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
            calendar_group_id=input.calendar_group_id,
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

        # Verify the event belongs to this org AND to the named calendar, and is not a grouped event.
        try:
            CalendarEvent.objects.filter_by_organization(org.id).get(
                id=input.event_id,
                calendar_fk_id=input.calendar_id,
                calendar_group_fk_id__isnull=True,
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
    def create_calendar_group_cancellation_booking_code(
        self,
        info: strawberry.Info,
        input: CreateGroupEventCodeInput,  # noqa: A002
    ) -> BookingCodeResult:
        """Mint a single-use cancellation code bound to a specific event on a calendar group.

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

        # Verify the calendar group belongs to the authenticated org.
        try:
            CalendarGroup.objects.filter_by_organization(org.id).get(id=input.calendar_group_id)
        except CalendarGroup.DoesNotExist:
            return BookingCodeResult(
                success=False,
                error_code=BookingCodeErrorCode.INVALID_CODE,
                error_message="Not found.",
            )

        # Verify the event belongs to this org AND to the named calendar group.
        try:
            CalendarEvent.objects.filter_by_organization(org.id).get(
                id=input.event_id,
                calendar_group_fk_id=input.calendar_group_id,
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
            calendar_group_id=input.calendar_group_id,
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

        deps = get_booking_code_mutation_dependencies()
        try:
            deps.calendar_permission_service.revoke_token(organization_id=org.id, token_id=input.id)
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

        # --- Step 3: scope check — must be single-calendar (not group) ---
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
        # Phase 8: create_event raises OverLimitError at the organization's postpaid
        # event_occurrences allowance (no payment method on file). Unlike the domain
        # errors above, this is not a booking-code-specific outcome the patient can
        # retry around, so it is rendered via the shared over-limit GraphQL contract
        # (raise_over_limit_graphql_error, also rolls back the request transaction --
        # see its docstring) rather than a CodeEventResult error_code.
        except OverLimitError as exc:
            raise_over_limit_graphql_error(exc)

        return CodeEventResult(success=True, event=event)  # type: ignore[arg-type]

    @strawberry.mutation
    def create_calendar_group_event_with_code(
        self,
        info: strawberry.Info,
        input: CreateGroupEventWithCodeInput,  # noqa: A002
    ) -> CodeEventResult:
        """Book a grouped calendar event using a single-use group booking code.

        This is an unauthenticated mutation: no org token is required.  The org
        context, permissions, and group scope are all derived from the booking
        code.  On success the code is atomically consumed so it cannot be
        replayed.  On a failed create (slot unavailable, invalid selection, etc.)
        the code is NOT consumed and the patient may retry.

        The group_id is taken STRICTLY from the token — the client cannot
        override it via the input.
        """
        deps = get_group_booking_code_mutation_dependencies()

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

        # --- Step 3: scope check — must be group-scoped (not single-calendar) ---
        if token.calendar_group is None:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message=(
                    "This code is not scoped to a calendar group. "
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

        # --- Step 6: build group event data ---
        # group_id comes from the token — not from client input — to enforce scope.
        group_event_data = CalendarGroupEventInputData(
            group_id=token.calendar_group.id,
            title=input.title,
            description=input.description or "",
            start_time=input.start_time,
            end_time=input.end_time,
            timezone=input.timezone,
            slot_selections=[
                CalendarGroupSlotSelectionInputData(
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
        # Explicitly wire it into ``deps.calendar_group_service`` so that the event
        # is created on the same CalendarService instance that carries the booking
        # code's token — this is necessary because the DI container's Factory
        # provider gives CalendarGroupService its OWN CalendarService instance via its
        # @inject __init__.  Without this explicit wiring the primary-calendar create
        # would use an uninitialized instance and the permission / availability checks
        # would fail.
        try:
            with transaction.atomic():
                deps.calendar_service.initialize_without_provider(
                    user_or_token=input.code, organization=org
                )
                deps.calendar_group_service.calendar_service = deps.calendar_service
                # Share the token-initialized permission service so the group-level
                # ``can_perform_group_scheduling`` gate can read the group-scoped token.
                # Without this the group service would hold a separate, uninitialized
                # CalendarPermissionService instance and deny private-group bookings
                # even when a valid group-scoped code was provided.
                deps.calendar_group_service.calendar_permission_service = (
                    deps.calendar_service.calendar_permission_service
                )
                deps.calendar_group_service.initialize(organization=org)
                event = deps.calendar_group_service.create_grouped_event(group_event_data)
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
        except (EventManagementError, CalendarGroupError):
            # Slot taken / invalid selection / invalid times — code NOT consumed (txn rolled
            # back), patient may retry with a different slot.
            # Note: NoAvailableTimeWindowsError is a subclass of EventManagementError and
            # is therefore already covered by the EventManagementError branch.
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.SLOT_UNAVAILABLE,
                error_message="The requested time slot is not available.",
            )
        # Phase 8: create_grouped_event raises OverLimitError at the organization's
        # postpaid event_occurrences allowance (no payment method on file). Rendered
        # via raise_over_limit_graphql_error (also rolls back the request transaction
        # -- see its docstring), like the single-calendar booking-code path above.
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

        # --- Step 3: scope check — must be event-scoped and single-calendar (not group) ---
        if token.event is None:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message="This code is not bound to a specific event.",
            )

        # A group-reschedule code has calendar_group set; route to Phase 6b instead.
        if token.calendar_group is not None:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message=(
                    "This code is scoped to a calendar group. "
                    "Use rescheduleCalendarGroupEventWithCode for group-scoped codes."
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
        except (EventManagementError, CalendarGroupError):
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
    def reschedule_calendar_group_event_with_code(
        self,
        info: strawberry.Info,
        input: RescheduleGroupWithCodeInput,  # noqa: A002
    ) -> CodeEventResult:
        """Reschedule a grouped event bound to a single-use GROUP RESCHEDULE code.

        This is an unauthenticated mutation: no org token is required.  The org
        context, calendar-group scope, and the specific grouped event to reschedule
        are all derived from the booking code.  On success the code is atomically
        consumed so it cannot be replayed.  On a failed reschedule (slot outside
        availability, etc.) the code is NOT consumed and the patient may retry.

        Only the start/end/timezone fields change — title, description, attendees,
        resource allocations, and the group's calendar selections are preserved
        exactly from the existing event (time-only v1; full slot re-selection is
        deferred per Open Question 3).  The event id is preserved so that external
        integrations (e.g. Building Blocks) continue to reference the same event.
        """
        deps = get_group_booking_code_mutation_dependencies()

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

        # --- Step 3: scope check — must be event-scoped AND group-scoped ---
        if token.event is None:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message="This code is not bound to a specific event.",
            )

        # A single-calendar reschedule code has no calendar_group; route to Phase 6a.
        if token.calendar_group is None:
            return CodeEventResult(
                success=False,
                error_code=BookingCodeErrorCode.NOT_PERMITTED,
                error_message=(
                    "This code is not scoped to a calendar group. "
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
        # only ever affect the exact grouped event it was minted for.
        event_id: int = token.event_fk_id  # type: ignore[assignment]

        # --- Step 7: availability pre-check (code-path only) ---
        # For the primary calendar of the bound grouped event: if it manages
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
        # ``deps.calendar_service`` is explicitly wired into ``deps.calendar_group_service``
        # so that the update runs on the same code-initialized CalendarService instance.
        try:
            with transaction.atomic():
                deps.calendar_service.initialize_without_provider(
                    user_or_token=input.code, organization=org
                )
                deps.calendar_group_service.calendar_service = deps.calendar_service
                deps.calendar_group_service.initialize(organization=org)
                event = deps.calendar_group_service.reschedule_grouped_event(
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
        except (EventManagementError, CalendarGroupError):
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
        context, scope (single-calendar or group), and the specific event to cancel
        are all derived from the booking code.  On success the code is atomically
        consumed so it cannot be replayed.

        Handles both a calendar-bound (non-grouped) cancel code and a group-bound
        (grouped event) cancel code via the SAME mutation.  The routing is determined
        by whether ``token.calendar_group`` is set.

        For grouped events the non-primary ``BlockedTime`` rows (linked only by the
        string ``external_id`` convention) are explicitly deleted before the primary
        event is removed, so no orphaned busy-markers remain.
        """
        deps = get_group_booking_code_mutation_dependencies()

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
        # For single-calendar path only; group path uses cancel_grouped_event.
        single_calendar_id: int | None = (
            None if token.calendar_group is not None else token.event.calendar_fk_id
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
                if token.calendar_group is not None:
                    # Group-cancel path: wire the same CalendarService instance so
                    # that permission checks run against the code's token.
                    deps.calendar_group_service.calendar_service = deps.calendar_service
                    deps.calendar_group_service.initialize(organization=org)
                    deps.calendar_group_service.cancel_grouped_event(
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
        except CalendarGroupValidationError:
            # Group-path: the bound event is not actually a grouped event (scope mismatch),
            # or the cancel_grouped_event preconditions failed for a structural reason.
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
        except (EventManagementError, CalendarGroupError):
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
        REST Phase 8a reject action. The calendar's primary ownership row is resolved
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
        # owner's social account credentials (matching the REST phase 8a pattern).
        # The calendar's primary ownership row determines which social account to use.
        ownership = (
            CalendarOwnership.objects.filter(
                calendar=calendar,
                organization_id=calendar.organization_id,
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
        # Phase 6c: both of these raise OverLimitError when the organization lacks the
        # relevant external-calendar entitlement -- authenticate() on the *authenticated
        # account's* provider, _get_write_adapter_for_calendar() on the *calendar's*
        # (they can differ; see that method's docstring). Rendered via
        # raise_over_limit_graphql_error (also rolls back the request transaction -- see
        # that function's docstring for why that matters under ATOMIC_REQUESTS).
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
