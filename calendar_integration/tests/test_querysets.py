import datetime
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

import pytest

from calendar_integration.constants import (
    CalendarProvider,
    CalendarSyncStatus,
    CalendarType,
    RecurrenceFrequency,
)
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarSync,
    RecurrenceRule,
)
from organizations.models import Organization


def _dt(year, month, day, hour=9, minute=0):
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.UTC)


@pytest.mark.django_db
class TestCalendarQuerySet(TestCase):
    """Test cases for CalendarQuerySet methods."""

    def setUp(self):
        """Set up test data."""
        # Create a base calendar organization for testing
        self.organization = Organization.objects.create(
            name="Test Organization",
            should_sync_rooms=True,
        )

        # Create calendars of different types
        self.personal_calendar = Calendar.objects.create(
            name="Personal Calendar",
            description="A personal calendar",
            email="personal@example.com",
            external_id="personal_123",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            organization=self.organization,
        )

        self.resource_calendar = Calendar.objects.create(
            name="Conference Room A",
            description="Meeting room calendar",
            email="room-a@example.com",
            external_id="room_a_123",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.RESOURCE,
            capacity=10,
            organization=self.organization,
        )

        self.virtual_calendar = Calendar.objects.create(
            name="Virtual Meeting Room",
            description="Online meeting calendar",
            email="virtual@example.com",
            external_id="virtual_123",
            provider=CalendarProvider.MICROSOFT,
            calendar_type=CalendarType.VIRTUAL,
            organization=self.organization,
        )

        # Create additional calendars for provider filtering
        self.apple_calendar = Calendar.objects.create(
            name="Apple Calendar",
            description="Apple calendar",
            email="apple@example.com",
            external_id="apple_123",
            provider=CalendarProvider.APPLE,
            calendar_type=CalendarType.PERSONAL,
            organization=self.organization,
        )

    def test_only_virtual_calendars_default(self):
        """Test filtering virtual calendars with default parameter."""
        virtual_calendars = (
            Calendar.objects.get_queryset()
            .filter_by_organization(organization_id=self.organization.id)
            .filter_by_is_virtual()
        )

        self.assertEqual(virtual_calendars.count(), 1)
        self.assertEqual(virtual_calendars.first(), self.virtual_calendar)

    def test_only_virtual_calendars_true(self):
        """Test filtering virtual calendars with is_virtual=True."""
        virtual_calendars = (
            Calendar.objects.get_queryset()
            .filter_by_organization(organization_id=self.organization.id)
            .filter_by_is_virtual(True)
        )

        self.assertEqual(virtual_calendars.count(), 1)
        self.assertEqual(virtual_calendars.first(), self.virtual_calendar)

    def test_only_virtual_calendars_false(self):
        """Test filtering non-virtual calendars with is_virtual=False."""
        non_virtual_calendars = (
            Calendar.objects.get_queryset()
            .filter_by_organization(organization_id=self.organization.id)
            .filter_by_is_virtual(False)
        )

        self.assertEqual(non_virtual_calendars.count(), 3)
        calendar_ids = list(non_virtual_calendars.values_list("id", flat=True))
        self.assertIn(self.personal_calendar.id, calendar_ids)
        self.assertIn(self.resource_calendar.id, calendar_ids)
        self.assertIn(self.apple_calendar.id, calendar_ids)
        self.assertNotIn(self.virtual_calendar.id, calendar_ids)

    def test_only_resource_calendars_default(self):
        """Test filtering resource calendars with default parameter."""
        resource_calendars = (
            Calendar.objects.get_queryset()
            .filter_by_organization(organization_id=self.organization.id)
            .filter_by_is_resource()
        )

        self.assertEqual(resource_calendars.count(), 1)
        self.assertEqual(resource_calendars.first(), self.resource_calendar)

    def test_only_resource_calendars_true(self):
        """Test filtering resource calendars with is_resource=True."""
        resource_calendars = (
            Calendar.objects.get_queryset()
            .filter_by_organization(organization_id=self.organization.id)
            .filter_by_is_resource(True)
        )

        self.assertEqual(resource_calendars.count(), 1)
        self.assertEqual(resource_calendars.first(), self.resource_calendar)

    def test_only_resource_calendars_false(self):
        """Test filtering non-resource calendars with is_resource=False."""
        non_resource_calendars = (
            Calendar.objects.get_queryset()
            .filter_by_organization(organization_id=self.organization.id)
            .filter_by_is_resource(False)
        )

        self.assertEqual(non_resource_calendars.count(), 3)
        calendar_ids = list(non_resource_calendars.values_list("id", flat=True))
        self.assertIn(self.personal_calendar.id, calendar_ids)
        self.assertIn(self.virtual_calendar.id, calendar_ids)
        self.assertIn(self.apple_calendar.id, calendar_ids)
        self.assertNotIn(self.resource_calendar.id, calendar_ids)

    def test_only_calendars_by_provider_google(self):
        """Test filtering calendars by Google provider."""
        google_calendars = Calendar.objects.filter_by_organization(
            organization_id=self.organization.id
        ).only_calendars_by_provider(CalendarProvider.GOOGLE)

        self.assertEqual(google_calendars.count(), 2)
        calendar_ids = list(google_calendars.values_list("id", flat=True))
        self.assertIn(self.personal_calendar.id, calendar_ids)
        self.assertIn(self.resource_calendar.id, calendar_ids)

    def test_only_calendars_by_provider_microsoft(self):
        """Test filtering calendars by Microsoft provider."""
        microsoft_calendars = Calendar.objects.filter_by_organization(
            organization_id=self.organization.id
        ).only_calendars_by_provider(CalendarProvider.MICROSOFT)

        self.assertEqual(microsoft_calendars.count(), 1)
        self.assertEqual(microsoft_calendars.first(), self.virtual_calendar)

    def test_only_calendars_by_provider_apple(self):
        """Test filtering calendars by Apple provider."""
        apple_calendars = Calendar.objects.filter_by_organization(
            organization_id=self.organization.id
        ).only_calendars_by_provider(CalendarProvider.APPLE)

        self.assertEqual(apple_calendars.count(), 1)
        self.assertEqual(apple_calendars.first(), self.apple_calendar)

    def test_only_calendars_by_provider_nonexistent(self):
        """Test filtering calendars by non-existent provider."""
        other_calendars = Calendar.objects.filter_by_organization(
            organization_id=self.organization.id
        ).only_calendars_by_provider(CalendarProvider.ICS)

        self.assertEqual(other_calendars.count(), 0)

    def test_prefetch_latest_sync(self):
        """Test prefetching latest sync records."""
        now = timezone.now()

        # Create sync records for personal calendar
        CalendarSync.objects.create(
            calendar=self.personal_calendar,
            start_datetime=now - timedelta(days=2),
            end_datetime=now - timedelta(days=1),
            should_update_events=True,
            status=CalendarSyncStatus.SUCCESS,
            organization=self.organization,
        )

        sync2 = CalendarSync.objects.create(
            calendar=self.personal_calendar,
            start_datetime=now - timedelta(days=1),
            end_datetime=now,
            should_update_events=True,
            status=CalendarSyncStatus.SUCCESS,
            organization=self.organization,
        )

        # Create a sync that should be ignored (should_update_events=False)
        CalendarSync.objects.create(
            calendar=self.personal_calendar,
            start_datetime=now,
            end_datetime=now + timedelta(hours=1),
            should_update_events=False,
            status=CalendarSyncStatus.SUCCESS,
            organization=self.organization,
        )

        # Create sync for resource calendar
        sync4 = CalendarSync.objects.create(
            calendar=self.resource_calendar,
            start_datetime=now - timedelta(hours=1),
            end_datetime=now,
            should_update_events=True,
            status=CalendarSyncStatus.IN_PROGRESS,
            organization=self.organization,
        )

        # Test the prefetch
        calendars = Calendar.objects.filter_by_organization(
            organization_id=self.organization
        ).prefetch_latest_sync()

        # Find our calendars
        personal_cal = None
        resource_cal = None
        virtual_cal = None

        for cal in calendars:
            if cal.id == self.personal_calendar.id:
                personal_cal = cal
            elif cal.id == self.resource_calendar.id:
                resource_cal = cal
            elif cal.id == self.virtual_calendar.id:
                virtual_cal = cal

        # Personal calendar should have the latest sync (sync2)
        self.assertIsNotNone(personal_cal)
        latest_sync = personal_cal.latest_sync
        self.assertIsNotNone(latest_sync)
        self.assertEqual(latest_sync.id, sync2.id)

        # Resource calendar should have its sync
        self.assertIsNotNone(resource_cal)
        latest_sync = resource_cal.latest_sync
        self.assertIsNotNone(latest_sync)
        self.assertEqual(latest_sync.id, sync4.id)

        # Virtual calendar should have no sync
        self.assertIsNotNone(virtual_cal)
        latest_sync = virtual_cal.latest_sync
        self.assertIsNone(latest_sync)

    def test_chaining_queryset_methods(self):
        """Test chaining multiple queryset methods."""
        # Create additional Google resource calendar
        google_resource = Calendar.objects.create(
            name="Google Conference Room",
            description="Google meeting room",
            email="google-room@example.com",
            external_id="google_room_123",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.RESOURCE,
            capacity=20,
            organization=self.organization,
        )

        # Chain methods: get Google resource calendars
        google_resource_calendars = (
            Calendar.objects.only_resource_calendars()
            .filter_by_organization(organization_id=self.organization.id)
            .only_calendars_by_provider(CalendarProvider.GOOGLE)
        )

        self.assertEqual(google_resource_calendars.count(), 2)
        calendar_ids = list(google_resource_calendars.values_list("id", flat=True))
        self.assertIn(self.resource_calendar.id, calendar_ids)
        self.assertIn(google_resource.id, calendar_ids)


