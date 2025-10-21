"""Django management command for webhook health check and diagnostics."""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from calendar_integration.services.webhook_analytics_service import WebhookAnalyticsService
from organizations.models import Organization


class Command(BaseCommand):
    """Management command for webhook system health checks."""

    help = "Check webhook system health and generate diagnostic reports"  # noqa: A003

    def add_arguments(self, parser: CommandParser) -> None:
        """Add command arguments."""
        parser.add_argument(
            "--organization-id",
            type=int,
            help="Organization ID to check (optional, checks all if not specified)",
        )
        parser.add_argument(
            "--hours-back",
            type=int,
            default=24,
            help="Hours to look back for statistics (default: 24)",
        )
        parser.add_argument(
            "--format",
            choices=["text", "json"],
            default="text",
            help="Output format (default: text)",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Include detailed information in output",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Execute the webhook health check command."""
        organization_id = options.get("organization_id")
        hours_back = options["hours_back"]
        output_format = options["format"]
        verbose = options["verbose"]

        if organization_id:
            try:
                organization = Organization.objects.get(id=organization_id)
                organizations = [organization]
            except Organization.DoesNotExist:
                self.stdout.write(self.style.ERROR(f"Organization {organization_id} not found"))
                return
        else:
            organizations = list(Organization.objects.all())

        total_report = {
            "organizations_checked": len(organizations),
            "total_subscriptions": 0,
            "total_active_subscriptions": 0,
            "total_expired_subscriptions": 0,
            "total_events": 0,
            "total_failed_events": 0,
            "organizations": [],
        }

        for org in organizations:
            org_report = self.check_organization_health(org, hours_back, verbose)
            total_report["organizations"].append(org_report)
            total_report["total_subscriptions"] += org_report["subscriptions"]["total"]
            total_report["total_active_subscriptions"] += org_report["subscriptions"]["active"]
            total_report["total_expired_subscriptions"] += org_report["subscriptions"]["expired"]
            total_report["total_events"] += org_report["events"]["total"]
            total_report["total_failed_events"] += org_report["events"]["failed"]

        if output_format == "json":
            import json

            self.stdout.write(json.dumps(total_report, indent=2, default=str))
        else:
            self.print_text_report(total_report, verbose)

    def check_organization_health(
        self, organization: Organization, hours_back: int, verbose: bool
    ) -> dict[str, Any]:
        """Check webhook health for a specific organization."""
        analytics_service = WebhookAnalyticsService(organization)

        # Get basic health data
        health_data = analytics_service.get_subscription_health_report()
        delivery_stats = health_data["delivery_stats"]

        # Get latency metrics
        latency_metrics = analytics_service.get_webhook_latency_metrics(hours_back=hours_back)

        # Get recent failed webhooks
        failed_webhooks = list(
            analytics_service.get_failed_webhooks(hours_back=hours_back, limit=5)
        )

        # Check for alerts
        alert = analytics_service.generate_webhook_failure_alert(hours_back=hours_back)

        org_report = {
            "organization_id": organization.id,
            "organization_name": organization.name,
            "subscriptions": {
                "total": health_data["total_subscriptions"],
                "active": health_data["active_subscriptions"],
                "expired": health_data["expired_subscriptions"],
                "expiring_soon": health_data["expiring_soon_subscriptions"],
                "stale": health_data["stale_subscriptions"],
            },
            "events": {
                "total": delivery_stats["total_events"],
                "successful": delivery_stats["successful_events"],
                "failed": delivery_stats["failed_events"],
                "ignored": delivery_stats["ignored_events"],
                "pending": delivery_stats["pending_events"],
                "success_rate": delivery_stats["success_rate"],
                "failure_rate": delivery_stats["failure_rate"],
            },
            "latency": latency_metrics,
            "alert": alert,
            "recent_failures": len(failed_webhooks),
        }

        if verbose:
            org_report["failed_webhook_details"] = [
                {
                    "id": fw.id,
                    "provider": fw.provider,
                    "event_type": fw.event_type,
                    "created": fw.created,
                    "error_message": fw.error_message,
                }
                for fw in failed_webhooks
            ]

        return org_report

    def print_text_report(self, report: dict[str, Any], verbose: bool) -> None:
        """Print the health check report in text format."""
        self.stdout.write(self.style.SUCCESS("=== Webhook System Health Report ==="))
        self.stdout.write(f"Organizations Checked: {report['organizations_checked']}")
        self.stdout.write(f"Total Subscriptions: {report['total_subscriptions']}")
        self.stdout.write(f"Total Active: {report['total_active_subscriptions']}")
        self.stdout.write(f"Total Expired: {report['total_expired_subscriptions']}")
        self.stdout.write(f"Total Events: {report['total_events']}")
        self.stdout.write(f"Total Failed Events: {report['total_failed_events']}")

        overall_success_rate = (
            ((report["total_events"] - report["total_failed_events"]) / report["total_events"])
            * 100
            if report["total_events"] > 0
            else 100.0
        )

        self.stdout.write(f"Overall Success Rate: {overall_success_rate:.1f}%")

        self.stdout.write("\n" + "=" * 50)

        for org_data in report["organizations"]:
            self.print_organization_report(org_data, verbose)

    def print_organization_report(self, org_data: dict[str, Any], verbose: bool) -> None:
        """Print health report for a specific organization."""
        self.stdout.write(
            f"\nOrganization: {org_data['organization_name']} (ID: {org_data['organization_id']})"
        )
        self.stdout.write("-" * 40)

        # Subscriptions
        subs = org_data["subscriptions"]
        self.stdout.write(f"Subscriptions - Total: {subs['total']}, Active: {subs['active']}")

        if subs["expired"] > 0:
            self.stdout.write(self.style.ERROR(f"  ⚠ Expired: {subs['expired']}"))

        if subs["expiring_soon"] > 0:
            self.stdout.write(self.style.WARNING(f"  ⚠ Expiring Soon: {subs['expiring_soon']}"))

        if subs["stale"] > 0:
            self.stdout.write(self.style.WARNING(f"  ⚠ Stale: {subs['stale']}"))

        # Events
        events = org_data["events"]
        self.stdout.write(
            f"Events - Total: {events['total']}, Success Rate: {events['success_rate']:.1f}%"
        )

        if events["failed"] > 0:
            self.stdout.write(self.style.ERROR(f"  ⚠ Failed: {events['failed']}"))

        if events["pending"] > 0:
            self.stdout.write(self.style.WARNING(f"  ⚠ Pending: {events['pending']}"))

        # Latency
        latency = org_data["latency"]
        if events["total"] > 0:
            self.stdout.write(
                f"Latency - Avg: {latency['avg_latency']:.2f}s, P95: {latency['p95_latency']:.2f}s"
            )

        # Alert
        if org_data["alert"]:
            self.stdout.write(self.style.ERROR(f"  🚨 ALERT: {org_data['alert']['message']}"))

        # Verbose details
        if verbose and "failed_webhook_details" in org_data:
            self.stdout.write("  Recent Failed Webhooks:")
            for fw in org_data["failed_webhook_details"]:
                self.stdout.write(f"    - {fw['provider']} {fw['event_type']} at {fw['created']}")
                if fw["error_message"]:
                    self.stdout.write(f"      Error: {fw['error_message'][:100]}")

        self.stdout.write("")
