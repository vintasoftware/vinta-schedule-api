"""Tests for refresh webhook subscriptions management command."""

import datetime
from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from model_bakery import baker

from calendar_integration.management.commands.refresh_webhook_subscriptions import Command
from organizations.models import Organization


class TestRefreshWebhookSubscriptionsCommand(TestCase):
    """Tests for refresh webhook subscriptions command."""

    def setUp(self) -> None:
        """Set up test data."""
        self.organization = baker.make(Organization)
        self.organization2 = baker.make(Organization)
        self.command = Command()

    def test_add_arguments(self) -> None:
        """Test that command arguments are properly added."""
        parser = Mock()
        self.command.add_arguments(parser)

        # Verify all expected arguments were added
        assert parser.add_argument.call_count == 4
        call_args_list = [call[0][0] for call in parser.add_argument.call_args_list]

        assert "--organization-id" in call_args_list
        assert "--hours-before-expiry" in call_args_list
        assert "--provider" in call_args_list
        assert "--dry-run" in call_args_list

    def test_handle_organization_not_found(self) -> None:
        """Test command with non-existent organization."""
        out = StringIO()
        call_command(
            "refresh_webhook_subscriptions",
            organization_id=99999,
            stdout=out,
        )

        output = out.getvalue()
        assert "Organization 99999 not found" in output

    @patch(
        "calendar_integration.management.commands.refresh_webhook_subscriptions.CalendarWebhookSubscription.objects"
    )
    def test_handle_single_organization_no_subscriptions(self, mock_qs: Mock) -> None:
        """Test command with single organization that has no expiring subscriptions."""
        # Mock the complete queryset chain
        mock_final_qs = Mock()
        mock_final_qs.select_related.return_value = []

        mock_filtered_qs = Mock()
        mock_filtered_qs.filter.return_value = mock_final_qs

        mock_qs.filter.return_value = mock_filtered_qs

        out = StringIO()
        call_command(
            "refresh_webhook_subscriptions",
            organization_id=self.organization.id,
            stdout=out,
        )

        output = out.getvalue()
        assert "No webhook subscriptions need refreshing" in output

    @patch("di_core.containers.container")
    @patch(
        "calendar_integration.management.commands.refresh_webhook_subscriptions.CalendarWebhookSubscription.objects"
    )
    def test_handle_single_organization_with_subscriptions(
        self, mock_qs: Mock, mock_container: Mock
    ) -> None:
        """Test command with single organization that has expiring subscriptions."""
        # Create mock subscriptions
        mock_subscription1 = Mock()
        mock_subscription1.id = 1
        mock_subscription1.provider = "google"
        mock_subscription1.expires_at = timezone.now() + datetime.timedelta(hours=12)
        mock_subscription1.calendar.name = "Test Calendar 1"
        mock_subscription1.organization = self.organization

        mock_subscription2 = Mock()
        mock_subscription2.id = 2
        mock_subscription2.provider = "microsoft"
        mock_subscription2.expires_at = timezone.now() + datetime.timedelta(hours=6)
        mock_subscription2.calendar.name = "Test Calendar 2"
        mock_subscription2.organization = self.organization

        subscriptions = [mock_subscription1, mock_subscription2]

        # Mock the complete queryset chain
        mock_final_qs = Mock()
        mock_final_qs.select_related.return_value = subscriptions

        mock_filtered_qs = Mock()
        mock_filtered_qs.filter.return_value = mock_final_qs

        mock_qs.filter.return_value = mock_filtered_qs

        # Mock DI container and calendar service
        mock_calendar_service = Mock()
        mock_calendar_service.refresh_webhook_subscription.side_effect = [
            mock_subscription1,  # First refresh succeeds
            None,  # Second refresh fails
        ]
        mock_container.calendar_service.return_value = mock_calendar_service

        out = StringIO()
        call_command(
            "refresh_webhook_subscriptions",
            organization_id=self.organization.id,
            stdout=out,
        )

        output = out.getvalue()
        assert "Found 2 webhook subscriptions expiring within 24 hours" in output
        assert "Successfully refreshed 1 webhook subscriptions" in output
        assert "Failed to refresh 1 webhook subscriptions" in output

    @patch("di_core.containers.container")
    @patch(
        "calendar_integration.management.commands.refresh_webhook_subscriptions.CalendarWebhookSubscription.objects"
    )
    def test_handle_dry_run(self, mock_qs: Mock, mock_container: Mock) -> None:
        """Test command with dry run option."""
        # Create mock subscription
        mock_subscription = Mock()
        mock_subscription.id = 1
        mock_subscription.provider = "google"
        mock_subscription.expires_at = timezone.now() + datetime.timedelta(hours=12)
        mock_subscription.calendar.name = "Test Calendar"
        mock_subscription.organization = self.organization

        subscriptions = [mock_subscription]

        # Mock the complete queryset chain
        mock_final_qs = Mock()
        mock_final_qs.select_related.return_value = subscriptions

        mock_filtered_qs = Mock()
        mock_filtered_qs.filter.return_value = mock_final_qs

        mock_qs.filter.return_value = mock_filtered_qs

        # Mock DI container
        mock_calendar_service = Mock()
        mock_container.calendar_service.return_value = mock_calendar_service

        out = StringIO()
        call_command(
            "refresh_webhook_subscriptions",
            organization_id=self.organization.id,
            dry_run=True,
            stdout=out,
        )

        output = out.getvalue()
        assert "Found 1 webhook subscriptions expiring within 24 hours" in output
        assert "DRY RUN: Would refresh 1 subscriptions" in output
        assert "Test Calendar" in output
        # Verify refresh_subscription was not called in dry run
        mock_subscription.refresh_subscription.assert_not_called()

    @patch(
        "calendar_integration.management.commands.refresh_webhook_subscriptions.CalendarWebhookSubscription.objects"
    )
    def test_handle_all_organizations(self, mock_qs: Mock) -> None:
        """Test command with all organizations."""
        # Mock the complete queryset chain for no results
        mock_final_qs = Mock()
        mock_final_qs.select_related.return_value = []

        mock_qs.filter.return_value = mock_final_qs

        out = StringIO()
        call_command("refresh_webhook_subscriptions", stdout=out)

        output = out.getvalue()
        assert "No webhook subscriptions need refreshing" in output

    @patch(
        "calendar_integration.management.commands.refresh_webhook_subscriptions.CalendarWebhookSubscription.objects"
    )
    def test_handle_with_provider_filter(self, mock_qs: Mock) -> None:
        """Test command with specific provider filter."""
        # Mock the complete queryset chain
        mock_final_qs = Mock()
        mock_final_qs.select_related.return_value = []

        mock_provider_filtered_qs = Mock()
        mock_provider_filtered_qs.filter.return_value = mock_final_qs

        mock_org_filtered_qs = Mock()
        mock_org_filtered_qs.filter.return_value = mock_provider_filtered_qs

        mock_qs.filter.return_value = mock_org_filtered_qs

        out = StringIO()
        call_command(
            "refresh_webhook_subscriptions",
            organization_id=self.organization.id,
            provider="google",
            stdout=out,
        )

        output = out.getvalue()
        assert "No webhook subscriptions need refreshing" in output

        # Verify the initial filter was called (we can't easily verify the provider filter due to chaining)
        mock_qs.filter.assert_called()

    @patch(
        "calendar_integration.management.commands.refresh_webhook_subscriptions.CalendarWebhookSubscription.objects"
    )
    def test_handle_with_custom_hours_before_expiry(self, mock_qs: Mock) -> None:
        """Test command with custom hours before expiry."""
        # Mock the complete queryset chain
        mock_final_qs = Mock()
        mock_final_qs.select_related.return_value = []

        mock_filtered_qs = Mock()
        mock_filtered_qs.filter.return_value = mock_final_qs

        mock_qs.filter.return_value = mock_filtered_qs

        out = StringIO()
        call_command(
            "refresh_webhook_subscriptions",
            organization_id=self.organization.id,
            hours_before_expiry=48,
            stdout=out,
        )

        output = out.getvalue()
        assert "No webhook subscriptions need refreshing" in output

    @patch("di_core.containers.container")
    @patch(
        "calendar_integration.management.commands.refresh_webhook_subscriptions.CalendarWebhookSubscription.objects"
    )
    def test_handle_refresh_exception(self, mock_qs: Mock, mock_container: Mock) -> None:
        """Test command when refresh_subscription raises an exception."""
        # Create mock subscription that raises exception
        mock_subscription = Mock()
        mock_subscription.id = 1
        mock_subscription.provider = "google"
        mock_subscription.expires_at = timezone.now() + datetime.timedelta(hours=12)
        mock_subscription.calendar.name = "Test Calendar"
        mock_subscription.organization = self.organization

        subscriptions = [mock_subscription]

        # Mock the complete queryset chain
        mock_final_qs = Mock()
        mock_final_qs.select_related.return_value = subscriptions

        mock_filtered_qs = Mock()
        mock_filtered_qs.filter.return_value = mock_final_qs

        mock_qs.filter.return_value = mock_filtered_qs

        # Mock DI container and calendar service that raises exception
        mock_calendar_service = Mock()
        mock_calendar_service.refresh_webhook_subscription.side_effect = Exception("Network error")
        mock_container.calendar_service.return_value = mock_calendar_service

        out = StringIO()
        call_command(
            "refresh_webhook_subscriptions",
            organization_id=self.organization.id,
            stdout=out,
        )

        output = out.getvalue()
        assert "Found 1 webhook subscriptions expiring within 24 hours" in output
        assert "Successfully refreshed 0 webhook subscriptions" in output
        assert "Failed to refresh 1 webhook subscriptions" in output
        assert "Network error" in output
        assert "Failed to refresh: Network error" in output
        assert "Network error" in output