@pytest.mark.django_db
class TestCalendarSyncQuerySet(TestCase):
    """Test cases for CalendarSyncQuerySet methods."""

    def setUp(self):
        """Set up test data."""
        self.organization = Organization.objects.create(
            name="Test Organization",
            should_sync_rooms=True,
        )

        self.calendar = Calendar.objects.create(
            name="Test Calendar",
            description="Test calendar for sync tests",
            email="test@example.com",
            external_id="test_123",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            organization=self.organization,
        )

        now = timezone.now()

        # Create sync records with different statuses
        self.not_started_sync = CalendarSync.objects.create(
            calendar=self.calendar,
            start_datetime=now,
            end_datetime=now + timedelta(hours=1),
            should_update_events=True,
            status=CalendarSyncStatus.NOT_STARTED,
            organization=self.organization,
        )

        self.in_progress_sync = CalendarSync.objects.create(
            calendar=self.calendar,
            start_datetime=now - timedelta(hours=1),
            end_datetime=now,
            should_update_events=True,
            status=CalendarSyncStatus.IN_PROGRESS,
            organization=self.organization,
        )

        self.success_sync = CalendarSync.objects.create(
            calendar=self.calendar,
            start_datetime=now - timedelta(hours=2),
            end_datetime=now - timedelta(hours=1),
            should_update_events=True,
            status=CalendarSyncStatus.SUCCESS,
            organization=self.organization,
        )

        self.failed_sync = CalendarSync.objects.create(
            calendar=self.calendar,
            start_datetime=now - timedelta(hours=3),
            end_datetime=now - timedelta(hours=2),
            should_update_events=True,
            status=CalendarSyncStatus.FAILED,
            organization=self.organization,
        )

    def test_get_not_started_calendar_sync_exists(self):
        """Test getting a not started calendar sync that exists."""
        result = CalendarSync.objects.filter_by_organization(
            organization_id=self.organization.id
        ).get_not_started_calendar_sync(self.not_started_sync.id)

        self.assertIsNotNone(result)
        self.assertEqual(result.id, self.not_started_sync.id)
        self.assertEqual(result.status, CalendarSyncStatus.NOT_STARTED)

    def test_get_not_started_calendar_sync_wrong_status(self):
        """Test getting a calendar sync with wrong status returns None."""
        result = CalendarSync.objects.filter_by_organization(
            organization_id=self.organization.id
        ).get_not_started_calendar_sync(self.in_progress_sync.id)

        self.assertIsNone(result)

    def test_get_not_started_calendar_sync_nonexistent_id(self):
        """Test getting a calendar sync with non-existent ID returns None."""
        result = CalendarSync.objects.filter_by_organization(
            organization_id=self.organization.id
        ).get_not_started_calendar_sync(99999)

        self.assertIsNone(result)

    def test_get_not_started_calendar_sync_with_success_status(self):
        """Test that success status sync is not returned."""
        result = CalendarSync.objects.filter_by_organization(
            organization_id=self.organization.id
        ).get_not_started_calendar_sync(self.success_sync.id)

        self.assertIsNone(result)

    def test_get_not_started_calendar_sync_with_failed_status(self):
        """Test that failed status sync is not returned."""
        result = CalendarSync.objects.filter_by_organization(
            organization_id=self.organization.id
        ).get_not_started_calendar_sync(self.failed_sync.id)

        self.assertIsNone(result)

    def test_get_not_started_calendar_sync_return_type(self):
        """Test that the method returns the correct type."""
        result = CalendarSync.objects.filter_by_organization(
            organization_id=self.organization.id
        ).get_not_started_calendar_sync(self.not_started_sync.id)

        self.assertIsInstance(result, CalendarSync)
        self.assertEqual(result.calendar, self.calendar)


