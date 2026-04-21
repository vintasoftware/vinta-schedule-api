"""GraphQL mutations for calendar integration webhook management."""

import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, cast

import strawberry
from dependency_injector.wiring import Provide, inject
from graphql import GraphQLError

from calendar_integration.exceptions import CalendarGroupError
from calendar_integration.graphql import (
    CalendarEventGraphQLType,
    CalendarGroupGraphQLType,
    CalendarWebhookSubscriptionGraphQLType,
)
from calendar_integration.models import CalendarGroup
from calendar_integration.services.dataclasses import (
    CalendarGroupEventInputData,
    CalendarGroupInputData,
    CalendarGroupSlotInputData,
    CalendarGroupSlotSelectionInputData,
    EventAttendanceInputData,
    EventExternalAttendanceInputData,
    ExternalAttendeeInputData,
)
from calendar_integration.services.webhook_analytics_service import WebhookAnalyticsService
from organizations.models import Organization


if TYPE_CHECKING:
    from calendar_integration.services.calendar_group_service import CalendarGroupService
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


@strawberry.input
class UpdateCalendarGroupInput:
    organization_id: int
    group_id: int
    name: str
    description: str = ""
    slots: list[CalendarGroupSlotInput] = strawberry.field(default_factory=list)


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
        try:
            group = deps.calendar_group_service.create_group(
                CalendarGroupInputData(
                    name=input.name,
                    description=input.description,
                    slots=_to_slot_input_data(input.slots),
                )
            )
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
            group = deps.calendar_group_service.update_group(
                group_id=input.group_id,
                data=CalendarGroupInputData(
                    name=input.name,
                    description=input.description,
                    slots=_to_slot_input_data(input.slots),
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
        except CalendarGroupError as e:
            return CalendarGroupEventResult(success=False, error_message=str(e))
        return CalendarGroupEventResult(success=True, event=event)  # type: ignore[arg-type]
