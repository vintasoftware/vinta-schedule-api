"""Filter inputs for aggregate GraphQL queries.

Each filter input encapsulates the narrowing predicates for an entity's
aggregate field, enforcing organization scoping and optional date bounds.
A filter is applied to an already-scoped queryset via its ``apply`` method.
"""

import datetime

from django.db.models import QuerySet

import strawberry
from graphql import GraphQLError

from calendar_integration.models import Calendar
from calendar_integration.querysets import AppointmentTypeQuerySet, CalendarPoolQuerySet
from organizations.models import Organization
from public_api.constants import MAX_AGGREGATE_RANGE
from public_api.models import SystemUser
from public_api.scoping import (
    scoped_appointment_type_queryset,
    scoped_calendar_ids,
    scoped_calendar_pool_queryset,
)


def _validate_temporal_range(
    start_datetime: datetime.datetime | None,
    end_datetime: datetime.datetime | None,
) -> None:
    """Validate a mandatory temporal range for aggregate filters.

    Raises ``GraphQLError`` if:
    - Either bound is None (required for temporal entities).
    - The range is backwards (end <= start).
    - The range exceeds MAX_AGGREGATE_RANGE.
    """
    if start_datetime is None or end_datetime is None:
        raise GraphQLError("startDatetime and endDatetime are required.")
    if end_datetime <= start_datetime:
        raise GraphQLError("Invalid time range: endDatetime must be after startDatetime.")
    if (end_datetime - start_datetime) > MAX_AGGREGATE_RANGE:
        raise GraphQLError("Requested time range is too large.")


@strawberry.input
class CalendarEventAggregateFilterInput:
    """Filter for CalendarEvent aggregates.

    Temporal entity: startDatetime and endDatetime are required.
    """

    calendar_id: int | None = strawberry.field(default=None, description="Filter by calendar ID.")
    user_id: int | None = strawberry.field(
        default=None, description="Filter by calendar owner user ID."
    )
    start_datetime: datetime.datetime = strawberry.field(
        description="Required: start of the date range (inclusive)."
    )
    end_datetime: datetime.datetime = strawberry.field(
        description="Required: end of the date range (exclusive)."
    )

    def apply(
        self,
        queryset: QuerySet,
        organization: Organization,
        system_user: SystemUser | None = None,
    ) -> QuerySet:
        """Apply this filter to a scoped CalendarEvent queryset."""
        _validate_temporal_range(self.start_datetime, self.end_datetime)

        if self.calendar_id is not None:
            queryset = queryset.filter(calendar_fk_id=self.calendar_id)

        if self.user_id is not None:
            calendar_ids = (
                Calendar.objects.filter_by_organization(organization.id)
                .filter(ownerships__membership__user_id=self.user_id)
                .values_list("id", flat=True)
            )
            queryset = queryset.filter(calendar_fk_id__in=calendar_ids)

        if system_user is not None:
            scoped_ids = scoped_calendar_ids(system_user, organization)
            if scoped_ids is not None:
                queryset = queryset.filter(calendar_fk_id__in=scoped_ids)

        queryset = queryset.filter(
            start_time__gte=self.start_datetime,
            start_time__lt=self.end_datetime,
        )

        return queryset


@strawberry.input
class AvailableTimeAggregateFilterInput:
    """Filter for AvailableTime aggregates.

    Temporal entity: startDatetime and endDatetime are required.
    """

    calendar_id: int | None = strawberry.field(default=None, description="Filter by calendar ID.")
    user_id: int | None = strawberry.field(
        default=None, description="Filter by calendar owner user ID."
    )
    start_datetime: datetime.datetime = strawberry.field(
        description="Required: start of the date range (inclusive)."
    )
    end_datetime: datetime.datetime = strawberry.field(
        description="Required: end of the date range (exclusive)."
    )

    def apply(
        self,
        queryset: QuerySet,
        organization: Organization,
        system_user: SystemUser | None = None,
    ) -> QuerySet:
        """Apply this filter to a scoped AvailableTime queryset."""
        _validate_temporal_range(self.start_datetime, self.end_datetime)

        if self.calendar_id is not None:
            queryset = queryset.filter(calendar_fk_id=self.calendar_id)

        if self.user_id is not None:
            calendar_ids = (
                Calendar.objects.filter_by_organization(organization.id)
                .filter(ownerships__membership__user_id=self.user_id)
                .values_list("id", flat=True)
            )
            queryset = queryset.filter(calendar_fk_id__in=calendar_ids)

        if system_user is not None:
            scoped_ids = scoped_calendar_ids(system_user, organization)
            if scoped_ids is not None:
                queryset = queryset.filter(calendar_fk_id__in=scoped_ids)

        queryset = queryset.filter(
            start_time__gte=self.start_datetime,
            start_time__lt=self.end_datetime,
        )

        return queryset