@pytest.mark.django_db
class TestQuerySetIntegration(TestCase):
    """Integration tests for both querysets working together."""

    def setUp(self):
        """Set up test data for integration tests."""
        now = timezone.now()

        self.organization = Organization.objects.create(
            name="Test Organization",
            should_sync_rooms=True,
        )

        # Create calendars
        self.google_personal = Calendar.objects.create(
            name="Google Personal",
            email="google-personal@example.com",
            external_id="google_personal_123",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            organization=self.organization,
        )

        self.microsoft_resource = Calendar.objects.create(
            name="Microsoft Resource",
            email="ms-resource@example.com",
            external_id="ms_resource_123",
            provider=CalendarProvider.MICROSOFT,
            calendar_type=CalendarType.RESOURCE,
            capacity=15,
            organization=self.organization,
        )

        # Create sync records
        self.google_sync = CalendarSync.objects.create(
            calendar=self.google_personal,
            start_datetime=now,
            end_datetime=now + timedelta(hours=1),
            should_update_events=True,
            status=CalendarSyncStatus.NOT_STARTED,
            organization=self.organization,
        )

        self.microsoft_sync = CalendarSync.objects.create(
            calendar=self.microsoft_resource,
            start_datetime=now,
            end_datetime=now + timedelta(hours=1),
            should_update_events=True,
            status=CalendarSyncStatus.IN_PROGRESS,
            organization=self.organization,
        )

    def test_calendar_with_not_started_sync(self):
        """Test finding calendars with not started syncs."""
        # Get Google calendars
        google_calendars = Calendar.objects.filter_by_organization(
            organization_id=self.organization.id
        ).only_calendars_by_provider(CalendarProvider.GOOGLE)
        self.assertEqual(google_calendars.count(), 1)

        # Check if this calendar has a not started sync
        calendar = google_calendars.first()
        not_started_sync = CalendarSync.objects.filter_by_organization(
            organization_id=self.organization.id
        ).get_not_started_calendar_sync(self.google_sync.id)

        self.assertIsNotNone(not_started_sync)
        self.assertEqual(not_started_sync.calendar, calendar)

    def test_resource_calendar_with_in_progress_sync(self):
        """Test finding resource calendars with in progress syncs."""
        # Get resource calendars
        resource_calendars = Calendar.objects.filter_by_organization(
            organization_id=self.organization.id
        ).only_resource_calendars()
        self.assertEqual(resource_calendars.count(), 1)

        calendar = resource_calendars.first()
        self.assertEqual(calendar, self.microsoft_resource)

        # This sync should not be returned as it's not NOT_STARTED
        not_started_sync = CalendarSync.objects.filter_by_organization(
            organization_id=self.organization.id
        ).get_not_started_calendar_sync(self.microsoft_sync.id)
        self.assertIsNone(not_started_sync)

    def test_prefetch_with_filtered_calendars(self):
        """Test prefetching syncs with filtered calendars."""
        now = timezone.now()

        # Add another sync to Google calendar that was created BEFORE the NOT_STARTED sync
        CalendarSync.objects.create(
            calendar=self.google_personal,
            start_datetime=now - timedelta(days=2),
            end_datetime=now - timedelta(days=1),
            should_update_events=True,
            status=CalendarSyncStatus.SUCCESS,
            organization=self.organization,
        )

        # Get Google calendars with prefetched syncs
        google_calendars = (
            Calendar.objects.filter_by_organization(organization_id=self.organization.id)
            .only_calendars_by_provider(CalendarProvider.GOOGLE)
            .prefetch_latest_sync()
        )

        self.assertEqual(google_calendars.count(), 1)
        calendar = google_calendars.first()

        # Should have latest sync
        latest_sync = calendar.latest_sync
        self.assertIsNotNone(latest_sync)
        # The NOT_STARTED sync should be the latest (most recent created)
        self.assertEqual(latest_sync.status, CalendarSyncStatus.NOT_STARTED)


