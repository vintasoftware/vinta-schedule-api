import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, TypeVar, cast

from django.db.models import Value
from django.db.models.functions import Concat

import strawberry
import strawberry_django
from dependency_injector.wiring import Provide, inject
from django_virtual_models import QuerySet
from graphql import GraphQLError

from calendar_integration.graphql import (
    AvailableTimeGraphQLType,
    AvailableTimeWindowGraphQLType,
    BlockedTimeGraphQLType,
    CalendarEventGraphQLType,
    CalendarGraphQLType,
    CalendarWebhookEventGraphQLType,
    CalendarWebhookSubscriptionGraphQLType,
    UnavailableTimeWindowGraphQLType,
    WebhookSubscriptionStatusGraphQLType,
)
from calendar_integration.models import AvailableTime, BlockedTime, Calendar, CalendarEvent
from public_api.permissions import (
    IsAuthenticated,
    OrganizationResourceAccess,
)
from public_api.types import PublicApiHttpRequest
from users.graphql import UserGraphQLType
from users.models import User


if TYPE_CHECKING:
    from calendar_integration.services.calendar_service import CalendarService


@dataclass
class QueryDependencies:
    calendar_service: "CalendarService"


@inject
def get_query_dependencies(
    calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
) -> QueryDependencies:
    required_dependencies = [calendar_service]
    if any(dep is None for dep in required_dependencies):
        raise GraphQLError(
            f"Missing required dependency {', '.join([str(dep) for dep in required_dependencies if dep is None])}"
        )

    return QueryDependencies(
        calendar_service=cast("CalendarService", calendar_service),
    )


def _get_org(info: strawberry.Info):
    org = info.context.request.public_api_organization
    if not org:
        raise GraphQLError("Organization not found in request context")
    return org


TQuerySet = TypeVar("TQuerySet", bound=QuerySet)


def _slice_qs(qs: TQuerySet, offset: int, limit: int) -> TQuerySet:
    if offset < 0:
        raise GraphQLError("Offset must be non-negative")
    if limit <= 0 or limit > 100:
        raise GraphQLError("Limit must be between 1 and 100")
    return qs[offset : offset + limit]


def _prepare_service_and_calendar(
    info: strawberry.Info, calendar_id: int
) -> tuple["CalendarService", Calendar]:
    org = _get_org(info)
    deps = get_query_dependencies()
    request: PublicApiHttpRequest = info.context.request
    deps.calendar_service.initialize_without_provider(
        user_or_token=request.public_api_system_user, organization=org
    )
    cal = Calendar.objects.filter_by_organization(org.id).get(id=calendar_id)
    return deps.calendar_service, cal


