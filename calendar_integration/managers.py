import datetime
from collections.abc import Iterable

from calendar_integration.querysets import (
    CalendarQuerySet,
    CalendarSyncQuerySet,
)
from organizations.managers import BaseOrganizationModelManager


class CalendarManager(BaseOrganizationModelManager):
    """
    Custom manager for Calendar model to handle specific queries.
    """

    def get_queryset(self):
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


class CalendarSyncManager(BaseOrganizationModelManager):
    """Custom manager for CalendarSync model to handle specific queries."""

    def get_queryset(self):
        return CalendarSyncQuerySet(self.model, using=self._db)

    def get_not_started_calendar_sync(self, calendar_sync_id: int):
        """
        Retrieve a calendar sync that has not started yet.
        :param calendar_sync_id: ID of the calendar sync to retrieve.
        :return: CalendarSync instance if found, otherwise None.
        """
        return self.get_queryset().get_not_started_calendar_sync(calendar_sync_id=calendar_sync_id)