@pytest.mark.django_db
class TestCalendarAvailabilityQuerySet(TestCase):
    def setUp(self):
        """Helper function to set up test data for calendar availability tests."""
        self.now = timezone.now()

        # Create a base calendar organization for testing
        self.organization = Organization.objects.create(
            name="Test Organization",
            should_sync_rooms=True,
        )

        # Create calendars with different availability management settings
        self.managed_calendar = Calendar.objects.create(
            name="Managed Calendar",
            description="Calendar with managed available windows",
            email="managed@example.com",
            external_id="managed_123",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=True,
            organization=self.organization,
        )

        self.unmanaged_calendar = Calendar.objects.create(
            name="Unmanaged Calendar",
            description="Calendar without managed available windows",
            email="unmanaged@example.com",
            external_id="unmanaged_123",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=False,
            organization=self.organization,
        )

        self.resource_calendar = Calendar.objects.create(
            name="Resource Calendar",
            description="Resource calendar",
            email="resource@example.com",
            external_id="resource_123",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.RESOURCE,
            manage_available_windows=True,
            capacity=10,
            organization=self.organization,
        )

        # Define test time ranges
        self.range1_start = self.now + timedelta(hours=1)
        self.range1_end = self.now + timedelta(hours=2)
        self.range2_start = self.now + timedelta(hours=3)
        self.range2_end = self.now + timedelta(hours=4)
        self.range3_start = self.now + timedelta(hours=5)
        self.range3_end = self.now + timedelta(hours=6)

        # Create available times for managed calendar
        AvailableTime.objects.create(
            calendar=self.managed_calendar,
            start_time=self.range1_start,
            end_time=self.range1_end,
            organization=self.organization,
        )
        AvailableTime.objects.create(
            calendar=self.managed_calendar,
            start_time=self.range2_start,
            end_time=self.range2_end,
            organization=self.organization,
        )

        # Create available times for resource calendar
        AvailableTime.objects.create(
            calendar=self.resource_calendar,
            start_time=self.range1_start,
            end_time=self.range1_end,
            organization=self.organization,
        )

        # Create events for unmanaged calendar (to test the other branch)
        CalendarEvent.objects.create(
            calendar=self.unmanaged_calendar,
            title="Test Event 1",
            description="Test event in range 1",
            start_time=self.range1_start,
            end_time=self.range1_end,
            external_id="event1_123",
            organization=self.organization,
        )

        # Create blocked times for unmanaged calendar
        BlockedTime.objects.create(
            calendar=self.unmanaged_calendar,
            start_time=self.range2_start,
            end_time=self.range2_end,
            reason="Blocked for testing",
            organization=self.organization,
        )

    def test_single_range_managed_calendar_with_available_time(self):
        """Test filtering with single range for managed calendar with available time."""
        organization = self.organization

        ranges = [(self.range1_start, self.range1_end)]

        available_calendars = Calendar.objects.filter_by_organization(
            organization_id=organization.id
        ).only_calendars_available_in_ranges(ranges)

        # Should include both managed and resource calendars that have available times
        assert available_calendars.count() == 2
        calendar_ids = list(available_calendars.values_list("id", flat=True))
        assert self.managed_calendar.id in calendar_ids
        assert self.resource_calendar.id in calendar_ids

    def test_single_range_unmanaged_calendar_with_events(self):
        """Test filtering with single range for unmanaged calendar with events."""
        organization = self.organization

        ranges = [(self.range1_start, self.range1_end)]

        available_calendars = Calendar.objects.filter_by_organization(
            organization_id=organization.id
        ).only_calendars_available_in_ranges(ranges)

        # Should NOT include unmanaged calendar because it has conflicting events in the range
        calendar_ids = list(available_calendars.values_list("id", flat=True))
        assert self.unmanaged_calendar.id not in calendar_ids

    def test_single_range_unmanaged_calendar_with_blocked_times(self):
        """Test filtering with single range for unmanaged calendar with blocked times."""
        organization = self.organization

        ranges = [(self.range2_start, self.range2_end)]

        available_calendars = Calendar.objects.filter_by_organization(
            organization_id=organization.id
        ).only_calendars_available_in_ranges(ranges)

        # Should NOT include unmanaged calendar because it has conflicting blocked times in the range
        calendar_ids = list(available_calendars.values_list("id", flat=True))
        assert self.unmanaged_calendar.id not in calendar_ids

    def test_multiple_ranges_all_available(self):
        """Test filtering with multiple ranges where calendar has availability in all."""
        organization = self.organization

        ranges = [
            (self.range1_start, self.range1_end),
            (self.range2_start, self.range2_end),
        ]

        available_calendars = Calendar.objects.filter_by_organization(
            organization_id=organization.id
        ).only_calendars_available_in_ranges(ranges)

        # Only managed calendar should be returned as it has available times in both ranges
        assert available_calendars.count() == 1
        assert available_calendars.first() == self.managed_calendar

    def test_multiple_ranges_partial_availability(self):
        """Test filtering with multiple ranges where calendar has partial availability."""
        organization = self.organization

        ranges = [
            (self.range1_start, self.range1_end),
            (self.range3_start, self.range3_end),  # No availability in range 3
        ]

        available_calendars = Calendar.objects.filter_by_organization(
            organization_id=organization.id
        ).only_calendars_available_in_ranges(ranges)

        # No calendars should be returned as none have availability in all ranges
        assert available_calendars.count() == 0

    def test_empty_ranges(self):
        """Test filtering with empty ranges list."""
        ranges = []
        organization = Organization.objects.create(
            name="Test Organization",
            should_sync_rooms=True,
        )

        available_calendars = Calendar.objects.filter_by_organization(
            organization_id=organization.id
        ).only_calendars_available_in_ranges(ranges)

        # Should return no calendars when no ranges are specified
        assert available_calendars.count() == 0

    def test_no_matching_calendars(self):
        """Test filtering with ranges that have no matching calendars."""
        organization = self.organization

        # Use a future range that has no available times, events, or blocked times
        future_start = timezone.now() + timedelta(days=30)
        future_end = timezone.now() + timedelta(days=30, hours=1)
        ranges = [(future_start, future_end)]

        available_calendars = Calendar.objects.filter_by_organization(
            organization_id=organization.id
        ).only_calendars_available_in_ranges(ranges)

        # Should include unmanaged calendars that have no conflicts in the range
        # (but not managed calendars that have no available times)
        assert available_calendars.count() >= 1

        # Verify it's the unmanaged calendar(s) being returned
        calendar_ids = list(available_calendars.values_list("id", flat=True))

        # Should include unmanaged calendar (no conflicts in future range)
        assert self.unmanaged_calendar.id in calendar_ids

        # Should not include managed calendars (no available times in future range)
        assert self.managed_calendar.id not in calendar_ids
        assert self.resource_calendar.id not in calendar_ids

    def test_managed_calendar_without_available_times(self):
        """Test managed calendar that has no available times in the specified range."""
        organization = self.organization

        # Create a managed calendar without any available times
        empty_managed_calendar = Calendar.objects.create(
            name="Empty Managed Calendar",
            email="empty@example.com",
            external_id="empty_123",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=True,
            organization=organization,
        )

        ranges = [(self.range1_start, self.range1_end)]
        available_calendars = Calendar.objects.filter_by_organization(
            organization_id=organization.id
        ).only_calendars_available_in_ranges(ranges)

        # Empty managed calendar should not be included
        calendar_ids = list(available_calendars.values_list("id", flat=True))
        assert empty_managed_calendar.id not in calendar_ids

    def test_unmanaged_calendar_without_events_or_blocked_times(self):
        """Test unmanaged calendar that has no events or blocked times in the specified range."""
        organization = self.organization

        # Create an unmanaged calendar without any events or blocked times
        empty_unmanaged_calendar = Calendar.objects.create(
            name="Empty Unmanaged Calendar",
            email="empty-unmanaged@example.com",
            external_id="empty_unmanaged_123",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=False,
            organization=organization,
        )

        ranges = [(self.range1_start, self.range1_end)]
        available_calendars = Calendar.objects.filter_by_organization(
            organization_id=organization.id
        ).only_calendars_available_in_ranges(ranges)

        # Empty unmanaged calendar SHOULD be included (no conflicts = available)
        calendar_ids = list(available_calendars.values_list("id", flat=True))
        assert empty_unmanaged_calendar.id in calendar_ids

    def test_mixed_manage_available_windows_settings(self):
        """Test filtering with calendars that have different manage_available_windows settings."""
        organization = self.organization

        # Create additional test data
        AvailableTime.objects.create(
            calendar=self.resource_calendar,
            start_time=self.range2_start,
            end_time=self.range2_end,
            organization=organization,
        )

        CalendarEvent.objects.create(
            calendar=self.unmanaged_calendar,
            title="Test Event 2",
            description="Test event in range 2",
            start_time=self.range2_start,
            end_time=self.range2_end,
            external_id="test_event_2_ext_id",
            organization=organization,
        )

        ranges = [
            (self.range1_start, self.range1_end),
            (self.range2_start, self.range2_end),
        ]

        available_calendars = Calendar.objects.filter_by_organization(
            organization_id=organization.id
        ).only_calendars_available_in_ranges(ranges)

        # Should include calendars that satisfy conditions for both ranges
        assert available_calendars.count() >= 1
        calendar_ids = list(available_calendars.values_list("id", flat=True))

        # Managed calendar should be included (has available times in both ranges)
        assert self.managed_calendar.id in calendar_ids

        # Resource calendar should be included (has available times in both ranges)
        assert self.resource_calendar.id in calendar_ids

        # Unmanaged calendar should NOT be included (has conflicting events/blocked times in both ranges)
        assert self.unmanaged_calendar.id not in calendar_ids

    def test_chaining_with_other_queryset_methods(self):
        """Test chaining only_calendars_available_in_ranges with other queryset methods."""
        organization = self.organization

        ranges = [(self.range1_start, self.range1_end)]

        # Chain with provider filter
        google_available_calendars = (
            Calendar.objects.filter_by_organization(organization_id=organization.id)
            .only_calendars_by_provider(CalendarProvider.GOOGLE)
            .only_calendars_available_in_ranges(ranges)
        )

        # Should only include Google calendars that are available in the range
        for calendar in google_available_calendars:
            assert calendar.provider == CalendarProvider.GOOGLE

    def test_complex_time_overlaps(self):
        """Test with complex time overlaps and edge cases."""
        organization = self.organization

        # Create overlapping available times
        overlap_start = self.range1_start + timedelta(minutes=30)
        overlap_end = self.range1_end + timedelta(minutes=30)

        AvailableTime.objects.create(
            calendar=self.managed_calendar,
            start_time=overlap_start,
            end_time=overlap_end,
            organization=organization,
        )

        ranges = [(overlap_start, overlap_end)]
        available_calendars = Calendar.objects.filter_by_organization(
            organization_id=organization.id
        ).only_calendars_available_in_ranges(ranges)

        calendar_ids = list(available_calendars.values_list("id", flat=True))
        assert self.managed_calendar.id in calendar_ids

    def test_no_available_calendars_all_have_conflicts(self):
        """Test case where all calendars have conflicts in the specified range."""
        organization = self.organization

        # Add conflicting events to all calendars for a specific range
        conflict_start = timezone.now() + timedelta(days=1)
        conflict_end = timezone.now() + timedelta(days=1, hours=1)

        # Add event to unmanaged calendar (creates conflict)
        CalendarEvent.objects.create(
            calendar=self.unmanaged_calendar,
            title="Conflict Event",
            description="Event that creates conflict",
            start_time=conflict_start,
            end_time=conflict_end,
            external_id="conflict_event_ext_id",
            organization=organization,
        )

        ranges = [(conflict_start, conflict_end)]
        available_calendars = Calendar.objects.filter_by_organization(
            organization_id=organization.id
        ).only_calendars_available_in_ranges(ranges)

        # Should return 0 calendars since:
        # - Managed calendars have no available times in this range
        # - Unmanaged calendar has conflicting event in this range
        assert available_calendars.count() == 0


