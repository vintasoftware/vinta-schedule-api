import datetime
from collections.abc import Iterable

from calendar_integration.querysets import (
    CalendarEventQuerySet,
    CalendarQuerySet,
    CalendarSyncQuerySet,
    RecurringQuerySetMixin,
)
from organizations.managers import BaseOrganizationModelManager


class RecurringManagerMixin:
    """
    Mixin for managers that provides recurring functionality.
    Should be used with managers that inherit from BaseOrganizationManager.
    The QuerySet should also inherit from RecurringQuerySetMixin.
    """

    def get_queryset(self) -> RecurringQuerySetMixin:
        raise NotImplementedError("Concrete managers must implement get_queryset")

    def annotate_recurring_occurrences_on_date_range(
        self, start_date: datetime.datetime, end_date: datetime.datetime, max_occurrences=10000
    ):
        """
        Annotate objects with their recurring occurrences in the date range.
        Delegates to the queryset implementation.
        """
        return self.get_queryset().annotate_recurring_occurrences_on_date_range(
            start_date, end_date, max_occurrences
        )

    def filter_master_recurring_objects(self):
        """Filter to get only master recurring objects (not instances)."""
        return self.get_queryset().filter_master_recurring_objects()

    def filter_recurring_instances(self):
        """Filter to get only recurring instances (not masters)."""
        return self.get_queryset().filter_recurring_instances()

    def filter_recurring_objects(self):
        """Filter to get objects that have recurrence rules."""
        return self.get_queryset().filter_recurring_objects()

    def filter_non_recurring_objects(self):
        """Filter to get objects that don't have recurrence rules."""
        return self.get_queryset().filter_non_recurring_objects()


class CalendarManager(BaseOrganizationModelManager):
    """
    Custom manager for Calendar model to handle specific queries.
    """

    def get_queryset(self) -> CalendarQuerySet:
        return CalendarQuerySet(self.model, using=self._db)

    def only_virtual_calendars(self):
        """
        Returns all virtual calendars.
        """
        return self.get_queryset().filter_by_is_virtual()

    def only_resource_calendars(self):
        """
        Returns all resource calendars.
        """
        return self.get_queryset().filter_by_is_resource()

    def only_calendars_by_provider(self, provider):
        """
        Returns calendars filtered by the specified provider.
        """
        return self.get_queryset().only_calendars_by_provider(provider=provider)

    def prefetch_latest_sync(self):
        """
        Prefetches the latest sync record for each calendar.
        """
        return self.get_queryset().prefetch_latest_sync()

    def only_calendars_available_in_ranges(
        self, ranges: Iterable[tuple[datetime.datetime, datetime.datetime]]
    ):
        """
        Returns calendars that are available in the specified date range.
        :param start_datetime: Start of the date range.
        :param end_datetime: End of the date range.
        :return: QuerySet of calendars available in the specified range.
        """
        return self.get_queryset().only_calendars_available_in_ranges(ranges=ranges)


class CalendarEventManager(BaseOrganizationModelManager, RecurringManagerMixin):
    """Custom manager for CalendarEvent model to handle specific queries."""

    def get_queryset(self) -> CalendarEventQuerySet:
        return CalendarEventQuerySet(self.model, using=self._db)


class CalendarSyncManager(BaseOrganizationModelManager):
    """Custom manager for CalendarSync model to handle specific queries."""

    def get_queryset(self) -> CalendarSyncQuerySet:
        return CalendarSyncQuerySet(self.model, using=self._db)

    def get_not_started_calendar_sync(self, calendar_sync_id: int):
        """
        Retrieve a calendar sync that has not started yet.
        :param calendar_sync_id: ID of the calendar sync to retrieve.
        :return: CalendarSync instance if found, otherwise None.
        """
        return self.get_queryset().get_not_started_calendar_sync(calendar_sync_id=calendar_sync_id)
