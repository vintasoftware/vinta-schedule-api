"""GraphQL mutations for calendar integration webhook management."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, cast

import strawberry
from dependency_injector.wiring import Provide, inject
from graphql import GraphQLError

from calendar_integration.graphql import CalendarWebhookSubscriptionGraphQLType
from calendar_integration.services.webhook_analytics_service import WebhookAnalyticsService
from organizations.models import Organization


if TYPE_CHECKING:
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