@pytest.mark.django_db
class TestBlockedTimeQuerySet(TestCase):
    """Test cases for BlockedTimeQuerySet recurring functionality."""

    def setUp(self):
        """Set up test data for BlockedTime recurring tests."""
        self.organization = Organization.objects.create(
            name="Test Organization",
            should_sync_rooms=True,
        )

        self.calendar = Calendar.objects.create(
            name="Test Calendar",
            description="Test calendar for blocked times",
            email="test@example.com",
            external_id="test_123",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            organization=self.organization,
        )

        self.now = timezone.now().replace(second=0, microsecond=0)

        # Create a daily recurrence rule
        self.daily_rule = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.DAILY,
            interval=1,
            count=5,
            organization=self.organization,
        )

        # Create a weekly recurrence rule
        self.weekly_rule = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.WEEKLY,
            interval=1,
            by_weekday="MO,WE,FR",
            count=3,
            organization=self.organization,
        )

        # Create recurring blocked time (daily)
        self.daily_blocked_time = BlockedTime.objects.create(
            calendar=self.calendar,
            start_time=self.now + timedelta(days=1),
            end_time=self.now + timedelta(days=1, hours=2),
            reason="Daily maintenance",
            external_id="daily_blocked_time",
            recurrence_rule=self.daily_rule,
            organization=self.organization,
        )

        # Create recurring blocked time (weekly)
        self.weekly_blocked_time = BlockedTime.objects.create(
            calendar=self.calendar,
            start_time=self.now + timedelta(days=7),
            end_time=self.now + timedelta(days=7, hours=1),
            reason="Weekly meeting",
            external_id="weekly_blocked_time",
            recurrence_rule=self.weekly_rule,
            organization=self.organization,
        )

        # Create non-recurring blocked time
        self.single_blocked_time = BlockedTime.objects.create(
            calendar=self.calendar,
            start_time=self.now + timedelta(days=2),
            end_time=self.now + timedelta(days=2, hours=1),
            reason="One-time block",
            external_id="single_blocked_time",
            organization=self.organization,
        )

    def test_filter_master_recurring_objects(self):
        """Test filtering master recurring blocked times."""
        masters = BlockedTime.objects.filter_by_organization(
            organization_id=self.organization.id
        ).filter_master_recurring_objects()

        assert masters.count() == 2
        master_ids = list(masters.values_list("id", flat=True))
        assert self.daily_blocked_time.id in master_ids
        assert self.weekly_blocked_time.id in master_ids
        assert self.single_blocked_time.id not in master_ids

    def test_filter_recurring_instances(self):
        """Test filtering recurring instance blocked times."""
        # Create a recurring instance
        instance = BlockedTime.objects.create(
            calendar=self.calendar,
            start_time=self.now + timedelta(days=3),
            end_time=self.now + timedelta(days=3, hours=2),
            reason="Modified occurrence",
            parent_recurring_object=self.daily_blocked_time,
            recurrence_id=self.now + timedelta(days=3),
            is_recurring_exception=True,
            organization=self.organization,
        )

        instances = BlockedTime.objects.filter_by_organization(
            organization_id=self.organization.id
        ).filter_recurring_instances()

        assert instances.count() == 1
        assert instances.first().id == instance.id

    def test_filter_non_recurring_objects(self):
        """Test filtering non-recurring blocked times."""
        non_recurring = BlockedTime.objects.filter_by_organization(
            organization_id=self.organization.id
        ).filter_non_recurring_objects()

        assert non_recurring.count() == 1
        assert non_recurring.first().id == self.single_blocked_time.id

    def test_annotate_recurring_occurrences_on_date_range(self):
        """Test annotating blocked times with their recurring occurrences."""
        start_date = self.now
        end_date = self.now + timedelta(days=10)

        blocked_times_with_occurrences = (
            BlockedTime.objects.filter_by_organization(organization_id=self.organization.id)
            .filter_master_recurring_objects()
            .annotate_recurring_occurrences_on_date_range(start_date, end_date)
        )

        # Check that annotation exists
        for blocked_time in blocked_times_with_occurrences:
            assert hasattr(blocked_time, "recurring_occurrences")

            if blocked_time.id == self.daily_blocked_time.id:
                # Daily blocked time should have occurrences
                assert blocked_time.recurring_occurrences is not None
                # Should be a list/array of JSON strings
                assert len(blocked_time.recurring_occurrences) > 0
            elif blocked_time.id == self.weekly_blocked_time.id:
                # Weekly blocked time might have occurrences depending on the date range
                assert blocked_time.recurring_occurrences is not None

    def test_recurring_objects_properties(self):
        """Test recurring properties on BlockedTime objects."""
        # Test master recurring object
        assert self.daily_blocked_time.is_recurring is True
        assert self.daily_blocked_time.is_recurring_instance is False

        # Test non-recurring object
        assert self.single_blocked_time.is_recurring is False
        assert self.single_blocked_time.is_recurring_instance is False

        # Create and test recurring instance
        instance = BlockedTime.objects.create(
            calendar=self.calendar,
            start_time=self.now + timedelta(days=3),
            end_time=self.now + timedelta(days=3, hours=2),
            reason="Modified occurrence",
            parent_recurring_object=self.daily_blocked_time,
            recurrence_id=self.now + timedelta(days=3),
            is_recurring_exception=True,
            organization=self.organization,
        )

        assert instance.is_recurring is False
        assert instance.is_recurring_instance is True

    def test_manager_delegation_methods(self):
        """Test that manager methods properly delegate to queryset."""
        # Test manager methods delegate correctly
        masters = BlockedTime.objects.filter_master_recurring_objects()
        assert masters.count() >= 2

        instances = BlockedTime.objects.filter_recurring_instances()
        # Should be 0 initially since we haven't created any instances yet
        assert instances.count() == 0

        non_recurring = BlockedTime.objects.filter_non_recurring_objects()
        assert non_recurring.count() >= 1


