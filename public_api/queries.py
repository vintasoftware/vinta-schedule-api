import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, cast

import strawberry
import strawberry_django
from dependency_injector.wiring import Provide, inject

from calendar_integration.graphql import (
    AvailableTimeGraphQLType,
    AvailableTimeWindowGraphQLType,
    BlockedTimeGraphQLType,
    CalendarEventGraphQLType,
    CalendarGraphQLType,
    UnavailableTimeWindowGraphQLType,
)
from calendar_integration.models import Calendar
from public_api.permissions import (
    IsAuthenticated,
    OrganizationResourceAccess,
)
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
        raise ValueError(
            f"Missing required dependency {', '.join([str(dep) for dep in required_dependencies if dep is None])}"
        )

    return QueryDependencies(
        calendar_service=cast("CalendarService", calendar_service),
    )


@strawberry.type
class Query:
    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def calendars(self, info: strawberry.Info) -> list[CalendarGraphQLType]:
        """Get calendars filtered by user's organization."""
        organization = info.context.request.public_api_organization
        if not organization:
            raise ValueError("Organization not found in request context")
        return Calendar.objects.filter_by_organization(organization.id)

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def calendar_events(
        self,
        info: strawberry.Info,
        calendar_id: int,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
    ) -> list[CalendarEventGraphQLType]:
        """Get calendar events filtered by user's organization."""
        # Get the user's organization from the GraphQL context
        organization = info.context.request.public_api_organization
        if not organization:
            raise ValueError("Organization not found in request context")

        deps = get_query_dependencies()

        # Initialize the calendar service
        deps.calendar_service.initialize_without_provider(organization)

        calendar = Calendar.objects.filter_by_organization(organization.id).get(id=calendar_id)

        return cast(
            list[CalendarEventGraphQLType],
            deps.calendar_service.get_calendar_events_expanded(
                calendar,
                start_datetime,
                end_datetime,
            ),
        )

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def blocked_times(
        self,
        info: strawberry.Info,
        calendar_id: int,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
    ) -> list[BlockedTimeGraphQLType]:
        """Get blocked times filtered by user's organization."""
        # Get the user's organization from the GraphQL context
        organization = info.context.request.public_api_organization
        if not organization:
            raise ValueError("Organization not found in request context")

        deps = get_query_dependencies()

        # Initialize the calendar service
        deps.calendar_service.initialize_without_provider(organization)

        calendar = Calendar.objects.filter_by_organization(organization.id).get(id=calendar_id)

        return cast(
            list[BlockedTimeGraphQLType],
            deps.calendar_service.get_blocked_times_expanded(
                calendar,
                start_datetime,
                end_datetime,
            ),
        )

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def available_times(
        self,
        info: strawberry.Info,
        calendar_id: int,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
    ) -> list[AvailableTimeGraphQLType]:
        """Get available times filtered by user's organization."""
        # Get the user's organization from the GraphQL context
        organization = info.context.request.public_api_organization
        if not organization:
            raise ValueError("Organization not found in request context")

        deps = get_query_dependencies()

        # Initialize the calendar service
        deps.calendar_service.initialize_without_provider(organization)

        calendar = Calendar.objects.filter_by_organization(organization.id).get(id=calendar_id)

        return cast(
            list[AvailableTimeGraphQLType],
            deps.calendar_service.get_available_times_expanded(
                calendar,
                start_datetime,
                end_datetime,
            ),
        )

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def users(self, info: strawberry.Info) -> list[UserGraphQLType]:
        """Get users filtered by user's organization."""
        organization = info.context.request.public_api_organization
        if not organization:
            raise ValueError("Organization not found in request context")
        # Return a concrete list and cast to the declared GraphQL return type so
        # mypy recognizes the return value matches the annotation.
        return cast(
            list[UserGraphQLType],
            list(User.objects.filter(organization_membership__organization=organization)),
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
        # Get the user's organization from the GraphQL context
        organization = info.context.request.public_api_organization
        if not organization:
            raise ValueError("Organization not found in request context")

        deps = get_query_dependencies()

        # Initialize the calendar service
        deps.calendar_service.initialize_without_provider(organization)

        # Get the calendar
        calendar = Calendar.objects.filter_by_organization(organization.id).get(id=calendar_id)

        # Get the availability windows
        availability_windows = deps.calendar_service.get_availability_windows_in_range(
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
        # Get the user's organization from the GraphQL context
        organization = info.context.request.public_api_organization
        if not organization:
            raise ValueError("Organization not found in request context")

        deps = get_query_dependencies()

        # Initialize the calendar service
        deps.calendar_service.initialize_without_provider(organization)

        # Get the calendar
        calendar = Calendar.objects.filter_by_organization(organization.id).get(id=calendar_id)

        unavailable_windows = deps.calendar_service.get_unavailable_time_windows_in_range(
            calendar=calendar, start_datetime=start_datetime, end_datetime=end_datetime
        )

        return [
            UnavailableTimeWindowGraphQLType(
                start_time=w.start_time, end_time=w.end_time, id=w.id, reason=w.reason
            )
            for w in unavailable_windows
        ]
