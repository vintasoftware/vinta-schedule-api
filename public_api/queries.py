import datetime
from typing import cast

import strawberry
import strawberry_django

from calendar_integration.graphql import (
    AvailableTimeGraphQLType,
    AvailableTimeWindowGraphQLType,
    BlockedTimeGraphQLType,
    CalendarEventGraphQLType,
    CalendarGraphQLType,
)
from calendar_integration.models import AvailableTime, BlockedTime, Calendar
from public_api.permissions import (
    IsAuthenticated,
    OrganizationResourceAccess,
)
from users.graphql import UserGraphQLType
from users.models import User


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
        from di_core.containers import container

        if not container:
            raise ValueError("DI container is not yet initialized")

        # Get the calendar service from the DI container
        calendar_service = container.calendar_service()

        # Get the user's organization from the GraphQL context
        organization = info.context.request.public_api_organization
        if not organization:
            raise ValueError("Organization not found in request context")

        # Initialize the calendar service
        calendar_service.initialize_without_provider(organization)

        calendar = Calendar.objects.filter_by_organization(organization.id).get(id=calendar_id)

        return cast(
            list[CalendarEventGraphQLType],
            calendar_service.get_calendar_events_expanded(
                calendar,
                start_datetime,
                end_datetime,
            ),
        )

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def blocked_times(self, info: strawberry.Info) -> list[BlockedTimeGraphQLType]:
        """Get blocked times filtered by user's organization."""
        organization = info.context.request.public_api_organization
        if not organization:
            raise ValueError("Organization not found in request context")
        return BlockedTime.objects.filter_by_organization(organization.id)

    @strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def available_times(self, info: strawberry.Info) -> list[AvailableTimeGraphQLType]:
        """Get available times filtered by user's organization."""
        organization = info.context.request.public_api_organization
        if not organization:
            raise ValueError("Organization not found in request context")
        return AvailableTime.objects.filter_by_organization(organization.id)

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
        from di_core.containers import container

        if not container:
            raise ValueError("DI container is not yet initialized")

        # Get the calendar service from the DI container
        calendar_service = container.calendar_service()

        # Get the user's organization from the GraphQL context
        organization = info.context.request.public_api_organization
        if not organization:
            raise ValueError("Organization not found in request context")

        # Initialize the calendar service
        calendar_service.initialize_without_provider(organization)

        # Get the calendar
        calendar = Calendar.objects.filter_by_organization(organization.id).get(id=calendar_id)

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