@pytest.mark.django_db
class TestAvailableTimeQuerySet(TestCase):
    """Test cases for AvailableTimeQuerySet recurring functionality."""

    def setUp(self):
        """Set up test data for AvailableTime recurring tests."""
        self.organization = Organization.objects.create(
            name="Test Organization",
            should_sync_rooms=True,
        )

        self.calendar = Calendar.objects.create(
            name="Test Calendar",
            description="Test calendar for available times",
            email="test@example.com",
            external_id="test_123",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            organization=self.organization,
        )

        self.now = timezone.now().replace(second=0, microsecond=0)

        # Create a daily recurrence rule for work hours
        self.daily_rule = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.DAILY,
            interval=1,
            by_weekday="MO,TU,WE,TH,FR",  # Weekdays only
            count=10,
            organization=self.organization,
        )

        # Create a weekly recurrence rule
        self.weekly_rule = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.WEEKLY,
            interval=2,  # Every 2 weeks
            count=4,
            organization=self.organization,
        )

        # Create recurring available time (daily work hours)
        self.daily_available_time = AvailableTime.objects.create(
            calendar=self.calendar,
            start_time=self.now.replace(hour=9, minute=0) + timedelta(days=1),  # 9 AM
            end_time=self.now.replace(hour=17, minute=0) + timedelta(days=1),  # 5 PM
            recurrence_rule=self.daily_rule,
            organization=self.organization,
        )

        # Create recurring available time (bi-weekly)
        self.weekly_available_time = AvailableTime.objects.create(
            calendar=self.calendar,
            start_time=self.now.replace(hour=10, minute=0) + timedelta(days=7),  # 10 AM
            end_time=self.now.replace(hour=12, minute=0) + timedelta(days=7),  # 12 PM
            recurrence_rule=self.weekly_rule,
            organization=self.organization,
        )

        # Create non-recurring available time
        self.single_available_time = AvailableTime.objects.create(
            calendar=self.calendar,
            start_time=self.now + timedelta(days=2, hours=14),
            end_time=self.now + timedelta(days=2, hours=16),
            organization=self.organization,
        )

    def test_filter_master_recurring_objects(self):
        """Test filtering master recurring available times."""
        masters = AvailableTime.objects.filter_by_organization(
            organization_id=self.organization.id
        ).filter_master_recurring_objects()

        assert masters.count() == 2
        master_ids = list(masters.values_list("id", flat=True))
        assert self.daily_available_time.id in master_ids
        assert self.weekly_available_time.id in master_ids
        assert self.single_available_time.id not in master_ids

    def test_filter_recurring_instances(self):
        """Test filtering recurring instance available times."""
        # Create a recurring instance (exception)
        instance = AvailableTime.objects.create(
            calendar=self.calendar,
            start_time=self.now + timedelta(days=3, hours=10),
            end_time=self.now + timedelta(days=3, hours=18),  # Extended hours
            parent_recurring_object=self.daily_available_time,
            recurrence_id=self.now + timedelta(days=3, hours=9),
            is_recurring_exception=True,
            organization=self.organization,
        )

        instances = AvailableTime.objects.filter_by_organization(
            organization_id=self.organization.id
        ).filter_recurring_instances()

        assert instances.count() == 1
        assert instances.first().id == instance.id

    def test_filter_non_recurring_objects(self):
        """Test filtering non-recurring available times."""
        non_recurring = AvailableTime.objects.filter_by_organization(
            organization_id=self.organization.id
        ).filter_non_recurring_objects()

        assert non_recurring.count() == 1
        assert non_recurring.first().id == self.single_available_time.id

    def test_annotate_recurring_occurrences_on_date_range(self):
        """Test annotating available times with their recurring occurrences."""
        start_date = self.now
        end_date = self.now + timedelta(days=14)

        available_times_with_occurrences = (
            AvailableTime.objects.filter_by_organization(organization_id=self.organization.id)
            .filter_master_recurring_objects()
            .annotate_recurring_occurrences_on_date_range(start_date, end_date)
        )

        # Check that annotation exists
        for available_time in available_times_with_occurrences:
            assert hasattr(available_time, "recurring_occurrences")

            if available_time.id == self.daily_available_time.id:
                # Daily available time should have multiple occurrences
                assert available_time.recurring_occurrences is not None
                assert len(available_time.recurring_occurrences) > 0
            elif available_time.id == self.weekly_available_time.id:
                # Weekly available time should have fewer occurrences
                assert available_time.recurring_occurrences is not None

    def test_recurring_objects_properties(self):
        """Test recurring properties on AvailableTime objects."""
        # Test master recurring object
        assert self.daily_available_time.is_recurring is True
        assert self.daily_available_time.is_recurring_instance is False

        # Test non-recurring object
        assert self.single_available_time.is_recurring is False
        assert self.single_available_time.is_recurring_instance is False

        # Create and test recurring instance
        instance = AvailableTime.objects.create(
            calendar=self.calendar,
            start_time=self.now + timedelta(days=5, hours=9),
            end_time=self.now + timedelta(days=5, hours=17),
            parent_recurring_object=self.daily_available_time,
            recurrence_id=self.now + timedelta(days=5, hours=9),
            is_recurring_exception=True,
            organization=self.organization,
        )

        assert instance.is_recurring is False
        assert instance.is_recurring_instance is True

    def test_manager_delegation_methods(self):
        """Test that manager methods properly delegate to queryset."""
        # Test manager methods delegate correctly
        masters = AvailableTime.objects.filter_master_recurring_objects()
        assert masters.count() >= 2

        instances = AvailableTime.objects.filter_recurring_instances()
        # Should be 0 initially since we haven't created any instances yet
        assert instances.count() == 0

        non_recurring = AvailableTime.objects.filter_non_recurring_objects()
        assert non_recurring.count() >= 1

    def test_duration_property(self):
        """Test duration property works correctly for AvailableTime."""
        expected_duration = timedelta(hours=8)  # 9 AM to 5 PM
        assert self.daily_available_time.duration == expected_duration

        expected_weekly_duration = timedelta(hours=2)  # 10 AM to 12 PM
        assert self.weekly_available_time.duration == expected_weekly_duration

    def test_chaining_queryset_methods(self):
        """Test chaining queryset methods for AvailableTime."""
        # Test chaining filter methods
        result = (
            AvailableTime.objects.filter_by_organization(organization_id=self.organization.id)
            .filter_master_recurring_objects()
            .filter(start_time__hour=9)  # Only 9 AM start times
        )

        assert result.count() == 1
        assert result.first().id == self.daily_available_time.id


