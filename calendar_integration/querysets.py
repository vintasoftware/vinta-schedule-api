import datetime
from collections.abc import Iterable
from typing import TYPE_CHECKING

from django.db.models import OuterRef, Prefetch, Q, Subquery

from calendar_integration.constants import CalendarSyncStatus, CalendarType
from calendar_integration.database_functions import (
    GetAvailableTimeOccurrencesJSON,
    GetAvailableTimeOccurrencesWithBulkModificationsJSON,
    GetBlockedTimeOccurrencesJSON,
    GetBlockedTimeOccurrencesWithBulkModificationsJSON,
    GetEventOccurrencesJSON,
    GetEventOccurrencesWithBulkModificationsJSON,
)
from organizations.querysets import BaseOrganizationModelQuerySet


if TYPE_CHECKING:
    from calendar_integration.models import CalendarSync as CalendarSyncType


class RecurringQuerySetMixin:
    """
    Mixin for querysets that provides recurring functionality.
    Should be used with querysets that inherit from BaseOrganizationModelQuerySet.
    """

    def annotate_recurring_occurrences_on_date_range(
        self, start_date: datetime.datetime, end_date: datetime.datetime, max_occurrences=10000
    ):
        """
        Annotate objects with their recurring occurrences in the date range.
        This method should be overridden by concrete querysets to use their specific database function.
        """
        raise NotImplementedError(
            "Concrete querysets must implement annotate_recurring_occurrences_on_date_range"
        )

    def filter_master_recurring_objects(self):
        """Filter to get only master recurring objects (not instances)."""
        return self.filter(parent_recurring_object__isnull=True, recurrence_rule__isnull=False)

    def filter_recurring_instances(self):
        """Filter to get only recurring instances (not masters)."""
        return self.filter(parent_recurring_object__isnull=False)

    def filter_recurring_objects(self):
        """Filter to get objects that have recurrence rules."""
        return self.filter(recurrence_rule__isnull=False)

    def filter_non_recurring_objects(self):
        """Filter to get objects that don't have recurrence rules."""
        return self.filter(recurrence_rule__isnull=True)

    def annotate_recurring_occurrences_with_bulk_modifications_on_date_range(
        self, start_date: datetime.datetime, end_date: datetime.datetime, max_occurrences=10000
    ):
        """
        Annotate objects with their recurring occurrences including bulk modifications in the date range.
        This method should be overridden by concrete querysets to use their specific bulk modification database function.
        """
        raise NotImplementedError(
            "Concrete querysets must implement annotate_recurring_occurrences_with_bulk_modifications_on_date_range"
        )

    def get_occurrences_in_range_with_bulk_modifications(
        self,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        include_continuations: bool = True,
        max_occurrences: int = 10000,
    ):
        """
        Get occurrences considering bulk modifications across all objects in the queryset.

        This method efficiently aggregates occurrences from:
        1. All objects in the queryset (potentially truncated by bulk modifications)
        2. Their continuation objects created by bulk modifications

        Args:
            start_date: Start of the date range
            end_date: End of the date range
            include_continuations: Whether to include occurrences from continuation objects
            max_occurrences: Maximum number of occurrences to return per object

        Returns:
            QuerySet of all occurrence instances, ordered by start time
        """
        all_objects = []

        # Get all recurring objects in the queryset
        recurring_objects = self.filter_recurring_objects()

        # For each recurring object, get its occurrences
        for obj in recurring_objects:
            occurrences = obj.get_occurrences_in_range_with_bulk_modifications(
                start_date=start_date,
                end_date=end_date,
                include_continuations=include_continuations,
                max_occurrences=max_occurrences,
            )
            all_objects.extend(occurrences)

        # Sort all occurrences by start time
        all_objects.sort(key=lambda occurrence: occurrence.start_time)

        # Convert to a queryset if possible, otherwise return as list
        if all_objects:
            # Get all IDs and return as a queryset ordered by start_time
            object_ids = [obj.id for obj in all_objects if hasattr(obj, "id")]
            if object_ids:
                # Create a case-when ordering to preserve the sorted order
                from django.db.models import Case, IntegerField, When

                preserved_order = Case(
                    *[When(pk=pk, then=pos) for pos, pk in enumerate(object_ids)],
                    output_field=IntegerField(),
                )
                return self.filter(id__in=object_ids).order_by(preserved_order)  # type: ignore

        # Return empty queryset if no occurrences found
        return self.none()  # type: ignore


