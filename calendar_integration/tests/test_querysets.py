from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

import pytest

from calendar_integration.constants import CalendarProvider, CalendarSyncStatus, CalendarType
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarSync,
)
from organizations.models import Organization


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