@strawberry.input
class BlockedTimeAggregateFilterInput:
    """Filter for BlockedTime aggregates.

    Temporal entity: startDatetime and endDatetime are required.
    """

    calendar_id: int | None = strawberry.field(default=None, description="Filter by calendar ID.")
    user_id: int | None = strawberry.field(
        default=None, description="Filter by calendar owner user ID."
    )
    start_datetime: datetime.datetime = strawberry.field(
        description="Required: start of the date range (inclusive)."
    )
    end_datetime: datetime.datetime = strawberry.field(
        description="Required: end of the date range (exclusive)."
    )

    def apply(
        self,
        queryset: QuerySet,
        organization: Organization,
        system_user: SystemUser | None = None,
    ) -> QuerySet:
        """Apply this filter to a scoped BlockedTime queryset."""
        _validate_temporal_range(self.start_datetime, self.end_datetime)

        if self.calendar_id is not None:
            queryset = queryset.filter(calendar_fk_id=self.calendar_id)

        if self.user_id is not None:
            calendar_ids = (
                Calendar.objects.filter_by_organization(organization.id)
                .filter(ownerships__membership__user_id=self.user_id)
                .values_list("id", flat=True)
            )
            queryset = queryset.filter(calendar_fk_id__in=calendar_ids)

        if system_user is not None:
            scoped_ids = scoped_calendar_ids(system_user, organization)
            if scoped_ids is not None:
                queryset = queryset.filter(calendar_fk_id__in=scoped_ids)

        queryset = queryset.filter(
            start_time__gte=self.start_datetime,
            start_time__lt=self.end_datetime,
        )

        return queryset


@strawberry.input
class AppointmentTypeAggregateFilterInput:
    """Filter for AppointmentType aggregates.

    Non-temporal entity: no date range required.
    """

    name: str | None = strawberry.field(default=None, description="Filter by name (partial match).")

    def apply(
        self,
        queryset: AppointmentTypeQuerySet,
        organization: Organization,
        system_user: SystemUser | None = None,
    ) -> AppointmentTypeQuerySet:
        """Apply this filter to a scoped AppointmentType queryset."""
        if self.name is not None:
            queryset = queryset.filter(name__icontains=self.name)

        if system_user is not None:
            queryset = scoped_appointment_type_queryset(system_user, organization, queryset)

        return queryset


@strawberry.input
class CalendarAggregateFilterInput:
    """Filter for Calendar aggregates.

    Non-temporal entity: no date range required.
    """

    name: str | None = strawberry.field(default=None, description="Filter by name (partial match).")

    def apply(
        self,
        queryset: QuerySet,
        organization: Organization,
        system_user: SystemUser | None = None,
    ) -> QuerySet:
        """Apply this filter to a scoped Calendar queryset."""
        if self.name is not None:
            queryset = queryset.filter(name__icontains=self.name)

        if system_user is not None:
            scoped_ids = scoped_calendar_ids(system_user, organization)
            if scoped_ids is not None:
                queryset = queryset.filter(id__in=scoped_ids)

        return queryset


@strawberry.input
class CalendarPoolAggregateFilterInput:
    """Filter for CalendarPool aggregates.

    Non-temporal entity: no date range required.
    """

    name: str | None = strawberry.field(default=None, description="Filter by name (partial match).")

    def apply(
        self,
        queryset: CalendarPoolQuerySet,
        organization: Organization,
        system_user: SystemUser | None = None,
    ) -> CalendarPoolQuerySet:
        """Apply this filter to a scoped CalendarPool queryset."""
        if self.name is not None:
            queryset = queryset.filter(name__icontains=self.name)

        if system_user is not None:
            queryset = scoped_calendar_pool_queryset(system_user, organization, queryset)

        return queryset
