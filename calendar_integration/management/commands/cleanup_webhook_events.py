"""Django management command for cleaning up old webhook events."""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from calendar_integration.services.webhook_analytics_service import WebhookAnalyticsService
from organizations.models import Organization


class Command(BaseCommand):
    """Management command for cleaning up old webhook events."""

    help_text = "Clean up old webhook events to manage database size"

    def add_arguments(self, parser: CommandParser) -> None:
        """Add command arguments."""
        parser.add_argument(
            "--organization-id",
            type=int,
            help="Organization ID to clean up (optional, cleans all if not specified)",
        )
        parser.add_argument(
            "--days-to-keep",
            type=int,
            default=30,
            help="Number of days of webhook events to keep (default: 30)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be deleted without actually deleting",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Execute the cleanup command."""
        organization_id = options.get("organization_id")
        days_to_keep = options["days_to_keep"]
        dry_run = options["dry_run"]

        if organization_id:
            try:
                organization = Organization.objects.get(id=organization_id)
                organizations = [organization]
            except Organization.DoesNotExist:
                self.stdout.write(self.style.ERROR(f"Organization {organization_id} not found"))
                return
        else:
            organizations = list(Organization.objects.all())

        total_deleted = 0

        for org in organizations:
            analytics_service = WebhookAnalyticsService(org)

            if dry_run:
                # Count what would be deleted
                import datetime

                from calendar_integration.models import CalendarWebhookEvent

                cutoff_date = datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(
                    days=days_to_keep
                )

                count_to_delete = CalendarWebhookEvent.objects.filter(
                    organization=org, created__lt=cutoff_date
                ).count()

                self.stdout.write(
                    f"Organization {org.name} (ID: {org.id}): "
                    f"Would delete {count_to_delete} events older than {days_to_keep} days"
                )
                total_deleted += count_to_delete
            else:
                deleted_count = analytics_service.cleanup_old_webhook_events(
                    days_to_keep=days_to_keep
                )
                total_deleted += deleted_count

                self.stdout.write(
                    f"Organization {org.name} (ID: {org.id}): "
                    f"Deleted {deleted_count} webhook events older than {days_to_keep} days"
                )

        if dry_run:
            self.stdout.write(
                self.style.WARNING(f"DRY RUN: Would delete {total_deleted} webhook events in total")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f"Successfully deleted {total_deleted} webhook events in total")
            )
