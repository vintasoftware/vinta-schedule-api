"""
Tests for Phase 2: Google Calendar Webhook Receiver
"""

import datetime
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.urls import reverse

import pytest

from calendar_integration.constants import (
    CalendarProvider,
    CalendarSyncStatus,
    IncomingWebhookProcessingStatus,
)
from calendar_integration.exceptions import ServiceNotAuthenticatedError
from calendar_integration.models import (
    Calendar,
    CalendarSync,
    CalendarWebhookEvent,
    CalendarWebhookSubscription,
)
from calendar_integration.services.calendar_adapters.google_calendar_adapter import (
    GoogleCalendarAdapter,
)
from calendar_integration.services.calendar_service import CalendarService
from organizations.models import Organization


@override_settings(GOOGLE_CLIENT_ID="test_client_id", GOOGLE_CLIENT_SECRET="test_client_secret")
class GoogleCalendarAdapterWebhookTest(TestCase):
    """Test Google Calendar adapter webhook methods."""

    def setUp(self):
        self.credentials = {
            "token": "test_token",
            "refresh_token": "test_refresh_token",
            "account_id": "test_account",
        }

    @patch("calendar_integration.services.calendar_adapters.google_calendar_adapter.build")
    def test_validate_webhook_notification_valid(self, mock_build):
        """Test validation of valid Google webhook notification."""
        adapter = GoogleCalendarAdapter(self.credentials)

        headers = {
            "X-Goog-Resource-ID": "test-resource-id",
            "X-Goog-Resource-URI": "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            "X-Goog-Resource-State": "exists",
            "X-Goog-Channel-ID": "test-channel-id",
            "X-Goog-Channel-Token": "test-token",
        }

        result = adapter.validate_webhook_notification(headers, "")

        assert result["provider"] == "google"
        assert result["calendar_id"] == "primary"
        assert result["resource_id"] == "test-resource-id"
        assert result["event_type"] == "exists"
        assert result["channel_id"] == "test-channel-id"

    @patch("calendar_integration.services.calendar_adapters.google_calendar_adapter.build")
    def test_validate_webhook_notification_missing_headers(self, mock_build):
        """Test validation with missing required headers."""
        adapter = GoogleCalendarAdapter(self.credentials)

        headers = {
            "X-Goog-Resource-ID": "test-resource-id",
            # Missing other required headers
        }

        with pytest.raises(ValueError, match="Missing required Google webhook headers"):
            adapter.validate_webhook_notification(headers, "")

    @patch("calendar_integration.services.calendar_adapters.google_calendar_adapter.build")
    def test_validate_webhook_notification_invalid_resource_uri(self, mock_build):
        """Test validation with invalid resource URI."""
        adapter = GoogleCalendarAdapter(self.credentials)

        headers = {
            "X-Goog-Resource-ID": "test-resource-id",
            "X-Goog-Resource-URI": "https://invalid-uri.com/not-calendar",
            "X-Goog-Resource-State": "exists",
            "X-Goog-Channel-ID": "test-channel-id",
            "X-Goog-Channel-Token": "test-token",
        }

        with pytest.raises(ValueError, match="Could not extract calendar ID"):
            adapter.validate_webhook_notification(headers, "")

    def test_validate_webhook_notification_static(self):
        """Test static validation method."""
        headers = {
            "X-Goog-Resource-ID": "test-resource-id",
            "X-Goog-Resource-URI": "https://www.googleapis.com/calendar/v3/calendars/test-calendar/events",
            "X-Goog-Resource-State": "exists",
            "X-Goog-Channel-ID": "test-channel-id",
            "X-Goog-Channel-Token": "test-token",
        }

        result = GoogleCalendarAdapter.validate_webhook_notification_static(headers, "")

        assert result["provider"] == "google"
        assert result["calendar_id"] == "test-calendar"
        assert result["event_type"] == "exists"

    @patch("calendar_integration.services.calendar_adapters.google_calendar_adapter.build")
    @patch(
        "calendar_integration.services.calendar_adapters.google_calendar_adapter.write_quote_limiter"
    )
    def test_create_webhook_subscription_with_tracking(self, mock_limiter, mock_build):
        """Test creating webhook subscription with tracking."""
        mock_client = Mock()
        mock_events = Mock()
        mock_client.events.return_value = mock_events
        mock_watch = Mock()
        mock_events.watch.return_value = mock_watch
        mock_watch.execute.return_value = {
            "id": "test-channel-id",
            "resourceId": "test-resource-id",
            "resourceUri": "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            "expiration": "1234567890000",
        }
        mock_build.return_value = mock_client

        adapter = GoogleCalendarAdapter(self.credentials)

        result = adapter.create_webhook_subscription_with_tracking(
            resource_id="primary",
            callback_url="https://example.com/webhook",
            tracking_params={"ttl_seconds": 3600},
        )

        assert result["channel_id"] == "test-channel-id"
        assert result["resource_id"] == "test-resource-id"
        assert result["calendar_id"] == "primary"
        assert result["callback_url"] == "https://example.com/webhook"


