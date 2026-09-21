import datetime
from typing import TYPE_CHECKING

from django.db.models import QuerySet

import strawberry
from graphql import GraphQLError

from calendar_integration.models import (
    AppointmentType,
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarPool,
)
from organizations.models import Organization
from public_api.constants import MAX_AGGREGATE_RANGE
from public_api.models import SystemUser
from public_api.scoping import (
    scoped_appointment_type_queryset,
    scoped_calendar_ids,
    scoped_calendar_pool_queryset,
)


if TYPE_CHECKING:
    pass


def _validate_datetime_range(
    start_datetime: datetime.datetime, end_datetime: datetime.datetime
) -> None:
    """Validate a client-supplied datetime range for aggregation queries.

    Raises ``GraphQLError`` if the range is backwards or exceeds
    ``MAX_AGGREGATE_RANGE``.
    """
    if end_datetime <= start_datetime:
        raise GraphQLError("Invalid time range.")
    if (end_datetime - start_datetime) > MAX_AGGREGATE_RANGE:
        raise GraphQLError("Requested time range is too large.")


@strawberry.input
class CalendarEventAggregateFilterInput:
    """Filter input for aggregating calendar events.

    Temporal entities require mandatory start_datetime and end_datetime bounds.
    """

    start_datetime: datetime.datetime
    end_datetime: datetime.datetime
    calendar_id: int | None = None

    def apply(
        self, system_user: SystemUser | None, organization: Organization
    ) -> QuerySet[CalendarEvent]:
        """Apply this filter to a calendar event queryset.

        Validates the date range, applies tenant scoping, and optionally filters
        by calendar. Returns a queryset ready for aggregation.
        """
        _validate_datetime_range(self.start_datetime, self.end_datetime)

        # Start from the scoped manager
        qs = CalendarEvent.objects.filter_by_organization(organization.id)

        # Apply temporal bounds
        qs = qs.filter(
            start_time_tz_unaware__gte=self.start_datetime,
            start_time_tz_unaware__lt=self.end_datetime,
        )

        # Apply owner-scope filtering for scoped tokens
        if system_user is not None:
            allowed_ids = scoped_calendar_ids(system_user, organization)
            if allowed_ids is not None:
                if self.calendar_id is not None:
                    # If a specific calendar was requested, verify it is in scope
                    if self.calendar_id not in allowed_ids:
                        return qs.none()
                    qs = qs.filter(calendar_fk=self.calendar_id)
                else:
                    qs = qs.filter(calendar_fk__in=allowed_ids)
            elif self.calendar_id is not None:
                qs = qs.filter(calendar_fk=self.calendar_id)
        elif self.calendar_id is not None:
            qs = qs.filter(calendar_fk=self.calendar_id)

        return qs


@strawberry.input
class AvailableTimeAggregateFilterInput:
    """Filter input for aggregating available times.

    Temporal entities require mandatory start_datetime and end_datetime bounds.
    """

    start_datetime: datetime.datetime
    end_datetime: datetime.datetime
    calendar_id: int | None = None

    def apply(
        self, system_user: SystemUser | None, organization: Organization
    ) -> QuerySet[AvailableTime]:
        """Apply this filter to an available time queryset.

        Validates the date range, applies tenant scoping, and optionally filters
        by calendar. Returns a queryset ready for aggregation.
        """
        _validate_datetime_range(self.start_datetime, self.end_datetime)

        # Start from the scoped manager
        qs = AvailableTime.objects.filter_by_organization(organization.id)

        # Apply temporal bounds
        qs = qs.filter(
            start_time_tz_unaware__gte=self.start_datetime,
            start_time_tz_unaware__lt=self.end_datetime,
        )

        # Apply owner-scope filtering for scoped tokens
        if system_user is not None:
            allowed_ids = scoped_calendar_ids(system_user, organization)
            if allowed_ids is not None:
                if self.calendar_id is not None:
                    # If a specific calendar was requested, verify it is in scope
                    if self.calendar_id not in allowed_ids:
                        return qs.none()
                    qs = qs.filter(calendar_fk=self.calendar_id)
                else:
                    qs = qs.filter(calendar_fk__in=allowed_ids)
            elif self.calendar_id is not None:
                qs = qs.filter(calendar_fk=self.calendar_id)
        elif self.calendar_id is not None:
            qs = qs.filter(calendar_fk=self.calendar_id)

        return qs


