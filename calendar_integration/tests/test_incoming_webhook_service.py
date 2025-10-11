"""
Tests for Phase 1: Core Webhook Infrastructure
"""

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

import pytest

from calendar_integration.constants import CalendarProvider, IncomingWebhookProcessingStatus
from calendar_integration.exceptions import WebhookAuthenticationError, WebhookValidationError
from calendar_integration.models import Calendar, CalendarWebhookEvent, CalendarWebhookSubscription
from calendar_integration.services.incoming_webhook_service import CalendarIncomingWebhookService
from organizations.models import Organization


class CalendarWebhookSubscriptionModelTest(TestCase):
    """Test the CalendarWebhookSubscription model."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.calendar = Calendar.objects.create(
            name="Test Calendar",
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            external_id="test-calendar-id",
        )

    def test_webhook_subscription_creation(self):
        """Test creating a webhook subscription."""
        subscription = CalendarWebhookSubscription.objects.create(
            calendar=self.calendar,
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            external_subscription_id="test-sub-id",
            external_resource_id="test-resource-id",
            callback_url="https://example.com/webhook",
            channel_id="test-channel-id",
            verification_token="test-token",
        )

        assert subscription.calendar == self.calendar
        assert subscription.provider == CalendarProvider.GOOGLE
        assert subscription.is_active is True

    def test_webhook_subscription_str_representation(self):
        """Test string representation of webhook subscription."""
        subscription = CalendarWebhookSubscription.objects.create(
            calendar=self.calendar,
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            external_subscription_id="test-sub-id",
            external_resource_id="test-resource-id",
            callback_url="https://example.com/webhook",
        )

        expected = f"WebhookSubscription({CalendarProvider.GOOGLE}:{self.calendar.name})"
        assert str(subscription) == expected

    def test_webhook_subscription_unique_constraint(self):
        """Test that we can't create duplicate subscriptions for same org/calendar/provider."""
        CalendarWebhookSubscription.objects.create(
            calendar=self.calendar,
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            external_subscription_id="test-sub-1",
            external_resource_id="test-resource-id",
            callback_url="https://example.com/webhook",
        )

        # Attempting to create another subscription for same org/calendar/provider should fail
        with pytest.raises((IntegrityError, ValueError)):
            CalendarWebhookSubscription.objects.create(
                calendar=self.calendar,
                organization=self.organization,
                provider=CalendarProvider.GOOGLE,
                external_subscription_id="test-sub-2",
                external_resource_id="test-resource-id-2",
                callback_url="https://example.com/webhook2",
            )