@strawberry.type
class Query:
    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def calendars(
        self,
        info: strawberry.Info,
        calendar_id: int | None = None,
        user_id: int | None = None,
        calendar_type: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[CalendarGraphQLType]:
        """Get calendars filtered by user's organization."""
        org = _get_org(info)

        # Validate pagination parameters
        if offset < 0:
            raise GraphQLError("Offset must be non-negative")
        if limit <= 0 or limit > 100:
            raise GraphQLError("Limit must be between 1 and 100")

        queryset = Calendar.objects.filter_by_organization(org.id)
        if calendar_id is not None:
            queryset = queryset.filter(id=calendar_id)

        # Optional filter by owner user (via CalendarOwnership)
        if user_id is not None:
            # related_name on CalendarOwnership is `ownerships`
            queryset = queryset.filter(ownerships__user_id=user_id)

        # Optional filter by calendar type
        if calendar_type is not None:
            queryset = queryset.filter(calendar_type=calendar_type)

        # Apply ordering first, then pagination
        queryset = _slice_qs(queryset.order_by("pk"), offset, limit)

        return list(queryset)

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def calendar_events(
        self,
        info: strawberry.Info,
        calendar_id: int | None = None,
        start_datetime: datetime.datetime | None = None,
        end_datetime: datetime.datetime | None = None,
        event_id: int | None = None,
    ) -> list[CalendarEventGraphQLType]:
        """Get calendar events filtered by user's organization."""
        # Get the user's organization from the GraphQL context
        org = _get_org(info)

        if event_id is not None:
            return CalendarEvent.objects.filter_by_organization(org.id).filter(id=event_id)

        if not calendar_id or not start_datetime or not end_datetime:
            raise GraphQLError(
                "Missing required parameters. If not filtered by id, querying events require "
                "calendarId, startDatetime, and endDatetime. "
            )

        calendar_service, calendar = _prepare_service_and_calendar(info, calendar_id)
        events = calendar_service.get_calendar_events_expanded(
            calendar,
            start_datetime,
            end_datetime,
        )

        return cast(
            list[CalendarEventGraphQLType],
            events,
        )

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def blocked_times(
        self,
        info: strawberry.Info,
        calendar_id: int | None = None,
        start_datetime: datetime.datetime | None = None,
        end_datetime: datetime.datetime | None = None,
        blocked_time_id: int | None = None,
    ) -> list[BlockedTimeGraphQLType]:
        """Get blocked times filtered by user's organization."""
        # Get the user's organization from the GraphQL context
        org = _get_org(info)

        if blocked_time_id is not None:
            return BlockedTime.objects.filter_by_organization(org.id).filter(id=blocked_time_id)

        if not calendar_id or not start_datetime or not end_datetime:
            raise GraphQLError(
                "Missing required parameters. If not filtered by id, querying blocked times "
                "require calendarId, startDatetime, and endDatetime. "
            )

        calendar_service, calendar = _prepare_service_and_calendar(info, calendar_id)

        blocked_times = calendar_service.get_blocked_times_expanded(
            calendar,
            start_datetime,
            end_datetime,
        )

        return cast(
            list[BlockedTimeGraphQLType],
            blocked_times,
        )

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def available_times(
        self,
        info: strawberry.Info,
        calendar_id: int | None = None,
        start_datetime: datetime.datetime | None = None,
        end_datetime: datetime.datetime | None = None,
        available_time_id: int | None = None,
    ) -> list[AvailableTimeGraphQLType]:
        """Get available times filtered by user's organization."""
        # Get the user's organization from the GraphQL context
        org = _get_org(info)

        if available_time_id is not None:
            return AvailableTime.objects.filter_by_organization(org.id).filter(id=available_time_id)

        if not calendar_id or not start_datetime or not end_datetime:
            raise GraphQLError(
                "Missing required parameters. If not filtered by id, querying available times "
                "require calendarId, startDatetime, and endDatetime. "
            )

        calendar_service, calendar = _prepare_service_and_calendar(info, calendar_id)

        available_times = calendar_service.get_available_times_expanded(
            calendar,
            start_datetime,
            end_datetime,
        )

        return cast(
            list[AvailableTimeGraphQLType],
            available_times,
        )

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def users(
        self,
        info: strawberry.Info,
        user_id: int | None = None,
        name: str | None = None,
        email: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[UserGraphQLType]:
        """Get users filtered by user's organization."""
        org = _get_org(info)

        queryset = User.objects.filter(organization_membership__organization=org)
        if user_id is not None:
            queryset = queryset.filter(id=user_id)

        # Filter by concatenated profile first + last name (case-insensitive contains)
        if name is not None:
            queryset = queryset.annotate(
                full_name=Concat("profile__first_name", Value(" "), "profile__last_name")
            ).filter(full_name__icontains=name)

        # Filter by email (case-insensitive contains)
        if email is not None:
            queryset = queryset.filter(email__icontains=email)

        # Apply ordering first, then pagination
        queryset = _slice_qs(queryset.order_by("pk"), offset, limit)

        # Return a concrete list and cast to the declared GraphQL return type so
        # mypy recognizes the return value matches the annotation.
        return cast(
            list[UserGraphQLType],
            list(queryset),
        )

    @strawberry.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def availability_windows(
        self,
        info: strawberry.Info,
        calendar_id: int,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
    ) -> list[AvailableTimeWindowGraphQLType]:
        """Get availability windows for a calendar within a date range."""
        calendar_service, calendar = _prepare_service_and_calendar(info, calendar_id)

        # Get the availability windows
        availability_windows = calendar_service.get_availability_windows_in_range(
            calendar=calendar,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
        )

        # Convert to GraphQL types
        return [
            AvailableTimeWindowGraphQLType(
                start_time=window.start_time,
                end_time=window.end_time,
                id=window.id,
                can_book_partially=window.can_book_partially,
            )
            for window in availability_windows
        ]

    @strawberry.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def unavailable_windows(
        self,
        info: strawberry.Info,
        calendar_id: int,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
    ) -> list[UnavailableTimeWindowGraphQLType]:
        """Get unavailable (blocked or event) windows for a calendar within a date range."""
        calendar_service, calendar = _prepare_service_and_calendar(info, calendar_id)

        unavailable_windows = calendar_service.get_unavailable_time_windows_in_range(
            calendar=calendar, start_datetime=start_datetime, end_datetime=end_datetime
        )

        return [
            UnavailableTimeWindowGraphQLType(
                start_time=w.start_time, end_time=w.end_time, id=w.id, reason=w.reason
            )
            for w in unavailable_windows
        ]

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def webhook_subscriptions(
        self,
        info: strawberry.Info,
        calendar_id: int | None = None,
        provider: str | None = None,
    ) -> list[CalendarWebhookSubscriptionGraphQLType]:
        """Get webhook subscriptions filtered by user's organization."""
        org = _get_org(info)
        deps = get_query_dependencies()

        # Set organization context on service
        deps.calendar_service.organization = org

        subscriptions = deps.calendar_service.list_webhook_subscriptions()

        if calendar_id is not None:
            subscriptions = subscriptions.filter(calendar__id=calendar_id)
        if provider is not None:
            subscriptions = subscriptions.filter(provider=provider)

        return list(subscriptions)  # type: ignore

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def webhook_events(
        self,
        info: strawberry.Info,
        subscription_id: int | None = None,
        processing_status: str | None = None,
        hours_back: int = 24,
        limit: int = 50,
    ) -> list[CalendarWebhookEventGraphQLType]:
        """Get recent webhook events filtered by user's organization."""
        org = _get_org(info)

        import datetime

        from calendar_integration.models import CalendarWebhookEvent

        start_time = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(hours=hours_back)

        queryset = (
            CalendarWebhookEvent.objects.filter(organization=org, created__gte=start_time)
            .select_related("subscription")
            .order_by("-created")
        )

        if subscription_id is not None:
            queryset = queryset.filter(subscription__id=subscription_id)
        if processing_status is not None:
            queryset = queryset.filter(processing_status=processing_status)

        return list(queryset[:limit])

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def webhook_health(
        self,
        info: strawberry.Info,
    ) -> WebhookSubscriptionStatusGraphQLType:
        """Get webhook system health status for the organization."""
        org = _get_org(info)
        deps = get_query_dependencies()

        # Set organization context on service
        deps.calendar_service.organization = org

        health_data = deps.calendar_service.get_webhook_health_status()

        return WebhookSubscriptionStatusGraphQLType(
            total_subscriptions=health_data["total_subscriptions"],
            active_subscriptions=health_data["active_subscriptions"],
            expired_subscriptions=health_data["expired_subscriptions"],
            expiring_soon_subscriptions=health_data["expiring_soon_subscriptions"],
            recent_events_count=health_data["recent_events_count"],
            failed_events_count=health_data["failed_events_count"],
            success_rate=health_data["success_rate"],
        )