class CalendarServiceWebhookTest(TestCase):
    """Test CalendarService webhook methods."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.calendar = Calendar.objects.create(
            name="Test Calendar",
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            external_id="test-calendar-id",
        )
        self.service = CalendarService()
        self.service.organization = self.organization

    def test_request_webhook_triggered_sync_calendar_not_found(self):
        """Test webhook sync when calendar not found."""
        webhook_event = CalendarWebhookEvent.objects.create(
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            event_type="exists",
            external_calendar_id="nonexistent-calendar",
            external_event_id="",
            raw_payload={"raw": ""},
        )

        result = self.service.request_webhook_triggered_sync(
            external_calendar_id="nonexistent-calendar", webhook_event=webhook_event
        )

        assert result is None

    @patch("calendar_integration.tasks.sync_calendar_task.delay")
    def test_request_webhook_triggered_sync_success(self, mock_sync_task):
        """Test successful webhook-triggered sync."""
        webhook_event = CalendarWebhookEvent.objects.create(
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            event_type="exists",
            external_calendar_id="test-calendar-id",
            external_event_id="",
            raw_payload={"raw": ""},
        )

        # Mock the calendar service as authenticated
        self.service.account = Mock()
        self.service.account.id = 1
        self.service.calendar_adapter = Mock()

        result = self.service.request_webhook_triggered_sync(
            external_calendar_id="test-calendar-id", webhook_event=webhook_event
        )

        assert result is not None
        assert isinstance(result, CalendarSync)
        assert result.calendar == self.calendar

        # Refresh webhook event to check updates
        webhook_event.refresh_from_db()
        assert webhook_event.processing_status == IncomingWebhookProcessingStatus.PROCESSED
        assert webhook_event.calendar_sync == result

    @patch("calendar_integration.tasks.sync_calendar_task.delay")
    def test_request_webhook_triggered_sync_recent_sync_exists(self, mock_sync_task):
        """Test webhook sync when recent sync already exists."""
        # Create a recent sync
        recent_sync = CalendarSync.objects.create(
            calendar=self.calendar,
            organization=self.organization,
            start_datetime=datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(hours=12),
            end_datetime=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(hours=12),
            status=CalendarSyncStatus.SUCCESS,
            should_update_events=True,
        )

        webhook_event = CalendarWebhookEvent.objects.create(
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            event_type="exists",
            external_calendar_id="test-calendar-id",
            external_event_id="",
            raw_payload={"raw": ""},
        )

        # Mock the calendar service as authenticated
        self.service.account = Mock()
        self.service.calendar_adapter = Mock()

        result = self.service.request_webhook_triggered_sync(
            external_calendar_id="test-calendar-id", webhook_event=webhook_event
        )

        # Should return the existing sync, not create a new one
        assert result == recent_sync

        # Check that no new sync was created
        assert CalendarSync.objects.filter(organization=self.organization).count() == 1

        # Verify webhook event was updated
        webhook_event.refresh_from_db()
        assert webhook_event.processing_status == IncomingWebhookProcessingStatus.PROCESSED
        assert webhook_event.calendar_sync == recent_sync

    @override_settings(GOOGLE_CLIENT_ID="test_client_id", GOOGLE_CLIENT_SECRET="test_client_secret")
    @patch("calendar_integration.services.calendar_adapters.google_calendar_adapter.build")
    @patch(
        "calendar_integration.services.calendar_adapters.google_calendar_adapter.write_quote_limiter"
    )
    def test_create_calendar_webhook_subscription_google(self, mock_limiter, mock_build):
        """Test creating Google Calendar webhook subscription."""
        # Mock Google API response
        mock_client = Mock()
        mock_events = Mock()
        mock_client.events.return_value = mock_events
        mock_watch = Mock()
        mock_events.watch.return_value = mock_watch
        mock_watch.execute.return_value = {
            "id": "test-channel-id",
            "resourceId": "test-resource-id",
            "resourceUri": "https://www.googleapis.com/calendar/v3/calendars/test-calendar-id/events",
            "expiration": "1234567890000",
        }
        mock_build.return_value = mock_client

        # Mock authenticated service
        self.service.account = Mock()
        self.service.calendar_adapter = Mock()
        self.service.calendar_adapter.create_webhook_subscription_with_tracking.return_value = {
            "channel_id": "test-channel-id",
            "resource_id": "test-resource-id",
            "resource_uri": "https://www.googleapis.com/calendar/v3/calendars/test-calendar-id/events",
            "expiration": "1234567890000",
            "calendar_id": "test-calendar-id",
            "callback_url": "https://example.com/webhook",
            "channel_token": "test-verification-token",
        }

        result = self.service.create_calendar_webhook_subscription(
            calendar=self.calendar, callback_url="https://example.com/webhook", expiration_hours=24
        )

        assert isinstance(result, CalendarWebhookSubscription)
        assert result.calendar == self.calendar
        assert result.provider == CalendarProvider.GOOGLE
        assert result.callback_url == "https://example.com/webhook"
        assert result.channel_id == "test-channel-id"

    def test_create_calendar_webhook_subscription_not_authenticated(self):
        """Test creating webhook subscription when not authenticated."""
        # Create a fresh service instance that's truly not authenticated
        unauthenticated_service = CalendarService()
        # Don't set organization, account, or adapter

        with pytest.raises(
            ServiceNotAuthenticatedError, match="Calendar service is not authenticated"
        ):
            unauthenticated_service.create_calendar_webhook_subscription(
                calendar=self.calendar, callback_url="https://example.com/webhook"
            )

    @patch.object(CalendarService, "request_webhook_triggered_sync")
    def test_process_webhook_notification_google(self, mock_sync):
        """Test processing Google webhook notification."""
        mock_sync.return_value = Mock(id=123)

        headers = {
            "X-Goog-Resource-ID": "test-resource-id",
            "X-Goog-Resource-URI": "https://www.googleapis.com/calendar/v3/calendars/test-calendar-id/events",
            "X-Goog-Resource-State": "exists",
            "X-Goog-Channel-ID": "test-channel-id",
            "X-Goog-Channel-Token": "test-token",
        }

        result = self.service.process_webhook_notification(provider="google", headers=headers)

        assert isinstance(result, CalendarWebhookEvent)
        assert result.provider == "google"
        assert result.event_type == "exists"
        assert result.external_calendar_id == "test-calendar-id"


class GoogleCalendarWebhookViewTest(TestCase):
    """Test Google Calendar webhook view."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.calendar = Calendar.objects.create(
            name="Test Calendar",
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            external_id="test-calendar-id",
        )
        self.webhook_url = reverse("calendar_webhooks:google-calendar-webhook")

    def test_google_webhook_sync_notification_ignored(self):
        """Test that sync notifications are ignored."""
        headers = {
            "HTTP_X_GOOG_CHANNEL_ID": "test-channel-id",
            "HTTP_X_GOOG_RESOURCE_ID": "test-resource-id",
            "HTTP_X_GOOG_RESOURCE_URI": "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            "HTTP_X_GOOG_RESOURCE_STATE": "sync",
            "HTTP_X_GOOG_CHANNEL_TOKEN": "test-token",
        }

        response = self.client.post(self.webhook_url, **headers)

        assert response.status_code == 200
        # No webhook event should be created for sync notifications
        assert CalendarWebhookEvent.objects.filter(organization=self.organization).count() == 0

    def test_google_webhook_exists_notification(self):
        """Test processing exists notification."""
        # Create webhook subscription to help with organization lookup
        CalendarWebhookSubscription.objects.create(
            calendar=self.calendar,
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            external_subscription_id="test-sub-id",
            channel_id="test-channel-id",
            callback_url="https://example.com/webhook",
            verification_token="test-token",
        )

        headers = {
            "HTTP_X_GOOG_CHANNEL_ID": "test-channel-id",
            "HTTP_X_GOOG_RESOURCE_ID": "test-resource-id",
            "HTTP_X_GOOG_RESOURCE_URI": "https://www.googleapis.com/calendar/v3/calendars/test-calendar-id/events",
            "HTTP_X_GOOG_RESOURCE_STATE": "exists",
            "HTTP_X_GOOG_CHANNEL_TOKEN": "test-token",
        }

        response = self.client.post(self.webhook_url, **headers)

        # Webhook should always return 200, even if service isn't fully authenticated
        assert response.status_code == 200

        # Verify webhook event was created
        assert (
            CalendarWebhookEvent.objects.filter(
                organization=self.organization,
                provider=CalendarProvider.GOOGLE,
                external_calendar_id="test-calendar-id",
            ).count()
            == 1
        )

    def test_google_webhook_missing_headers(self):
        """Test webhook with missing required headers."""
        # Missing most required headers
        headers = {
            "HTTP_X_GOOG_CHANNEL_ID": "test-channel-id",
        }

        response = self.client.post(self.webhook_url, **headers)

        assert response.status_code == 400

    def test_google_webhook_no_organization_found(self):
        """Test webhook when organization cannot be determined."""
        headers = {
            "HTTP_X_GOOG_CHANNEL_ID": "nonexistent-channel-id",
            "HTTP_X_GOOG_RESOURCE_ID": "test-resource-id",
            "HTTP_X_GOOG_RESOURCE_URI": "https://www.googleapis.com/calendar/v3/calendars/test-calendar-id/events",
            "HTTP_X_GOOG_RESOURCE_STATE": "exists",
            "HTTP_X_GOOG_CHANNEL_TOKEN": "test-token",
        }

        response = self.client.post(self.webhook_url, **headers)

        # Webhook should still return 200 even when organization can't be determined
        # The fallback logic in the webhook view handles this by using the first available org
        assert response.status_code == 200

        # Verify webhook event was still created (using fallback organization)
        # Use the test organization to check since we know it exists
        assert CalendarWebhookEvent.objects.filter(organization=self.organization).count() >= 1