class CalendarWebhookEventModelTest(TestCase):
    """Test the CalendarWebhookEvent model."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")

    def test_webhook_event_creation(self):
        """Test creating a webhook event."""
        event = CalendarWebhookEvent.objects.create(
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            event_type="created",
            external_calendar_id="test-calendar-id",
            external_event_id="test-event-id",
            raw_payload={"test": "data"},
            headers={"X-Test": "header"},
        )

        assert event.provider == CalendarProvider.GOOGLE
        assert event.processing_status == IncomingWebhookProcessingStatus.PENDING
        assert event.sync_triggered is False
        assert event.error_message is None

    def test_webhook_event_properties(self):
        """Test webhook event derived properties."""
        from calendar_integration.constants import CalendarSyncStatus
        from calendar_integration.models import CalendarSync

        calendar = Calendar.objects.create(
            name="Test Calendar",
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            external_id="test-calendar-id",
        )

        # Create a failed sync
        sync = CalendarSync.objects.create(
            calendar=calendar,
            organization=self.organization,
            start_datetime=timezone.now(),
            end_datetime=timezone.now(),
            should_update_events=True,
            status=CalendarSyncStatus.FAILED,
            error_message="Test error message",
        )

        # Create webhook event linked to the sync
        event = CalendarWebhookEvent.objects.create(
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            event_type="created",
            external_calendar_id="test-calendar-id",
            raw_payload={"test": "data"},
            headers={},
            calendar_sync=sync,
        )

        # Test properties
        assert event.sync_triggered is True
        assert event.error_message == "Test error message"


class CalendarIncomingWebhookServiceTest(TestCase):
    """Test the CalendarIncomingWebhookService."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.service = CalendarIncomingWebhookService(self.organization)

    def test_google_webhook_validation_success(self):
        """Test successful Google webhook validation."""
        headers = {
            "X-Goog-Channel-ID": "test-channel",
            "X-Goog-Resource-ID": "test-resource",
            "X-Goog-Resource-State": "exists",
        }

        result = self.service.validate_webhook_signature(
            CalendarProvider.GOOGLE, headers, b"test body"
        )

        assert result is True

    def test_google_webhook_validation_missing_headers(self):
        """Test Google webhook validation with missing headers."""
        headers = {
            "X-Goog-Channel-ID": "test-channel",
            # Missing required headers
        }

        with pytest.raises(WebhookAuthenticationError):
            self.service.validate_webhook_signature(CalendarProvider.GOOGLE, headers, b"test body")

    def test_microsoft_webhook_validation_token(self):
        """Test Microsoft webhook validation with validation token."""
        headers = {"validationToken": "test-validation-token"}

        result = self.service.validate_webhook_signature(
            CalendarProvider.MICROSOFT, headers, b"test body"
        )

        assert result is True

    def test_unsupported_provider(self):
        """Test webhook validation with unsupported provider."""
        with pytest.raises(WebhookValidationError):
            self.service.validate_webhook_signature("unsupported", {}, b"test body")

    def test_process_microsoft_validation_request(self):
        """Test processing Microsoft validation request."""
        result = self.service.process_webhook_notification(
            provider=CalendarProvider.MICROSOFT,
            headers={"validationToken": "test-token"},
            payload={},
            validation_token="test-token",
        )

        assert result == "test-token"

    def test_process_google_webhook_notification(self):
        """Test processing Google webhook notification."""
        headers = {
            "X-Goog-Channel-ID": "test-channel",
            "X-Goog-Resource-ID": "test-resource",
            "X-Goog-Resource-State": "exists",
            "X-Goog-Resource-URI": "https://www.googleapis.com/calendar/v3/calendars/test-cal-id/events",
        }
        payload = {"test": "notification"}

        result = self.service.process_webhook_notification(
            provider=CalendarProvider.GOOGLE, headers=headers, payload=payload
        )

        assert isinstance(result, CalendarWebhookEvent)
        assert result.provider == CalendarProvider.GOOGLE
        assert result.event_type == "exists"
        assert result.external_calendar_id == "test-cal-id"
        assert result.processing_status == IncomingWebhookProcessingStatus.PROCESSED

    def test_extract_google_calendar_id(self):
        """Test extracting calendar ID from Google resource URI."""
        uri = "https://www.googleapis.com/calendar/v3/calendars/test-calendar-123/events"
        result = self.service._extract_google_calendar_id(uri)
        assert result == "test-calendar-123"

        # Test with invalid URI
        result = self.service._extract_google_calendar_id("invalid-uri")
        assert result == "unknown"

        # Test with empty URI
        result = self.service._extract_google_calendar_id("")
        assert result == "unknown"

    def test_get_webhook_subscription(self):
        """Test getting webhook subscription."""
        calendar = Calendar.objects.create(
            name="Test Calendar",
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            external_id="test-calendar-id",
        )

        subscription = CalendarWebhookSubscription.objects.create(
            calendar=calendar,
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            external_subscription_id="test-sub-id",
            external_resource_id="test-resource-id",
            callback_url="https://example.com/webhook",
        )

        result = self.service.get_webhook_subscription(CalendarProvider.GOOGLE, "test-sub-id")

        assert result == subscription

    def test_get_nonexistent_webhook_subscription(self):
        """Test getting nonexistent webhook subscription."""
        result = self.service.get_webhook_subscription(CalendarProvider.GOOGLE, "nonexistent-id")

        assert result is None
