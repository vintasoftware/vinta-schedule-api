"""Django admin interface for calendar integration webhook management."""

import datetime
from typing import ClassVar

from django.contrib import admin
from django.db.models import Count
from django.http import HttpRequest
from django.utils.html import format_html

from calendar_integration.constants import IncomingWebhookProcessingStatus
from calendar_integration.models import CalendarWebhookEvent, CalendarWebhookSubscription


class CalendarWebhookEventInline(admin.TabularInline):
    """Inline admin for webhook events related to subscriptions."""

    model = CalendarWebhookEvent
    fields = (
        "created",
        "event_type",
        "processing_status",
        "external_event_id",
        "sync_triggered_display",
    )
    readonly_fields = (
        "created",
        "event_type",
        "processing_status",
        "external_event_id",
        "sync_triggered_display",
    )
    extra = 0
    max_num = 10  # Limit to recent events

    @admin.display(description="Sync Triggered")
    def sync_triggered_display(self, obj: CalendarWebhookEvent) -> str:
        """Display whether sync was triggered with a colored indicator."""
        if obj.sync_triggered:
            return format_html('<span style="color: green;">✓ Yes</span>')
        return format_html('<span style="color: red;">✗ No</span>')


@admin.register(CalendarWebhookSubscription)
class CalendarWebhookSubscriptionAdmin(admin.ModelAdmin):
    """Admin interface for calendar webhook subscriptions."""

    list_display = (
        "id",
        "calendar",
        "provider",
        "is_active",
        "expires_at",
        "last_notification_at",
        "events_count",
        "health_status",
        "created",
    )
    list_filter = (
        "provider",
        "is_active",
        "created",
        "expires_at",
        "last_notification_at",
    )
    search_fields = (
        "calendar__name",
        "external_subscription_id",
        "external_resource_id",
        "callback_url",
    )
    readonly_fields = (
        "organization",
        "external_subscription_id",
        "external_resource_id",
        "channel_id",
        "resource_uri",
        "created",
        "modified",
        "events_count",
        "health_status",
    )
    fields = (
        "organization",
        "calendar",
        "provider",
        "external_subscription_id",
        "external_resource_id",
        "callback_url",
        "channel_id",
        "resource_uri",
        "verification_token",
        "expires_at",
        "is_active",
        "last_notification_at",
        "events_count",
        "health_status",
        "created",
        "modified",
    )
    inlines: ClassVar = [CalendarWebhookEventInline]

    def get_queryset(self, request: HttpRequest):
        """Optimize queryset with annotations for event counts."""
        qs = super().get_queryset(request)
        return qs.select_related("calendar", "organization").annotate(
            events_count_annotation=Count("webhook_events")
        )

    @admin.display(description="Events Count")
    def events_count(self, obj: CalendarWebhookSubscription) -> int:
        """Display count of webhook events for this subscription."""
        if hasattr(obj, "events_count_annotation"):
            return obj.events_count_annotation
        return obj.webhook_events.count()

    @admin.display(description="Health Status")
    def health_status(self, obj: CalendarWebhookSubscription) -> str:
        """Display health status with colored indicators."""
        now = datetime.datetime.now(tz=datetime.UTC)

        # Check if expired
        if obj.expires_at and obj.expires_at < now:
            return format_html('<span style="color: red;">⚠ Expired</span>')

        # Check if expiring soon (within 24 hours)
        if obj.expires_at and obj.expires_at < (now + datetime.timedelta(hours=24)):
            return format_html('<span style="color: orange;">⚠ Expiring Soon</span>')

        # Check if no recent activity (no notifications in 7 days)
        seven_days_ago = now - datetime.timedelta(days=7)
        if not obj.last_notification_at or obj.last_notification_at < seven_days_ago:
            return format_html('<span style="color: orange;">⚠ Stale</span>')

        # Check for recent failed events
        recent_failed_events = obj.webhook_events.filter(
            created__gte=now - datetime.timedelta(hours=24),
            processing_status=IncomingWebhookProcessingStatus.FAILED,
        ).count()

        if recent_failed_events > 0:
            return format_html(
                '<span style="color: red;">⚠ {} Failed (24h)</span>', recent_failed_events
            )

        return format_html('<span style="color: green;">✓ Healthy</span>')

    actions: ClassVar = ["renew_subscriptions", "deactivate_subscriptions"]

    @admin.action(description="Renew selected subscriptions")
    def renew_subscriptions(self, request: HttpRequest, queryset):
        """Admin action to renew selected subscriptions."""
        count = 0
        now = datetime.datetime.now(tz=datetime.UTC)

        for subscription in queryset.filter(is_active=True):
            if subscription.provider == "google":
                subscription.expires_at = now + datetime.timedelta(days=7)
            elif subscription.provider == "microsoft":
                subscription.expires_at = now + datetime.timedelta(minutes=4230)
            else:
                subscription.expires_at = now + datetime.timedelta(days=1)
            subscription.save()
            count += 1

        self.message_user(request, f"Renewed {count} webhook subscriptions.")

    @admin.action(description="Deactivate selected subscriptions")
    def deactivate_subscriptions(self, request: HttpRequest, queryset):
        """Admin action to deactivate selected subscriptions."""
        count = queryset.update(is_active=False)
        self.message_user(request, f"Deactivated {count} webhook subscriptions.")


