"""Tests for Phase 4 webhook management and monitoring features."""

import datetime
from unittest.mock import patch

from django.test import TestCase

import pytest

from calendar_integration.constants import CalendarProvider, IncomingWebhookProcessingStatus
from calendar_integration.models import CalendarWebhookEvent
from calendar_integration.services.webhook_analytics_service import WebhookAnalyticsService
from organizations.models import Organization


@pytest.mark.django_db
class TestWebhookAnalyticsService:
    """Test webhook analytics service functionality."""

    def setup_method(self):
        """Set up test data."""
        self.organization = Organization.objects.create(name="Test Org")
        self.analytics_service = WebhookAnalyticsService(self.organization)

    def test_get_webhook_delivery_stats_no_events(self):
        """Test delivery stats with no events."""
        stats = self.analytics_service.get_webhook_delivery_stats()

        assert stats["total_events"] == 0
        assert stats["successful_events"] == 0
        assert stats["failed_events"] == 0
        assert stats["success_rate"] == 100.0

    @patch("calendar_integration.services.webhook_analytics_service.datetime")
    def test_get_webhook_delivery_stats_with_events(self, mock_datetime):
        """Test delivery stats with sample events."""
        # Mock the current time
        now = datetime.datetime(2025, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
        mock_datetime.datetime.now.return_value = now
        mock_datetime.timedelta = datetime.timedelta
        mock_datetime.UTC = datetime.UTC

        # Create test events
        start_time = now - datetime.timedelta(hours=1)

        CalendarWebhookEvent.objects.create(
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            event_type="created",
            external_calendar_id="test-cal",
            processing_status=IncomingWebhookProcessingStatus.PROCESSED,
            created=start_time,
            processed_at=start_time + datetime.timedelta(seconds=5),
            raw_payload={"test": "payload"},
        )

        CalendarWebhookEvent.objects.create(
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            event_type="updated",
            external_calendar_id="test-cal",
            processing_status=IncomingWebhookProcessingStatus.FAILED,
            created=start_time,
            raw_payload={"test": "payload"},
        )

        stats = self.analytics_service.get_webhook_delivery_stats(hours_back=24)

        assert stats["total_events"] == 2
        assert stats["successful_events"] == 1
        assert stats["failed_events"] == 1
        assert stats["success_rate"] == 50.0
        assert stats["failure_rate"] == 50.0

    def test_get_webhook_latency_metrics_no_events(self):
        """Test latency metrics with no processed events."""
        metrics = self.analytics_service.get_webhook_latency_metrics()

        assert metrics["min_latency"] == 0.0
        assert metrics["max_latency"] == 0.0
        assert metrics["avg_latency"] == 0.0

    @patch("calendar_integration.services.webhook_analytics_service.datetime")
    def test_get_webhook_latency_metrics_with_events(self, mock_datetime):
        """Test latency metrics with processed events."""
        # Mock the current time
        now = datetime.datetime(2025, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
        mock_datetime.datetime.now.return_value = now
        mock_datetime.timedelta = datetime.timedelta
        mock_datetime.UTC = datetime.UTC

        # Create test events with different processing times
        start_time = now - datetime.timedelta(hours=1)

        CalendarWebhookEvent.objects.create(
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            event_type="created",
            external_calendar_id="test-cal",
            processing_status=IncomingWebhookProcessingStatus.PROCESSED,
            created=start_time,
            processed_at=start_time + datetime.timedelta(seconds=2),  # 2 second latency
            raw_payload={"test": "payload"},
        )

        CalendarWebhookEvent.objects.create(
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            event_type="updated",
            external_calendar_id="test-cal",
            processing_status=IncomingWebhookProcessingStatus.PROCESSED,
            created=start_time,
            processed_at=start_time + datetime.timedelta(seconds=4),  # 4 second latency
            raw_payload={"test": "payload"},
        )

        metrics = self.analytics_service.get_webhook_latency_metrics(hours_back=24)

        assert metrics["min_latency"] == 2.0
        assert metrics["max_latency"] == 4.0
        assert metrics["avg_latency"] == 3.0

    def test_generate_webhook_failure_alert_no_alert(self):
        """Test alert generation when failure rate is acceptable."""
        # Create successful events only
        now = datetime.datetime.now(tz=datetime.UTC)

        for i in range(10):
            CalendarWebhookEvent.objects.create(
                organization=self.organization,
                provider=CalendarProvider.GOOGLE,
                event_type="created",
                external_calendar_id=f"test-cal-{i}",
                processing_status=IncomingWebhookProcessingStatus.PROCESSED,
                created=now - datetime.timedelta(minutes=30),
                raw_payload={"test": "payload"},
            )

        alert = self.analytics_service.generate_webhook_failure_alert(
            failure_threshold_percent=20.0, hours_back=1
        )

        assert alert is None

    def test_generate_webhook_failure_alert_with_alert(self):
        """Test alert generation when failure rate exceeds threshold."""
        # Create events with high failure rate
        now = datetime.datetime.now(tz=datetime.UTC)

        # 3 successful, 7 failed = 70% failure rate
        for i in range(3):
            CalendarWebhookEvent.objects.create(
                organization=self.organization,
                provider=CalendarProvider.GOOGLE,
                event_type="created",
                external_calendar_id=f"test-cal-success-{i}",
                processing_status=IncomingWebhookProcessingStatus.PROCESSED,
                created=now - datetime.timedelta(minutes=30),
                raw_payload={"test": "payload"},
            )

        for i in range(7):
            CalendarWebhookEvent.objects.create(
                organization=self.organization,
                provider=CalendarProvider.GOOGLE,
                event_type="created",
                external_calendar_id=f"test-cal-fail-{i}",
                processing_status=IncomingWebhookProcessingStatus.FAILED,
                created=now - datetime.timedelta(minutes=30),
                raw_payload={"test": "payload"},
            )

        alert = self.analytics_service.generate_webhook_failure_alert(
            failure_threshold_percent=20.0, hours_back=1
        )

        assert alert is not None
        assert alert["alert_type"] == "high_webhook_failure_rate"
        assert alert["organization_id"] == self.organization.id
        assert alert["failure_rate"] == 70.0
        assert alert["threshold"] == 20.0
        assert "exceeds threshold" in alert["message"]

    @patch("calendar_integration.services.webhook_analytics_service.datetime")
    def test_cleanup_old_webhook_events(self, mock_datetime):
        """Test cleanup of old webhook events."""
        # Mock the current time
        now = datetime.datetime(2025, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
        mock_datetime.datetime.now.return_value = now
        mock_datetime.timedelta = datetime.timedelta
        mock_datetime.UTC = datetime.UTC

        # Create old and recent events
        old_time = now - datetime.timedelta(days=35)  # Older than 30 days
        recent_time = now - datetime.timedelta(days=5)  # Recent

        CalendarWebhookEvent.objects.create(
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            event_type="created",
            external_calendar_id="old-event",
            created=old_time,
            raw_payload={"test": "payload"},
        )

        CalendarWebhookEvent.objects.create(
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            event_type="created",
            external_calendar_id="recent-event",
            created=recent_time,
            raw_payload={"test": "payload"},
        )

        # Cleanup events older than 30 days
        deleted_count = self.analytics_service.cleanup_old_webhook_events(days_to_keep=30)

        assert deleted_count == 1

        # Verify only recent event remains
        remaining_events = CalendarWebhookEvent.objects.filter(organization=self.organization)
        assert remaining_events.count() == 1
        assert remaining_events.first().external_calendar_id == "recent-event"


class TestWebhookManagementCommands(TestCase):
    """Test webhook management commands."""

    def setUp(self):
        """Set up test data."""
        self.organization = Organization.objects.create(name="Test Org")

    def test_webhook_health_check_command_import(self):
        """Test that webhook health check command can be imported."""
        from calendar_integration.management.commands.webhook_health_check import Command

        command = Command()
        assert command.help == "Check webhook system health and generate diagnostic reports"  # noqa: A003

    def test_cleanup_webhook_events_command_import(self):
        """Test that cleanup command can be imported."""
        from calendar_integration.management.commands.cleanup_webhook_events import Command

        command = Command()
        assert command.help == "Clean up old webhook events to manage database size"  # noqa: A003

    def test_refresh_webhook_subscriptions_command_import(self):
        """Test that refresh command can be imported."""
        from calendar_integration.management.commands.refresh_webhook_subscriptions import Command

        command = Command()
        assert command.help == "Refresh expiring webhook subscriptions"  # noqa: A003


class TestWebhookAdmin(TestCase):
    """Test webhook admin interface."""

    def setUp(self):
        """Set up test data."""
        self.organization = Organization.objects.create(name="Test Org")

    def test_webhook_admin_imports(self):
        """Test that admin classes can be imported."""
        from calendar_integration.admin import (
            CalendarWebhookEventAdmin,
            CalendarWebhookSubscriptionAdmin,
            WebhookHealthDashboard,
        )

        assert CalendarWebhookEventAdmin is not None
        assert CalendarWebhookSubscriptionAdmin is not None
        assert WebhookHealthDashboard is not None


class TestPhase4Integration(TestCase):
    """Integration tests for Phase 4 features."""

    def setUp(self):
        """Set up test data."""
        self.organization = Organization.objects.create(name="Test Org")

    def test_graphql_types_import(self):
        """Test that new GraphQL types can be imported."""
        from calendar_integration.graphql import (
            CalendarWebhookEventGraphQLType,
            CalendarWebhookSubscriptionGraphQLType,
            WebhookSubscriptionStatusGraphQLType,
        )

        assert CalendarWebhookEventGraphQLType is not None
        assert CalendarWebhookSubscriptionGraphQLType is not None
        assert WebhookSubscriptionStatusGraphQLType is not None

    def test_mutations_import(self):
        """Test that webhook mutations can be imported."""
        from calendar_integration.mutations import (
            CalendarWebhookMutations,
            WebhookCleanupResult,
            WebhookDeleteResult,
            WebhookSubscriptionResult,
        )

        assert CalendarWebhookMutations is not None
        assert WebhookSubscriptionResult is not None
        assert WebhookDeleteResult is not None
        assert WebhookCleanupResult is not None
