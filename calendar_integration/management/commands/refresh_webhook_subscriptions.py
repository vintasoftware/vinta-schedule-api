"""Django management command for refreshing webhook subscriptions."""

import datetime
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from calendar_integration.models import CalendarWebhookSubscription
from organizations.models import Organization


class Command(BaseCommand):
    """Management command for refreshing webhook subscriptions."""

    help = "Refresh expiring webhook subscriptions"  # noqa: A003

    def add_arguments(self, parser: CommandParser) -> None:
        """Add command arguments."""
        parser.add_argument(
            "--organization-id",
            type=int,
            help="Organization ID to refresh subscriptions for (optional, refreshes all if not specified)",
        )
        parser.add_argument(
            "--hours-before-expiry",
            type=int,
            default=24,
            help="Refresh subscriptions expiring within this many hours (default: 24)",
        )
        parser.add_argument(
            "--provider",
            choices=["google", "microsoft"],
            help="Refresh subscriptions for specific provider only",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be refreshed without actually refreshing",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Execute the refresh command."""
        organization_id = options.get("organization_id")
        hours_before_expiry = options["hours_before_expiry"]
        provider = options.get("provider")
        dry_run = options["dry_run"]

        # Calculate expiry threshold
        now = datetime.datetime.now(tz=datetime.UTC)
        expiry_threshold = now + datetime.timedelta(hours=hours_before_expiry)

        # Get subscriptions to refresh
        subscriptions_qs = CalendarWebhookSubscription.objects.filter(
            is_active=True,
            expires_at__lte=expiry_threshold,
            expires_at__gt=now,  # Not yet expired
        )

        if organization_id:
            try:
                organization = Organization.objects.get(id=organization_id)
                subscriptions_qs = subscriptions_qs.filter(organization=organization)
            except Organization.DoesNotExist:
                self.stdout.write(self.style.ERROR(f"Organization {organization_id} not found"))
                return

        if provider:
            subscriptions_qs = subscriptions_qs.filter(provider=provider)

        subscriptions = list(subscriptions_qs.select_related("calendar", "organization"))

        if not subscriptions:
            self.stdout.write(self.style.SUCCESS("No webhook subscriptions need refreshing"))
            return

        self.stdout.write(
            f"Found {len(subscriptions)} webhook subscriptions expiring within "
            f"{hours_before_expiry} hours"
        )

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN MODE - No changes will be made"))

        # Get DI container
        from di_core.containers import container

        if not container:
            raise RuntimeError("DI container is not initialized")

        refreshed_count = 0
        failed_count = 0

        for subscription in subscriptions:
            try:
                # Get calendar service for this organization
                calendar_service = container.calendar_service()
                calendar_service.organization = subscription.organization

                self.stdout.write(
                    f"Processing subscription {subscription.id} for "
                    f"{subscription.organization.name} / {subscription.calendar.name} "
                    f"({subscription.provider}) - expires {subscription.expires_at}"
                )

                if not dry_run:
                    refreshed_subscription = calendar_service.refresh_webhook_subscription(
                        subscription_id=subscription.id
                    )

                    if refreshed_subscription:
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"  ✓ Refreshed - new expiry: {refreshed_subscription.expires_at}"
                            )
                        )
                        refreshed_count += 1
                    else:
                        self.stdout.write(
                            self.style.ERROR("  ✗ Failed to refresh - subscription not found")
                        )
                        failed_count += 1
                else:
                    self.stdout.write("  → Would refresh this subscription")
                    refreshed_count += 1

            except Exception as e:  # noqa: BLE001
                self.stdout.write(self.style.ERROR(f"  ✗ Failed to refresh: {e!s}"))
                failed_count += 1

        if dry_run:
            self.stdout.write(
                self.style.WARNING(f"DRY RUN: Would refresh {refreshed_count} subscriptions")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully refreshed {refreshed_count} webhook subscriptions"
                )
            )

            if failed_count > 0:
                self.stdout.write(
                    self.style.ERROR(f"Failed to refresh {failed_count} webhook subscriptions")
                )