@admin.register(CalendarWebhookEvent)
class CalendarWebhookEventAdmin(admin.ModelAdmin):
    """Admin interface for calendar webhook events."""

    list_display = (
        "id",
        "provider",
        "event_type",
        "processing_status",
        "sync_triggered_display",
        "subscription_info",
        "created",
        "processed_at",
    )
    list_filter = (
        "provider",
        "event_type",
        "processing_status",
        "created",
        "processed_at",
    )
    search_fields = (
        "external_calendar_id",
        "external_event_id",
        "subscription__calendar__name",
    )
    readonly_fields = (
        "organization",
        "subscription",
        "provider",
        "event_type",
        "external_calendar_id",
        "external_event_id",
        "raw_payload",
        "headers",
        "processed_at",
        "processing_status",
        "calendar_sync",
        "created",
        "modified",
        "sync_triggered_display",
        "error_message_display",
    )
    fields = (
        "organization",
        "subscription",
        "provider",
        "event_type",
        "external_calendar_id",
        "external_event_id",
        "processing_status",
        "processed_at",
        "calendar_sync",
        "sync_triggered_display",
        "error_message_display",
        "created",
        "modified",
        "raw_payload",
        "headers",
    )

    def get_queryset(self, request: HttpRequest):
        """Optimize queryset with related objects."""
        qs = super().get_queryset(request)
        return qs.select_related(
            "subscription",
            "subscription__calendar",
            "calendar_sync",
            "organization",
        )

    @admin.display(description="Sync Triggered")
    def sync_triggered_display(self, obj: CalendarWebhookEvent) -> str:
        """Display whether sync was triggered with a colored indicator."""
        if obj.sync_triggered:
            sync_link = (
                f'<a href="/admin/calendar_integration/calendarsync/{obj.calendar_sync.id}/change/">'
                f"Sync #{obj.calendar_sync.id}"
                "</a>"
            )
            return format_html('<span style="color: green;">✓ {}</span>', sync_link)
        return format_html('<span style="color: red;">✗ No</span>')

    @admin.display(description="Subscription")
    def subscription_info(self, obj: CalendarWebhookEvent) -> str:
        """Display subscription information."""
        if obj.subscription:
            return format_html(
                '<a href="/admin/calendar_integration/calendarwebhooksubscription/{}/change/">'
                "Sub #{} ({})"
                "</a>",
                obj.subscription.id,
                obj.subscription.id,
                obj.subscription.calendar.name[:20],
            )
        return "No Subscription"

    @admin.display(description="Error Message")
    def error_message_display(self, obj: CalendarWebhookEvent) -> str:
        """Display error message if available."""
        error = obj.error_message
        if error:
            return format_html('<span style="color: red;">{}</span>', error[:100])
        return "No errors"

    actions: ClassVar = ["mark_as_processed", "reprocess_events"]

    @admin.action(description="Mark selected events as processed")
    def mark_as_processed(self, request: HttpRequest, queryset):
        """Admin action to mark events as processed."""
        count = queryset.update(
            processing_status=IncomingWebhookProcessingStatus.PROCESSED,
            processed_at=datetime.datetime.now(tz=datetime.UTC),
        )
        self.message_user(request, f"Marked {count} events as processed.")

    @admin.action(description="Reset selected events for reprocessing")
    def reprocess_events(self, request: HttpRequest, queryset):
        """Admin action to reset events for reprocessing."""
        count = queryset.update(
            processing_status=IncomingWebhookProcessingStatus.PENDING,
            processed_at=None,
        )
        self.message_user(request, f"Reset {count} events for reprocessing.")


class WebhookHealthDashboard:
    """Dashboard widget for webhook health overview."""

    def __init__(self):
        self.name = "Webhook Health Dashboard"

    def render(self, request: HttpRequest) -> str:
        """Render webhook health dashboard."""
        now = datetime.datetime.now(tz=datetime.UTC)
        twenty_four_hours_ago = now - datetime.timedelta(hours=24)

        # Get statistics
        total_subscriptions = CalendarWebhookSubscription.objects.count()
        active_subscriptions = CalendarWebhookSubscription.objects.filter(is_active=True).count()
        expired_subscriptions = CalendarWebhookSubscription.objects.filter(
            is_active=True, expires_at__lt=now
        ).count()

        recent_events = CalendarWebhookEvent.objects.filter(
            created__gte=twenty_four_hours_ago
        ).count()
        failed_events = CalendarWebhookEvent.objects.filter(
            created__gte=twenty_four_hours_ago,
            processing_status=IncomingWebhookProcessingStatus.FAILED,
        ).count()

        success_rate = (
            ((recent_events - failed_events) / recent_events) * 100 if recent_events > 0 else 100.0
        )

        return format_html(
            """
            <div style="background: #f8f9fa; border: 1px solid #dee2e6; border-radius: 5px; padding: 15px; margin: 10px 0;">
                <h3 style="margin-top: 0;">Webhook System Health</h3>
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px;">
                    <div>
                        <strong>Subscriptions:</strong><br>
                        Total: {total}<br>
                        Active: {active}<br>
                        Expired: <span style="color: {expired_color};">{expired}</span>
                    </div>
                    <div>
                        <strong>Events (24h):</strong><br>
                        Total: {recent}<br>
                        Failed: <span style="color: {failed_color};">{failed}</span><br>
                        Success Rate: <span style="color: {success_color};">{success:.1f}%</span>
                    </div>
                </div>
            </div>
            """,
            total=total_subscriptions,
            active=active_subscriptions,
            expired=expired_subscriptions,
            expired_color="red" if expired_subscriptions > 0 else "green",
            recent=recent_events,
            failed=failed_events,
            failed_color="red" if failed_events > 0 else "green",
            success=success_rate,
            success_color="green"
            if success_rate >= 95
            else "orange"
            if success_rate >= 80
            else "red",
        )