class CalendarQuerySet(BaseOrganizationModelQuerySet):
    """
    Custom QuerySet for Calendar model to handle specific queries.
    """

    def filter_by_is_virtual(self, is_virtual=True):
        """
        Returns virtual calendars when is_virtual=True, or non-virtual calendars when is_virtual=False.
        """
        if is_virtual:
            return self.filter(calendar_type=CalendarType.VIRTUAL)
        else:
            return self.exclude(calendar_type=CalendarType.VIRTUAL)

    def filter_by_is_resource(self, is_resource=True):
        """
        Returns resource calendars when is_resource=True, or non-resource calendars when is_resource=False.
        """
        if is_resource:
            return self.filter(calendar_type=CalendarType.RESOURCE)
        else:
            return self.exclude(calendar_type=CalendarType.RESOURCE)

    def only_calendars_by_provider(self, provider):
        """
        Returns calendars filtered by the specified provider.
        """
        return self.filter(provider=provider)

    def only_resource_calendars(self):
        """
        Returns only resource calendars.
        """
        return self.filter_by_is_resource(True)

    def only_virtual_calendars(self):
        """
        Returns only virtual calendars.
        """
        return self.filter_by_is_virtual(True)

    def prefetch_latest_sync(self):
        """
        Prefetches the latest sync record for each calendar.
        """
        from calendar_integration.models import CalendarSync

        return self.prefetch_related(
            Prefetch(
                "syncs",
                CalendarSync.objects.filter(
                    should_update_events=True,
                    id__in=Subquery(
                        CalendarSync.objects.filter(
                            should_update_events=True,
                            calendar_fk_id=OuterRef("calendar_fk_id"),
                            organization_id=OuterRef("organization_id"),
                        )
                        .order_by("-start_datetime")
                        .values("id")[:1]
                    ),
                ),
                to_attr="_latest_sync",
            )
        )

    def update(self, **kwargs):
        # find model fields that are CalendarOrganizationForeignKey
        foreign_key_fields_in_kwargs = [
            field.name
            for field in self._meta.get_fields()
            if (
                self.model.is_field_organization_foreign_key(field)
                and (field.name in kwargs.keys() or f"{field.name}_id" in kwargs.keys())
            )
        ]

        for field_name in foreign_key_fields_in_kwargs:
            if field_name in kwargs.keys() and not kwargs.get(f"{field_name}_fk", None):
                kwargs[f"{field_name}_fk"] = kwargs.pop(field_name)
                continue
            if f"{field_name}_id" in kwargs.keys() and not kwargs.get(f"{field_name}_fk_id", None):
                kwargs[f"{field_name}_fk_id"] = kwargs.pop(f"{field_name}_id")
                continue
        return super().update(**kwargs)

    def only_calendars_available_in_ranges(
        self, ranges: Iterable[tuple[datetime.datetime, datetime.datetime]]
    ):
        """
        Returns calendars that have available time windows in all specified ranges.
        """
        from calendar_integration.models import AvailableTime, BlockedTime, CalendarEvent

        if not ranges:
            return self.none()

        queries = []
        for start_datetime, end_datetime in ranges:
            # For managed calendars: must have available time exactly matching the range
            managed_query = Q(
                manage_available_windows=True,
                id__in=Subquery(
                    AvailableTime.objects.filter(
                        calendar_fk_id=OuterRef("id"),
                        start_time__lte=start_datetime,
                        end_time__gte=end_datetime,
                    )
                    .values("calendar_fk_id")
                    .distinct()
                ),
            )

            # For unmanaged calendars: must NOT have conflicting events or blocked times
            unmanaged_query = Q(
                manage_available_windows=False,
            ) & ~Q(
                Q(
                    id__in=Subquery(
                        CalendarEvent.objects.annotate_recurring_occurrences_on_date_range(
                            start_datetime, end_datetime
                        )
                        .filter(
                            Q(start_time__range=(start_datetime, end_datetime))
                            | Q(end_time__range=(start_datetime, end_datetime))
                            | Q(start_time__lte=start_datetime, end_time__gte=end_datetime)
                            | Q(recurring_occurrences__len__gt=0),
                            calendar_fk_id=OuterRef("id"),
                        )
                        .values("calendar_fk_id")
                        .distinct()
                    )
                )
                | Q(
                    id__in=Subquery(
                        BlockedTime.objects.filter(
                            Q(start_time__range=(start_datetime, end_datetime))
                            | Q(end_time__range=(start_datetime, end_datetime))
                            | Q(start_time__lte=start_datetime, end_time__gte=end_datetime),
                            calendar_fk_id=OuterRef("id"),
                        )
                        .values("calendar_fk_id")
                        .distinct()
                    )
                )
            )

            # Combine both conditions
            range_query = managed_query | unmanaged_query
            queries.append(range_query)

        # All ranges must be satisfied (AND operation)
        combined_query = queries[0]
        for query in queries[1:]:
            combined_query &= query

        return self.filter(combined_query)