@strawberry.input
class BlockedTimeAggregateFilterInput:
    """Filter input for aggregating blocked times.

    Temporal entities require mandatory start_datetime and end_datetime bounds.
    """

    start_datetime: datetime.datetime
    end_datetime: datetime.datetime
    calendar_id: int | None = None

    def apply(
        self, system_user: SystemUser | None, organization: Organization
    ) -> QuerySet[BlockedTime]:
        """Apply this filter to a blocked time queryset.

        Validates the date range, applies tenant scoping, and optionally filters
        by calendar. Returns a queryset ready for aggregation.
        """
        _validate_datetime_range(self.start_datetime, self.end_datetime)

        # Start from the scoped manager
        qs = BlockedTime.objects.filter_by_organization(organization.id)

        # Apply temporal bounds
        qs = qs.filter(
            start_time_tz_unaware__gte=self.start_datetime,
            start_time_tz_unaware__lt=self.end_datetime,
        )

        # Apply owner-scope filtering for scoped tokens
        if system_user is not None:
            allowed_ids = scoped_calendar_ids(system_user, organization)
            if allowed_ids is not None:
                if self.calendar_id is not None:
                    # If a specific calendar was requested, verify it is in scope
                    if self.calendar_id not in allowed_ids:
                        return qs.none()
                    qs = qs.filter(calendar_fk=self.calendar_id)
                else:
                    qs = qs.filter(calendar_fk__in=allowed_ids)
            elif self.calendar_id is not None:
                qs = qs.filter(calendar_fk=self.calendar_id)
        elif self.calendar_id is not None:
            qs = qs.filter(calendar_fk=self.calendar_id)

        return qs


@strawberry.input
class AppointmentTypeAggregateFilterInput:
    """Filter input for aggregating appointment types.

    Non-temporal entity without mandatory date bounds.
    """

    def apply(
        self, system_user: SystemUser | None, organization: Organization
    ) -> QuerySet[AppointmentType]:
        """Apply this filter to an appointment type queryset.

        Applies tenant scoping and role-aware visibility filtering.
        Returns a queryset ready for aggregation.
        """
        # Start from the scoped manager
        qs = AppointmentType.objects.filter_by_organization(organization.id)

        # Apply role-aware visibility scoping for scoped tokens
        qs = scoped_appointment_type_queryset(system_user, organization, qs)

        return qs


@strawberry.input
class CalendarAggregateFilterInput:
    """Filter input for aggregating calendars.

    Non-temporal entity without mandatory date bounds.
    """

    def apply(
        self, system_user: SystemUser | None, organization: Organization
    ) -> QuerySet[Calendar]:
        """Apply this filter to a calendar queryset.

        Applies tenant scoping and owner-scope filtering.
        Returns a queryset ready for aggregation.
        """
        # Start from the scoped manager
        qs = Calendar.objects.filter_by_organization(organization.id)

        # Apply owner-scope filtering for scoped tokens
        if system_user is not None:
            allowed_ids = scoped_calendar_ids(system_user, organization)
            if allowed_ids is not None:
                qs = qs.filter(id__in=allowed_ids)

        return qs


@strawberry.input
class CalendarPoolAggregateFilterInput:
    """Filter input for aggregating calendar pools.

    Non-temporal entity without mandatory date bounds.
    """

    def apply(
        self, system_user: SystemUser | None, organization: Organization
    ) -> QuerySet[CalendarPool]:
        """Apply this filter to a calendar pool queryset.

        Applies tenant scoping and role-aware visibility filtering.
        Returns a queryset ready for aggregation.
        """
        # Start from the scoped manager
        qs = CalendarPool.objects.filter_by_organization(organization.id)

        # Apply role-aware visibility scoping for scoped tokens
        qs = scoped_calendar_pool_queryset(system_user, organization, qs)

        return qs
