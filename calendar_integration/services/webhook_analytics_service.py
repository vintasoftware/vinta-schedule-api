"""Webhook analytics and monitoring service for tracking webhook performance."""

import datetime
import logging
from typing import Any

from django.db.models import Avg, Count, F, Q, QuerySet

from calendar_integration.constants import IncomingWebhookProcessingStatus
from calendar_integration.models import CalendarWebhookEvent, CalendarWebhookSubscription
from organizations.models import Organization


class WebhookAnalyticsService:
    """Service for webhook analytics and monitoring operations."""

    def __init__(self, organization: Organization):
        """Initialize service with organization context.

        Args:
            organization: Organization to scope analytics to
        """
        self.organization = organization

    def get_webhook_delivery_stats(self, hours_back: int = 24) -> dict[str, int | float]:
        """Get webhook delivery success rates and statistics.

        Args:
            hours_back: Number of hours to look back for statistics

        Returns:
            Dictionary with delivery statistics
        """
        start_time = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(hours=hours_back)

        events_qs = CalendarWebhookEvent.objects.filter(
            organization=self.organization, created__gte=start_time
        )

        total_events = events_qs.count()
        if total_events == 0:
            return {
                "total_events": 0,
                "successful_events": 0,
                "failed_events": 0,
                "ignored_events": 0,
                "pending_events": 0,
                "success_rate": 100.0,
                "failure_rate": 0.0,
                "average_processing_time_seconds": 0.0,
            }

        # Count events by status
        status_counts = events_qs.aggregate(
            successful=Count(
                "id",
                filter=Q(processing_status=IncomingWebhookProcessingStatus.PROCESSED),
            ),
            failed=Count("id", filter=Q(processing_status=IncomingWebhookProcessingStatus.FAILED)),
            ignored=Count(
                "id", filter=Q(processing_status=IncomingWebhookProcessingStatus.IGNORED)
            ),
            pending=Count(
                "id", filter=Q(processing_status=IncomingWebhookProcessingStatus.PENDING)
            ),
        )

        successful_events = status_counts["successful"] or 0
        failed_events = status_counts["failed"] or 0
        ignored_events = status_counts["ignored"] or 0
        pending_events = status_counts["pending"] or 0

        # Calculate rates
        success_rate = (successful_events / total_events) * 100 if total_events > 0 else 100.0
        failure_rate = (failed_events / total_events) * 100 if total_events > 0 else 0.0

        # Calculate average processing time for processed events
        processed_events = events_qs.filter(
            processing_status=IncomingWebhookProcessingStatus.PROCESSED,
            processed_at__isnull=False,
        ).annotate(processing_time=F("processed_at") - F("created"))

        avg_processing_time = processed_events.aggregate(avg_time=Avg("processing_time"))[
            "avg_time"
        ]

        avg_processing_seconds = avg_processing_time.total_seconds() if avg_processing_time else 0.0

        return {
            "total_events": total_events,
            "successful_events": successful_events,
            "failed_events": failed_events,
            "ignored_events": ignored_events,
            "pending_events": pending_events,
            "success_rate": success_rate,
            "failure_rate": failure_rate,
            "average_processing_time_seconds": avg_processing_seconds,
        }

    def get_webhook_latency_metrics(self, hours_back: int = 24) -> dict[str, float]:
        """Get webhook processing latency metrics.

        Args:
            hours_back: Number of hours to look back for metrics

        Returns:
            Dictionary with latency metrics in seconds
        """
        start_time = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(hours=hours_back)

        processed_events = (
            CalendarWebhookEvent.objects.filter(
                organization=self.organization,
                created__gte=start_time,
                processing_status=IncomingWebhookProcessingStatus.PROCESSED,
                processed_at__isnull=False,
            )
            .annotate(processing_time=F("processed_at") - F("created"))
            .values_list("processing_time", flat=True)
        )

        if not processed_events:
            return {
                "min_latency": 0.0,
                "max_latency": 0.0,
                "avg_latency": 0.0,
                "p50_latency": 0.0,
                "p95_latency": 0.0,
                "p99_latency": 0.0,
            }

        # Convert to seconds
        latencies = [pt.total_seconds() for pt in processed_events]
        latencies.sort()

        count = len(latencies)
        p50_index = int(count * 0.5)
        p95_index = int(count * 0.95)
        p99_index = int(count * 0.99)

        return {
            "min_latency": min(latencies),
            "max_latency": max(latencies),
            "avg_latency": sum(latencies) / count,
            "p50_latency": latencies[p50_index] if p50_index < count else latencies[-1],
            "p95_latency": latencies[p95_index] if p95_index < count else latencies[-1],
            "p99_latency": latencies[p99_index] if p99_index < count else latencies[-1],
        }

    def get_failed_webhooks(
        self, hours_back: int = 24, limit: int = 50
    ) -> QuerySet[CalendarWebhookEvent]:
        """Get recent failed webhook events for analysis.

        Args:
            hours_back: Number of hours to look back
            limit: Maximum number of events to return

        Returns:
            QuerySet of failed CalendarWebhookEvent objects
        """
        start_time = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(hours=hours_back)

        return (
            CalendarWebhookEvent.objects.filter(
                organization=self.organization,
                created__gte=start_time,
                processing_status=IncomingWebhookProcessingStatus.FAILED,
            )
            .select_related("subscription", "calendar_sync")
            .order_by("-created")[:limit]
        )

    def generate_webhook_failure_alert(
        self, failure_threshold_percent: float = 20.0, hours_back: int = 1
    ) -> dict[str, Any] | None:
        """Generate alert if webhook failure rate exceeds threshold.

        Args:
            failure_threshold_percent: Failure rate threshold (0-100)
            hours_back: Hours to analyze

        Returns:
            Alert dictionary if threshold exceeded, None otherwise
        """
        stats = self.get_webhook_delivery_stats(hours_back=hours_back)

        if stats["total_events"] < 5:  # Need minimum sample size
            return None

        if stats["failure_rate"] > failure_threshold_percent:
            return {
                "alert_type": "high_webhook_failure_rate",
                "organization_id": self.organization.id,
                "failure_rate": stats["failure_rate"],
                "threshold": failure_threshold_percent,
                "total_events": stats["total_events"],
                "failed_events": stats["failed_events"],
                "time_window_hours": hours_back,
                "timestamp": datetime.datetime.now(tz=datetime.UTC),
                "message": (
                    f"Webhook failure rate ({stats['failure_rate']:.1f}%) "
                    f"exceeds threshold ({failure_threshold_percent}%) "
                    f"for organization {self.organization.id}"
                ),
            }

        return None

    def get_subscription_health_report(self) -> dict[str, Any]:
        """Get comprehensive health report for webhook subscriptions.

        Returns:
            Dictionary with subscription health metrics
        """
        now = datetime.datetime.now(tz=datetime.UTC)
        expiring_threshold = now + datetime.timedelta(hours=24)

        subscriptions_qs = CalendarWebhookSubscription.objects.filter(
            organization=self.organization
        )

        # Basic counts
        total_subscriptions = subscriptions_qs.count()
        active_subscriptions = subscriptions_qs.filter(is_active=True).count()
        expired_subscriptions = subscriptions_qs.filter(is_active=True, expires_at__lt=now).count()
        expiring_soon = subscriptions_qs.filter(
            is_active=True, expires_at__gte=now, expires_at__lte=expiring_threshold
        ).count()

        # Subscriptions without recent activity
        seven_days_ago = now - datetime.timedelta(days=7)
        stale_subscriptions = (
            subscriptions_qs.filter(is_active=True)
            .filter(
                Q(last_notification_at__isnull=True) | Q(last_notification_at__lt=seven_days_ago)
            )
            .count()
        )

        # Get delivery stats for active subscriptions
        delivery_stats = self.get_webhook_delivery_stats(hours_back=24)

        return {
            "total_subscriptions": total_subscriptions,
            "active_subscriptions": active_subscriptions,
            "expired_subscriptions": expired_subscriptions,
            "expiring_soon_subscriptions": expiring_soon,
            "stale_subscriptions": stale_subscriptions,
            "delivery_stats": delivery_stats,
            "report_generated_at": now,
        }

    def cleanup_old_webhook_events(self, days_to_keep: int = 30) -> int:
        """Clean up old webhook events to manage database size.

        Args:
            days_to_keep: Number of days of events to keep

        Returns:
            Number of events deleted
        """
        cutoff_date = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(days=days_to_keep)

        deleted_count, _ = CalendarWebhookEvent.objects.filter(
            organization=self.organization, created__lt=cutoff_date
        ).delete()

        if deleted_count:
            logging.getLogger(__name__).info(
                "Cleaned up %d old webhook events for organization %d",
                deleted_count,
                self.organization.id,
            )

        return deleted_count