class CalendarEventQuerySet(BaseOrganizationModelQuerySet, RecurringQuerySetMixin):
    """
    Custom QuerySet for CalendarEvent model to handle specific queries.
    """

    def annotate_recurring_occurrences_on_date_range(
        self, start: datetime.datetime, end: datetime.datetime, max_occurrences=10000
    ):
        """
        Annotated an Array aggregating all occurrences of a recurring event within the specified date range.
        The occurrences are calculated dynamically based on the master event's recurrence rule.
        Each occurrence will be a JSON containing the start_datetime and the end_datetime in UTC.
        """
        return self.annotate(
            recurring_occurrences=GetEventOccurrencesJSON("id", start, end, max_occurrences)
        )

    def annotate_recurring_occurrences_with_bulk_modifications_on_date_range(
        self, start: datetime.datetime, end: datetime.datetime, max_occurrences=10000
    ):
        """
        Annotate an Array aggregating all occurrences of a recurring event within the specified date range,
        including occurrences from continuation events created by bulk modifications.

        The occurrences are calculated dynamically and include:
        1. Occurrences from the original event (potentially truncated)
        2. Occurrences from any continuation events created by bulk modifications

        Each occurrence will be a JSON containing the start_datetime, end_datetime in UTC,
        and source_event_id to identify which event generated the occurrence.
        """
        return self.annotate(
            recurring_occurrences=GetEventOccurrencesWithBulkModificationsJSON(
                "id", start, end, max_occurrences
            )
        )


class CalendarSyncQuerySet(BaseOrganizationModelQuerySet):
    """
    Custom QuerySet for CalendarSync model to handle specific queries.
    """

    def get_not_started_calendar_sync(self, calendar_sync_id: int) -> "CalendarSyncType | None":
        """
        Retrieve a calendar sync that has not started yet.
        :param calendar_sync_id: ID of the calendar sync to retrieve.
        :return: CalendarSync instance if found, otherwise None.
        """
        return self.filter(id=calendar_sync_id, status=CalendarSyncStatus.NOT_STARTED).first()


class BlockedTimeQuerySet(BaseOrganizationModelQuerySet, RecurringQuerySetMixin):
    """
    Custom QuerySet for BlockedTime model to handle specific queries.
    """

    def annotate_recurring_occurrences_on_date_range(
        self, start: datetime.datetime, end: datetime.datetime, max_occurrences=10000
    ):
        """
        Annotated an Array aggregating all occurrences of a recurring blocked time within the specified date range.
        The occurrences are calculated dynamically based on the master blocked time's recurrence rule.
        Each occurrence will be a JSON containing the start_datetime and the end_datetime in UTC.
        """
        return self.annotate(
            recurring_occurrences=GetBlockedTimeOccurrencesJSON("id", start, end, max_occurrences)
        )

    def annotate_recurring_occurrences_with_bulk_modifications_on_date_range(
        self, start: datetime.datetime, end: datetime.datetime, max_occurrences=10000
    ):
        """
        Annotate an Array aggregating all occurrences of a recurring blocked time within the specified date range,
        including occurrences from continuation blocked times created by bulk modifications.

        Each occurrence will be a JSON containing the start_datetime, end_datetime in UTC,
        and source_blocked_time_id to identify which blocked time generated the occurrence.
        """
        return self.annotate(
            recurring_occurrences_with_bulk_modifications=GetBlockedTimeOccurrencesWithBulkModificationsJSON(
                "id", start, end, max_occurrences
            )
        )


class AvailableTimeQuerySet(BaseOrganizationModelQuerySet, RecurringQuerySetMixin):
    """
    Custom QuerySet for AvailableTime model to handle specific queries.
    """

    def annotate_recurring_occurrences_on_date_range(
        self, start: datetime.datetime, end: datetime.datetime, max_occurrences=10000
    ):
        """
        Annotated an Array aggregating all occurrences of a recurring available time within the specified date range.
        The occurrences are calculated dynamically based on the master available time's recurrence rule.
        Each occurrence will be a JSON containing the start_datetime and the end_datetime in UTC.
        """
        return self.annotate(
            recurring_occurrences=GetAvailableTimeOccurrencesJSON("id", start, end, max_occurrences)
        )

    def annotate_recurring_occurrences_with_bulk_modifications_on_date_range(
        self, start: datetime.datetime, end: datetime.datetime, max_occurrences=10000
    ):
        """
        Annotate an Array aggregating all occurrences of a recurring available time within the specified date range,
        including occurrences from continuation available times created by bulk modifications.

        Each occurrence will be a JSON containing the start_datetime, end_datetime in UTC,
        and source_available_time_id to identify which available time generated the occurrence.
        """
        return self.annotate(
            recurring_occurrences_with_bulk_modifications=GetAvailableTimeOccurrencesWithBulkModificationsJSON(
                "id", start, end, max_occurrences
            )
        )