@pytest.mark.django_db
class TestRecurringIntegration(TestCase):
    """Integration tests for recurring functionality across BlockedTime and AvailableTime."""

    def setUp(self):
        """Set up test data for integration tests."""
        self.organization = Organization.objects.create(
            name="Test Organization",
            should_sync_rooms=True,
        )

        self.calendar = Calendar.objects.create(
            name="Test Calendar",
            description="Test calendar for integration tests",
            email="test@example.com",
            external_id="test_123",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            organization=self.organization,
        )

        self.now = timezone.now().replace(second=0, microsecond=0)

        # Create a shared recurrence rule
        self.shared_rule = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.DAILY,
            interval=1,
            count=5,
            organization=self.organization,
        )

    def test_blocked_and_available_time_with_same_rule(self):
        """Test that both BlockedTime and AvailableTime can use the same recurrence rule."""
        # Create recurring blocked time
        blocked_time = BlockedTime.objects.create(
            calendar=self.calendar,
            start_time=self.now + timedelta(days=1, hours=12),
            end_time=self.now + timedelta(days=1, hours=13),  # Lunch break
            reason="Daily lunch break",
            recurrence_rule=self.shared_rule,
            organization=self.organization,
        )

        # Create recurring available time with same rule
        available_time = AvailableTime.objects.create(
            calendar=self.calendar,
            start_time=self.now + timedelta(days=1, hours=9),
            end_time=self.now + timedelta(days=1, hours=17),  # Work hours
            recurrence_rule=self.shared_rule,
            organization=self.organization,
        )

        # Both should be recurring
        assert blocked_time.is_recurring is True
        assert available_time.is_recurring is True

        # Both should use the same rule
        assert blocked_time.recurrence_rule == self.shared_rule
        assert available_time.recurrence_rule == self.shared_rule

    def test_master_objects_count_across_models(self):
        """Test counting master recurring objects across both models."""
        # Create different rules for each model
        blocked_rule = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.WEEKLY,
            interval=1,
            count=3,
            organization=self.organization,
        )

        available_rule = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.DAILY,
            interval=1,
            count=7,
            organization=self.organization,
        )

        # Create recurring objects
        BlockedTime.objects.create(
            calendar=self.calendar,
            start_time=self.now + timedelta(days=1),
            end_time=self.now + timedelta(days=1, hours=1),
            reason="Weekly block",
            recurrence_rule=blocked_rule,
            organization=self.organization,
        )

        AvailableTime.objects.create(
            calendar=self.calendar,
            start_time=self.now + timedelta(days=1),
            end_time=self.now + timedelta(days=1, hours=2),
            recurrence_rule=available_rule,
            organization=self.organization,
        )

        # Count master objects in each model
        blocked_masters = BlockedTime.objects.filter_by_organization(
            organization_id=self.organization.id
        ).filter_master_recurring_objects()

        available_masters = AvailableTime.objects.filter_by_organization(
            organization_id=self.organization.id
        ).filter_master_recurring_objects()

        assert blocked_masters.count() == 1
        assert available_masters.count() == 1

    def test_database_functions_work_independently(self):
        """Test that database functions work independently for each model."""
        # Create different recurring objects
        daily_rule = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.DAILY,
            interval=1,
            count=3,
            organization=self.organization,
        )

        blocked_time = BlockedTime.objects.create(
            calendar=self.calendar,
            start_time=self.now + timedelta(days=1, hours=10),
            end_time=self.now + timedelta(days=1, hours=11),
            reason="Daily block",
            recurrence_rule=daily_rule,
            organization=self.organization,
        )

        available_time = AvailableTime.objects.create(
            calendar=self.calendar,
            start_time=self.now + timedelta(days=1, hours=14),
            end_time=self.now + timedelta(days=1, hours=16),
            recurrence_rule=daily_rule,
            organization=self.organization,
        )

        start_date = self.now
        end_date = self.now + timedelta(days=5)

        # Test BlockedTime database function
        blocked_with_occurrences = (
            BlockedTime.objects.filter_by_organization(organization_id=self.organization.id)
            .filter(id=blocked_time.id)
            .annotate_recurring_occurrences_on_date_range(start_date, end_date)
        )

        blocked_result = blocked_with_occurrences.first()
        assert hasattr(blocked_result, "recurring_occurrences")
        assert blocked_result.recurring_occurrences is not None

        # Test AvailableTime database function
        available_with_occurrences = (
            AvailableTime.objects.filter_by_organization(organization_id=self.organization.id)
            .filter(id=available_time.id)
            .annotate_recurring_occurrences_on_date_range(start_date, end_date)
        )

        available_result = available_with_occurrences.first()
        assert hasattr(available_result, "recurring_occurrences")
        assert available_result.recurring_occurrences is not None


