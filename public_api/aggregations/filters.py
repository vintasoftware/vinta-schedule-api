"""Filter inputs for aggregate queries over calendar and scheduling entities.

Each filter input narrows its model's scoped queryset before aggregation. Temporal
entities carry non-null startDatetime / endDatetime fields with a maximum-span
validation, while non-temporal entities omit them. All filters reuse existing
scoping helpers from public_api/scoping.py rather than re-deriving owner scope.
"""

import datetime
from typing import TYPE_CHECKING

from django.db.models import QuerySet

import strawberry

from calendar_integration.models import (
    AppointmentType,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarPool,
)
from calendar_integration.querysets import AppointmentTypeQuerySet, CalendarPoolQuerySet
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


class AggregateFilterValidationError(Exception):
    """Raised when an aggregate filter input violates validation rules."""

    pass


@strawberry.input
class CalendarEventAggregateFilterInput:
    """Filter input for aggregating over CalendarEvent.

    Temporal bounds are mandatory (enforced at the type level by non-null fields).
    The date range must not exceed MAX_AGGREGATE_RANGE (366 days).
    """

    start_datetime: datetime.datetime
    end_datetime: datetime.datetime
    calendar_id: int | None = None

    def apply(
        self,
        system_user: SystemUser | None,
        organization: Organization,
    ) -> QuerySet:
        """Apply filter to CalendarEvent queryset and return the narrowed result.

        Raises:
            AggregateFilterValidationError: If the date range exceeds MAX_AGGREGATE_RANGE.

        Returns:
            A scoped, filtered CalendarEvent queryset.
        """
        span = self.end_datetime - self.start_datetime
        if span > MAX_AGGREGATE_RANGE:
            raise AggregateFilterValidationError(
                f"Date range exceeds maximum of {MAX_AGGREGATE_RANGE.days} days."
            )

        qs = CalendarEvent.objects.filter_by_organization(organization.id)

        if self.calendar_id is not None:
            qs = qs.filter(calendar_fk_id=self.calendar_id)

        qs = qs.filter(
            start_time__gte=self.start_datetime,
            end_time__lte=self.end_datetime,
        )

        return qs


@strawberry.input
class BlockedTimeAggregateFilterInput:
    """Filter input for aggregating over BlockedTime.

    Temporal bounds are mandatory (enforced at the type level by non-null fields).
    The date range must not exceed MAX_AGGREGATE_RANGE (366 days).
    """

    start_datetime: datetime.datetime
    end_datetime: datetime.datetime
    calendar_id: int | None = None

    def apply(
        self,
        system_user: SystemUser | None,
        organization: Organization,
    ) -> QuerySet:
        """Apply filter to BlockedTime queryset and return the narrowed result.

        Raises:
            AggregateFilterValidationError: If the date range exceeds MAX_AGGREGATE_RANGE.

        Returns:
            A scoped, filtered BlockedTime queryset.
        """
        span = self.end_datetime - self.start_datetime
        if span > MAX_AGGREGATE_RANGE:
            raise AggregateFilterValidationError(
                f"Date range exceeds maximum of {MAX_AGGREGATE_RANGE.days} days."
            )

        qs = BlockedTime.objects.filter_by_organization(organization.id)

        if self.calendar_id is not None:
            qs = qs.filter(calendar_fk_id=self.calendar_id)

        qs = qs.filter(
            start_time__gte=self.start_datetime,
            end_time__lte=self.end_datetime,
        )

        return qs


@strawberry.input
class AvailableTimeAggregateFilterInput:
    """Filter input for aggregating over AvailableTime.

    Temporal bounds are mandatory (enforced at the type level by non-null fields).
    The date range must not exceed MAX_AGGREGATE_RANGE (366 days).
    """

    start_datetime: datetime.datetime
    end_datetime: datetime.datetime
    calendar_id: int | None = None

    def apply(
        self,
        system_user: SystemUser | None,
        organization: Organization,
    ) -> QuerySet:
        """Apply filter to AvailableTime queryset and return the narrowed result.

        Raises:
            AggregateFilterValidationError: If the date range exceeds MAX_AGGREGATE_RANGE.

        Returns:
            A scoped, filtered AvailableTime queryset.
        """
        span = self.end_datetime - self.start_datetime
        if span > MAX_AGGREGATE_RANGE:
            raise AggregateFilterValidationError(
                f"Date range exceeds maximum of {MAX_AGGREGATE_RANGE.days} days."
            )

        from calendar_integration.models import AvailableTime

        qs = AvailableTime.objects.filter_by_organization(organization.id)

        if self.calendar_id is not None:
            qs = qs.filter(calendar_fk_id=self.calendar_id)

        qs = qs.filter(
            start_time__gte=self.start_datetime,
            end_time__lte=self.end_datetime,
        )

        return qs


@strawberry.input
class AppointmentTypeAggregateFilterInput:
    """Filter input for aggregating over AppointmentType.

    Non-temporal entity with optional filtering by calendar membership.
    """

    calendar_id: int | None = None

    def apply(
        self,
        system_user: SystemUser | None,
        organization: Organization,
    ) -> AppointmentTypeQuerySet:
        """Apply filter to AppointmentType queryset and return the narrowed result.

        Applies role-aware visibility scoping via scoped_appointment_type_queryset.

        Returns:
            A scoped, filtered AppointmentType queryset.
        """
        qs = AppointmentType.objects.filter_by_organization(organization.id)

        if self.calendar_id is not None:
            qs = qs.filter(slot_memberships__calendar_fk_id=self.calendar_id).distinct()

        qs = scoped_appointment_type_queryset(system_user, organization, qs)
        return qs


@strawberry.input
class CalendarAggregateFilterInput:
    """Filter input for aggregating over Calendar.

    Non-temporal entity with owner-scope filtering for scoped tokens.
    """

    def apply(
        self,
        system_user: SystemUser | None,
        organization: Organization,
    ) -> QuerySet:
        """Apply filter to Calendar queryset and return the narrowed result.

        For scoped tokens, filters to only calendars in the token's allowed set.

        Returns:
            A scoped, filtered Calendar queryset.
        """
        qs = Calendar.objects.filter_by_organization(organization.id)

        if system_user is not None:
            allowed_ids = scoped_calendar_ids(system_user, organization)
            if allowed_ids is not None:
                qs = qs.filter(id__in=allowed_ids)

        return qs


@strawberry.input
class CalendarPoolAggregateFilterInput:
    """Filter input for aggregating over CalendarPool.

    Non-temporal entity with role-aware membership visibility scoping.
    """

    def apply(
        self,
        system_user: SystemUser | None,
        organization: Organization,
    ) -> CalendarPoolQuerySet:
        """Apply filter to CalendarPool queryset and return the narrowed result.

        Applies role-aware visibility scoping via scoped_calendar_pool_queryset.

        Returns:
            A scoped, filtered CalendarPool queryset.
        """
        qs = CalendarPool.objects.filter_by_organization(organization.id)
        qs = scoped_calendar_pool_queryset(system_user, organization, qs)
        return qs
