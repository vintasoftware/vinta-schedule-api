"""Tests for webhook analytics service."""

import datetime
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from model_bakery import baker

from calendar_integration.constants import CalendarSyncStatus, IncomingWebhookProcessingStatus

# Calendar integration models are accessed via baker.make strings
from calendar_integration.models import (
    CalendarSync,
    CalendarWebhookEvent,
)
from calendar_integration.services.webhook_analytics_service import WebhookAnalyticsService
from organizations.models import Organization


class TestWebhookAnalyticsService(TestCase):
    """Tests for WebhookAnalyticsService."""

    def setUp(self) -> None:
        """Set up test data."""
        self.organization = baker.make(Organization)
        self.service = WebhookAnalyticsService(self.organization)

    def test_init(self) -> None:
        """Test service initialization."""
        assert self.service.organization == self.organization

    def test_get_webhook_delivery_stats_no_events(self) -> None:
        """Test delivery stats when no events exist."""
        stats = self.service.get_webhook_delivery_stats(hours_back=24)

        expected = {
            "total_events": 0,
            "successful_events": 0,
            "failed_events": 0,
            "ignored_events": 0,
            "pending_events": 0,
            "success_rate": 100.0,
            "failure_rate": 0.0,
            "average_processing_time_seconds": 0.0,
        }

        assert stats == expected

    def test_get_webhook_delivery_stats_with_events(self) -> None:
        """Test delivery stats with various event statuses."""
        now = timezone.now()

        # Create events with different statuses
        baker.make(
            CalendarWebhookEvent,
            organization=self.organization,
            processing_status=IncomingWebhookProcessingStatus.PROCESSED,
            created=now - datetime.timedelta(hours=1),
            processed_at=now - datetime.timedelta(hours=1) + datetime.timedelta(seconds=30),
            _quantity=5,
        )

        baker.make(
            CalendarWebhookEvent,
            organization=self.organization,
            processing_status=IncomingWebhookProcessingStatus.FAILED,
            created=now - datetime.timedelta(hours=2),
            _quantity=2,
        )

        baker.make(
            CalendarWebhookEvent,
            organization=self.organization,
            processing_status=IncomingWebhookProcessingStatus.IGNORED,
            created=now - datetime.timedelta(hours=3),
            _quantity=1,
        )

        baker.make(
            CalendarWebhookEvent,
            organization=self.organization,
            processing_status=IncomingWebhookProcessingStatus.PENDING,
            created=now - datetime.timedelta(hours=4),
            _quantity=2,
        )

        stats = self.service.get_webhook_delivery_stats(hours_back=24)

        assert stats["total_events"] == 10
        assert stats["successful_events"] == 5
        assert stats["failed_events"] == 2
        assert stats["ignored_events"] == 1
        assert stats["pending_events"] == 2
        assert stats["success_rate"] == 50.0
        assert stats["failure_rate"] == 20.0

    def test_get_webhook_delivery_stats_time_filtering(self) -> None:
        """Test delivery stats respects time filtering."""
        now = timezone.now()

        # Create event within time window
        baker.make(
            CalendarWebhookEvent,
            organization=self.organization,
            processing_status=IncomingWebhookProcessingStatus.PROCESSED,
            created=now - datetime.timedelta(hours=1),
        )

        # Create event outside time window
        baker.make(
            CalendarWebhookEvent,
            organization=self.organization,
            processing_status=IncomingWebhookProcessingStatus.PROCESSED,
            created=now - datetime.timedelta(hours=25),
        )

        stats = self.service.get_webhook_delivery_stats(hours_back=24)

        assert stats["total_events"] == 1
        assert stats["successful_events"] == 1

    def test_get_webhook_latency_metrics_no_events(self) -> None:
        """Test latency metrics when no processed events exist."""
        metrics = self.service.get_webhook_latency_metrics(hours_back=24)

        expected = {
            "min_latency": 0.0,
            "max_latency": 0.0,
            "avg_latency": 0.0,
            "p50_latency": 0.0,
            "p95_latency": 0.0,
            "p99_latency": 0.0,
        }

        assert metrics == expected

    def test_get_webhook_latency_metrics_with_events(self) -> None:
        """Test latency metrics with processed events."""
        now = timezone.now()

        # Create processed events with different processing times
        baker.make(
            CalendarWebhookEvent,
            organization=self.organization,
            processing_status=IncomingWebhookProcessingStatus.PROCESSED,
            created=now - datetime.timedelta(hours=1),
            processed_at=now - datetime.timedelta(hours=1) + datetime.timedelta(seconds=1),
        )

        baker.make(
            CalendarWebhookEvent,
            organization=self.organization,
            processing_status=IncomingWebhookProcessingStatus.PROCESSED,
            created=now - datetime.timedelta(hours=2),
            processed_at=now - datetime.timedelta(hours=2) + datetime.timedelta(seconds=3),
        )

        metrics = self.service.get_webhook_latency_metrics(hours_back=24)

        assert metrics["avg_latency"] == 2.0
        assert metrics["min_latency"] == 1.0
        assert metrics["max_latency"] == 3.0

    def test_get_failed_webhooks(self) -> None:
        """Test getting failed webhooks."""
        now = timezone.now()

        # Create failed calendar syncs that will provide error messages
        failed_sync1 = baker.make(
            CalendarSync,
            organization=self.organization,
            status=CalendarSyncStatus.FAILED,
            error_message="Error 1",
        )

        failed_sync2 = baker.make(
            CalendarSync,
            organization=self.organization,
            status=CalendarSyncStatus.FAILED,
            error_message="Error 2",
        )

        # Create failed webhooks
        failed1 = baker.make(
            CalendarWebhookEvent,
            organization=self.organization,
            processing_status=IncomingWebhookProcessingStatus.FAILED,
            created=now - datetime.timedelta(hours=1),
            calendar_sync=failed_sync1,
        )

        failed2 = baker.make(
            CalendarWebhookEvent,
            organization=self.organization,
            processing_status=IncomingWebhookProcessingStatus.FAILED,
            created=now - datetime.timedelta(hours=2),
            calendar_sync=failed_sync2,
        )

        # Create successful webhook (should not be included)
        baker.make(
            CalendarWebhookEvent,
            organization=self.organization,
            processing_status=IncomingWebhookProcessingStatus.PROCESSED,
            created=now - datetime.timedelta(hours=3),
        )

        failed_webhooks = list(self.service.get_failed_webhooks(hours_back=24, limit=10))

        assert len(failed_webhooks) == 2
        # Should be ordered by most recent first
        assert failed_webhooks[0].id == failed1.id
        assert failed_webhooks[1].id == failed2.id

    def test_get_failed_webhooks_limit(self) -> None:
        """Test failed webhooks respects limit parameter."""
        now = timezone.now()

        # Create 5 failed webhooks
        baker.make(
            CalendarWebhookEvent,
            organization=self.organization,
            processing_status=IncomingWebhookProcessingStatus.FAILED,
            created=now - datetime.timedelta(hours=1),
            _quantity=5,
        )

        failed_webhooks = list(self.service.get_failed_webhooks(hours_back=24, limit=3))

        assert len(failed_webhooks) == 3

    def test_generate_webhook_failure_alert_no_alert(self) -> None:
        """Test failure alert when no alert condition is met."""
        # Create some successful events
        baker.make(
            CalendarWebhookEvent,
            organization=self.organization,
            processing_status=IncomingWebhookProcessingStatus.PROCESSED,
            _quantity=10,
        )

        alert = self.service.generate_webhook_failure_alert(hours_back=24)

        assert alert is None

    def test_generate_webhook_failure_alert_high_failure_rate(self) -> None:
        """Test failure alert for high failure rate."""
        now = timezone.now()

        # Create mostly failed events (>50% failure rate)
        baker.make(
            CalendarWebhookEvent,
            organization=self.organization,
            processing_status=IncomingWebhookProcessingStatus.FAILED,
            created=now - datetime.timedelta(hours=1),
            _quantity=7,
        )

        baker.make(
            CalendarWebhookEvent,
            organization=self.organization,
            processing_status=IncomingWebhookProcessingStatus.PROCESSED,
            created=now - datetime.timedelta(hours=1),
            _quantity=3,
        )

        alert = self.service.generate_webhook_failure_alert(hours_back=24)

        assert alert is not None
        assert alert["alert_type"] == "high_webhook_failure_rate"
        assert "failure rate" in alert["message"].lower()
        assert alert["failure_rate"] == 70.0

    def test_generate_webhook_failure_alert_no_recent_events(self) -> None:
        """Test failure alert when no recent events exist."""
        # Create some old events
        old_time = timezone.now() - datetime.timedelta(hours=25)
        baker.make(
            CalendarWebhookEvent,
            organization=self.organization,
            processing_status=IncomingWebhookProcessingStatus.PROCESSED,
            created=old_time,
            _quantity=5,
        )

        alert = self.service.generate_webhook_failure_alert(hours_back=24)

        assert alert is None

    @patch(
        "calendar_integration.services.webhook_analytics_service.CalendarWebhookSubscription.objects"
    )
    def test_get_subscription_health_report(self, mock_subscription_objects: Mock) -> None:
        """Test getting subscription health report."""
        # Mock the queryset chain
        mock_qs = Mock()
        mock_subscription_objects.filter.return_value = mock_qs

        # Mock counts
        mock_qs.count.return_value = 6  # total subscriptions
        mock_qs.filter.return_value.count.side_effect = [
            4,
            0,
            1,
            2,
        ]  # active, expired, expiring_soon, stale

        # Mock delivery stats by patching the method
        with patch.object(self.service, "get_webhook_delivery_stats") as mock_delivery_stats:
            mock_delivery_stats.return_value = {
                "total_events": 10,
                "success_rate": 80.0,
                "failure_rate": 20.0,
            }

            report = self.service.get_subscription_health_report()

        assert report["total_subscriptions"] == 6
        assert report["active_subscriptions"] == 4
        assert report["expired_subscriptions"] == 0
        assert report["expiring_soon_subscriptions"] == 1
        assert "delivery_stats" in report
        assert report["delivery_stats"]["success_rate"] == 80.0

    @patch("calendar_integration.services.webhook_analytics_service.CalendarWebhookEvent.objects")
    def test_cleanup_old_webhook_events_with_filter(self, mock_qs: Mock) -> None:
        """Test cleanup old webhook events with filtering by date."""
        mock_filter_result = Mock()
        mock_filter_result.delete.return_value = (
            150,
            {"calendar_integration.CalendarWebhookEvent": 150},
        )
        mock_qs.filter.return_value = mock_filter_result

        result = self.service.cleanup_old_webhook_events(days_to_keep=30)

        assert result == 150
        # Verify the filter was called with proper arguments
        mock_qs.filter.assert_called_once()
        mock_filter_result.delete.assert_called_once()

    @patch("calendar_integration.services.webhook_analytics_service.CalendarWebhookEvent.objects")
    def test_cleanup_old_webhook_events_actual_delete(self, mock_qs: Mock) -> None:
        """Test cleanup old webhook events with actual deletion."""
        mock_filter_result = Mock()
        mock_filter_result.delete.return_value = (
            100,
            {"calendar_integration.CalendarWebhookEvent": 100},
        )
        mock_qs.filter.return_value = mock_filter_result

        result = self.service.cleanup_old_webhook_events(days_to_keep=7)

        assert result == 100
        mock_filter_result.delete.assert_called_once()