class GoogleCalendarWebhookIntegrationTest(TestCase):
    """Integration tests for Google Calendar webhook functionality."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.calendar = Calendar.objects.create(
            name="Test Calendar",
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            external_id="test-calendar-id",
        )

    @patch("calendar_integration.tasks.sync_calendar_task.delay")
    @patch("calendar_integration.webhook_views.CalendarService")
    def test_end_to_end_webhook_processing(self, mock_service_class, mock_sync_task):
        """Test complete webhook processing flow."""
        # Create webhook subscription
        CalendarWebhookSubscription.objects.create(
            calendar=self.calendar,
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            external_subscription_id="test-sub-id",
            channel_id="test-channel-id",
            callback_url="https://example.com/webhook",
            verification_token="test-token",
        )

        # Mock CalendarService
        mock_service = CalendarService()
        mock_service.organization = self.organization
        mock_service.account = Mock()
        mock_service.account.id = 1
        mock_service.calendar_adapter = Mock()
        mock_service_class.return_value = mock_service

        webhook_url = reverse("calendar_webhooks:google-calendar-webhook")

        headers = {
            "HTTP_X_GOOG_CHANNEL_ID": "test-channel-id",
            "HTTP_X_GOOG_RESOURCE_ID": "test-resource-id",
            "HTTP_X_GOOG_RESOURCE_URI": "https://www.googleapis.com/calendar/v3/calendars/test-calendar-id/events",
            "HTTP_X_GOOG_RESOURCE_STATE": "exists",
            "HTTP_X_GOOG_CHANNEL_TOKEN": "test-token",
        }

        response = self.client.post(webhook_url, **headers)

        assert response.status_code == 200

        # Verify webhook event was created
        assert CalendarWebhookEvent.objects.filter(organization=self.organization).count() == 1