@pytest.mark.django_db
class TestBulkModificationQuerySet:
    def test_calendar_event_get_occurrences_with_bulk_modifications(self):
        organization = Organization.objects.create(name="Test Organization", should_sync_rooms=True)
        calendar = Calendar.objects.create(
            name="Test Calendar",
            email="test@example.com",
            external_id="test_123",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            organization=organization,
        )
        rule = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.DAILY, interval=1, count=5, organization=organization
        )
        event = CalendarEvent.objects.create(
            calendar=calendar,
            title="Daily Standup",
            description="Daily standup meeting",
            start_time=_dt(2023, 10, 1, 9, 0),
            end_time=_dt(2023, 10, 1, 9, 30),
            external_id="event_123",
            recurrence_rule=rule,
            organization=organization,
        )
        # Create continuation event (bulk modification)
        continuation_rule = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.DAILY, interval=1, count=2, organization=organization
        )
        _continuation = CalendarEvent.objects.create(
            calendar=calendar,
            title="Modified Standup",
            description="Modified",
            start_time=_dt(2023, 10, 6, 9, 0),
            end_time=_dt(2023, 10, 6, 9, 30),
            external_id="event_124",
            recurrence_rule=continuation_rule,
            organization=organization,
            bulk_modification_parent=event,
        )
        occurrences = event.get_occurrences_in_range_with_bulk_modifications(
            _dt(2023, 10, 1), _dt(2023, 10, 10), include_continuations=True
        )
        assert len(occurrences) == 7

    def test_queryset_bulk_modification_annotation(self):
        organization = Organization.objects.create(name="Test Organization", should_sync_rooms=True)
        calendar = Calendar.objects.create(
            name="Test Calendar",
            email="test@example.com",
            external_id="test_124",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            organization=organization,
        )
        rule1 = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.DAILY, interval=1, count=2, organization=organization
        )
        rule2 = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.DAILY, interval=1, count=2, organization=organization
        )
        _at1 = AvailableTime.objects.create(
            calendar=calendar,
            start_time=_dt(2023, 10, 1, 9, 0),
            end_time=_dt(2023, 10, 1, 17, 0),
            recurrence_rule=rule1,
            organization=organization,
        )
        _at2 = AvailableTime.objects.create(
            calendar=calendar,
            start_time=_dt(2023, 10, 2, 9, 0),
            end_time=_dt(2023, 10, 2, 17, 0),
            recurrence_rule=rule2,
            organization=organization,
        )
        available_times = AvailableTime.objects.filter(
            calendar=calendar, organization=organization
        ).annotate_recurring_occurrences_with_bulk_modifications_on_date_range(
            _dt(2023, 10, 1), _dt(2023, 10, 3)
        )
        assert available_times.count() == 2
        for obj in available_times:
            assert hasattr(obj, "recurring_occurrences_with_bulk_modifications")

    def test_blocked_time_bulk_modifications(self):
        organization = Organization.objects.create(name="Test Organization", should_sync_rooms=True)
        calendar = Calendar.objects.create(
            name="Test Calendar",
            email="test@example.com",
            external_id="test_125",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            organization=organization,
        )
        rule1 = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.DAILY, interval=1, count=2, organization=organization
        )
        rule2 = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.DAILY, interval=1, count=2, organization=organization
        )
        _bt1 = BlockedTime.objects.create(
            calendar=calendar,
            start_time=_dt(2023, 10, 1, 10, 0),
            end_time=_dt(2023, 10, 1, 11, 0),
            reason="Daily meeting",
            recurrence_rule=rule1,
            organization=organization,
            external_id="blocked_1",
        )
        _bt2 = BlockedTime.objects.create(
            calendar=calendar,
            start_time=_dt(2023, 10, 2, 10, 0),
            end_time=_dt(2023, 10, 2, 11, 0),
            reason="Weekly sync",
            recurrence_rule=rule2,
            organization=organization,
            external_id="blocked_2",
        )
        blocked_times = BlockedTime.objects.filter(
            calendar=calendar, organization=organization
        ).annotate_recurring_occurrences_with_bulk_modifications_on_date_range(
            _dt(2023, 10, 1), _dt(2023, 10, 3)
        )
        assert blocked_times.count() == 2
        for obj in blocked_times:
            assert hasattr(obj, "recurring_occurrences_with_bulk_modifications")

    def test_available_time_bulk_modifications(self):
        organization = Organization.objects.create(name="Test Organization", should_sync_rooms=True)
        calendar = Calendar.objects.create(
            name="Test Calendar",
            email="test@example.com",
            external_id="test_126",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            organization=organization,
        )
        rule1 = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.WEEKLY, interval=1, count=1, organization=organization
        )
        rule2 = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.WEEKLY, interval=1, count=1, organization=organization
        )
        at1 = AvailableTime.objects.create(
            calendar=calendar,
            start_time=_dt(2023, 10, 1, 9, 0),
            end_time=_dt(2023, 10, 1, 17, 0),
            recurrence_rule=rule1,
            organization=organization,
        )
        _at2 = AvailableTime.objects.create(
            calendar=calendar,
            start_time=_dt(2023, 10, 8, 9, 0),
            end_time=_dt(2023, 10, 8, 17, 0),
            recurrence_rule=rule2,
            organization=organization,
            bulk_modification_parent=at1,
        )
        all_occurrences = at1.get_occurrences_in_range_with_bulk_modifications(
            _dt(2023, 10, 1), _dt(2023, 10, 15), include_continuations=True
        )
        assert len(all_occurrences) == 2
        available_times = AvailableTime.objects.filter(
            calendar=calendar, organization=organization
        ).annotate_recurring_occurrences_with_bulk_modifications_on_date_range(
            _dt(2023, 10, 1), _dt(2023, 10, 15)
        )
        for obj in available_times:
            assert hasattr(obj, "recurring_occurrences_with_bulk_modifications")

    def test_manager_bulk_modification_methods(self):
        organization = Organization.objects.create(name="Test Organization", should_sync_rooms=True)
        calendar = Calendar.objects.create(
            name="Test Calendar",
            email="test@example.com",
            external_id="test_127",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            organization=organization,
        )
        rule = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.DAILY, interval=1, count=2, organization=organization
        )
        _event = CalendarEvent.objects.create(
            calendar=calendar,
            title="Test Event",
            start_time=_dt(2023, 10, 1, 9, 0),
            end_time=_dt(2023, 10, 1, 10, 0),
            recurrence_rule=rule,
            organization=organization,
            external_id="event_128",
        )
        events_with_bulk_occurrences = CalendarEvent.objects.filter(
            organization=organization
        ).annotate_recurring_occurrences_with_bulk_modifications_on_date_range(
            _dt(2023, 10, 1), _dt(2023, 10, 5), 10
        )
        for obj in events_with_bulk_occurrences:
            assert hasattr(obj, "recurring_occurrences")
        blocked_rule = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.DAILY, interval=1, count=2, organization=organization
        )
        _blocked = BlockedTime.objects.create(
            calendar=calendar,
            start_time=_dt(2023, 10, 1, 10, 0),
            end_time=_dt(2023, 10, 1, 11, 0),
            recurrence_rule=blocked_rule,
            organization=organization,
            external_id="blocked_3",
        )
        blocked_with_bulk = BlockedTime.objects.filter(
            organization=organization
        ).annotate_recurring_occurrences_with_bulk_modifications_on_date_range(
            _dt(2023, 10, 1), _dt(2023, 10, 5)
        )
        for obj in blocked_with_bulk:
            assert hasattr(obj, "recurring_occurrences_with_bulk_modifications")

    def test_bulk_cancellation_support(self):
        organization = Organization.objects.create(name="Test Organization", should_sync_rooms=True)
        calendar = Calendar.objects.create(
            name="Test Calendar",
            email="test@example.com",
            external_id="test_128",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            organization=organization,
        )
        rule = RecurrenceRule.objects.create(
            frequency=RecurrenceFrequency.DAILY, interval=1, count=5, organization=organization
        )
        event = CalendarEvent.objects.create(
            calendar=calendar,
            title="Event to Cancel",
            start_time=_dt(2023, 10, 1, 9, 0),
            end_time=_dt(2023, 10, 1, 10, 0),
            recurrence_rule=rule,
            organization=organization,
            external_id="event_129",
        )
        # Simulate cancellation by creating a continuation event with recurrence_rule=None
        _ = CalendarEvent.objects.create(
            calendar=calendar,
            title="Cancelled Event",
            start_time=_dt(2023, 10, 3, 9, 0),
            end_time=_dt(2023, 10, 3, 10, 0),
            recurrence_rule=None,
            organization=organization,
            bulk_modification_parent=event,
            external_id="event_130",
        )
        all_occurrences = event.get_occurrences_in_range_with_bulk_modifications(
            _dt(2023, 10, 1), _dt(2023, 10, 10), include_continuations=True
        )
        assert len(all_occurrences) == 5
