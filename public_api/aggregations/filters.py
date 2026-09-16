import datetime
from typing import Any

import strawberry
from graphql import GraphQLError

from public_api.constants import MAX_AGGREGATE_RANGE
from public_api.scoping import (
    scoped_appointment_type_queryset,
    scoped_calendar_ids,
    scoped_calendar_pool_queryset,
)


def _apply_temporal_range_filter(
    base_qs: Any,
    organization_id: int,
    calendar_id: int | None,
    start_datetime: datetime.datetime,
    end_datetime: datetime.datetime,
    system_user: Any = None,
) -> Any:
    """Apply temporal range and calendar scope filtering.

    Shared helper for CalendarEvent, AvailableTime, and BlockedTime filters.
    """
    if end_datetime <= start_datetime:
        raise GraphQLError("Invalid time range: end_datetime must be after start_datetime.")
    if (end_datetime - start_datetime) > MAX_AGGREGATE_RANGE:
        raise GraphQLError("Requested time range is too large.")

    qs = base_qs.filter_by_organization(organization_id).filter(
        start_time__lt=end_datetime,
        end_time__gt=start_datetime,
    )

    if calendar_id is not None:
        qs = qs.filter(calendar_fk_id=calendar_id)

    if system_user is not None:
        from organizations.models import Organization

        org = Organization.objects.get(id=organization_id)
        allowed_ids = scoped_calendar_ids(system_user, org)
        if allowed_ids is not None:
            qs = qs.filter(calendar_fk_id__in=allowed_ids)

    return qs


@strawberry.input
class CalendarEventAggregateFilterInput:
    """Filter input for aggregating calendar events.

    Temporal entities require non-null startDatetime and endDatetime.
    """

    calendar_id: int | None = None
    start_datetime: datetime.datetime
    end_datetime: datetime.datetime

    def apply(self, base_qs: Any, organization_id: int, system_user: Any = None) -> Any:
        """Return a narrowed queryset filtered by this input.

        Validates the date range and applies calendar ownership scope.
        """
        return _apply_temporal_range_filter(
            base_qs,
            organization_id,
            self.calendar_id,
            self.start_datetime,
            self.end_datetime,
            system_user,
        )


@strawberry.input
class AvailableTimeAggregateFilterInput:
    """Filter input for aggregating available times.

    Temporal entities require non-null startDatetime and endDatetime.
    """

    calendar_id: int | None = None
    start_datetime: datetime.datetime
    end_datetime: datetime.datetime

    def apply(self, base_qs: Any, organization_id: int, system_user: Any = None) -> Any:
        """Return a narrowed queryset filtered by this input.

        Validates the date range and applies calendar ownership scope.
        """
        return _apply_temporal_range_filter(
            base_qs,
            organization_id,
            self.calendar_id,
            self.start_datetime,
            self.end_datetime,
            system_user,
        )


@strawberry.input
class BlockedTimeAggregateFilterInput:
    """Filter input for aggregating blocked times.

    Temporal entities require non-null startDatetime and endDatetime.
    """

    calendar_id: int | None = None
    start_datetime: datetime.datetime
    end_datetime: datetime.datetime

    def apply(self, base_qs: Any, organization_id: int, system_user: Any = None) -> Any:
        """Return a narrowed queryset filtered by this input.

        Validates the date range and applies calendar ownership scope.
        """
        return _apply_temporal_range_filter(
            base_qs,
            organization_id,
            self.calendar_id,
            self.start_datetime,
            self.end_datetime,
            system_user,
        )


@strawberry.input
class AppointmentTypeAggregateFilterInput:
    """Filter input for aggregating appointment types."""

    name: str | None = None

    def apply(self, base_qs: Any, organization_id: int, system_user: Any = None) -> Any:
        """Return a narrowed queryset filtered by this input.

        Applies role-aware scoping to appointment types.
        """
        from organizations.models import Organization

        qs = base_qs.filter_by_organization(organization_id)

        if self.name is not None:
            qs = qs.filter(name=self.name)

        if system_user is not None:
            org = Organization.objects.get(id=organization_id)
            qs = scoped_appointment_type_queryset(system_user, org, qs)

        return qs


@strawberry.input
class CalendarAggregateFilterInput:
    """Filter input for aggregating calendars."""

    calendar_id: int | None = None

    def apply(self, base_qs: Any, organization_id: int, system_user: Any = None) -> Any:
        """Return a narrowed queryset filtered by this input.

        Applies calendar ownership scope.
        """
        qs = base_qs.filter_by_organization(organization_id)

        if self.calendar_id is not None:
            qs = qs.filter(id=self.calendar_id)

        if system_user is not None:
            from organizations.models import Organization

            org = Organization.objects.get(id=organization_id)
            allowed_ids = scoped_calendar_ids(system_user, org)
            if allowed_ids is not None:
                qs = qs.filter(id__in=allowed_ids)

        return qs


@strawberry.input
class CalendarPoolAggregateFilterInput:
    """Filter input for aggregating calendar pools."""

    name: str | None = None

    def apply(self, base_qs: Any, organization_id: int, system_user: Any = None) -> Any:
        """Return a narrowed queryset filtered by this input.

        Applies role-aware scoping to calendar pools.
        """
        from organizations.models import Organization

        qs = base_qs.filter_by_organization(organization_id)

        if self.name is not None:
            qs = qs.filter(name=self.name)

        if system_user is not None:
            org = Organization.objects.get(id=organization_id)
            qs = scoped_calendar_pool_queryset(system_user, org, qs)

        return qs
