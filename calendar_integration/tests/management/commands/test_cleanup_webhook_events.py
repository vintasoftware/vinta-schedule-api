"""Tests for cleanup webhook events management command."""

from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import TestCase

import pytest
from model_bakery import baker

from calendar_integration.management.commands.cleanup_webhook_events import Command
from organizations.models import Organization


class TestCleanupWebhookEventsCommand(TestCase):
    """Tests for cleanup webhook events command."""

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
        assert parser.add_argument.call_count == 3
        call_args_list = [call[0][0] for call in parser.add_argument.call_args_list]

        assert "--organization-id" in call_args_list
        assert "--days-to-keep" in call_args_list
        assert "--dry-run" in call_args_list

    def test_handle_organization_not_found(self) -> None:
        """Test command with non-existent organization."""
        out = StringIO()
        call_command(
            "cleanup_webhook_events",
            organization_id=99999,
            stdout=out,
        )

        output = out.getvalue()
        assert "Organization 99999 not found" in output

    @patch(
        "calendar_integration.management.commands.cleanup_webhook_events.WebhookAnalyticsService"
    )
    def test_handle_single_organization(self, mock_analytics_service: Mock) -> None:
        """Test command with single organization."""
        mock_service_instance = Mock()
        mock_analytics_service.return_value = mock_service_instance
        mock_service_instance.cleanup_old_webhook_events.return_value = 150

        out = StringIO()
        call_command(
            "cleanup_webhook_events",
            organization_id=self.organization.id,
            days_to_keep=30,
            stdout=out,
        )

        output = out.getvalue()
        assert f"Organization {self.organization.name} (ID: {self.organization.id})" in output
        assert "Deleted 150 webhook events older than 30 days" in output
        assert "Successfully deleted 150 webhook events in total" in output

        # Verify the service was called with correct parameters
        mock_analytics_service.assert_called_once_with(self.organization)
        mock_service_instance.cleanup_old_webhook_events.assert_called_once_with(days_to_keep=30)

    @patch(
        "calendar_integration.management.commands.cleanup_webhook_events.WebhookAnalyticsService"
    )
    def test_handle_all_organizations(self, mock_analytics_service: Mock) -> None:
        """Test command with all organizations."""
        mock_service_instance = Mock()
        mock_analytics_service.return_value = mock_service_instance
        mock_service_instance.cleanup_old_webhook_events.return_value = 75

        out = StringIO()
        call_command("cleanup_webhook_events", stdout=out)

        output = out.getvalue()
        # Should process both organizations
        assert "Organization" in output
        assert "Successfully deleted 150 webhook events in total" in output  # 75 * 2 organizations

    @patch("calendar_integration.models.CalendarWebhookEvent")
    def test_handle_dry_run(self, mock_webhook_event: Mock) -> None:
        """Test command with dry run option."""
        # Mock the count query for dry run
        mock_qs = Mock()
        mock_qs.count.return_value = 200
        mock_webhook_event.objects.filter.return_value = mock_qs

        out = StringIO()
        call_command(
            "cleanup_webhook_events",
            organization_id=self.organization.id,
            dry_run=True,
            stdout=out,
        )

        output = out.getvalue()
        assert "Would delete 200 events older than 30 days" in output
        assert "DRY RUN: Would delete 200 webhook events in total" in output

        # Verify the count query was called
        mock_webhook_event.objects.filter.assert_called_once()

    @patch(
        "calendar_integration.management.commands.cleanup_webhook_events.WebhookAnalyticsService"
    )
    def test_handle_custom_days_to_keep(self, mock_analytics_service: Mock) -> None:
        """Test command with custom days to keep."""
        mock_service_instance = Mock()
        mock_analytics_service.return_value = mock_service_instance
        mock_service_instance.cleanup_old_webhook_events.return_value = 50

        out = StringIO()
        call_command(
            "cleanup_webhook_events",
            organization_id=self.organization.id,
            days_to_keep=7,
            stdout=out,
        )

        output = out.getvalue()
        assert "Deleted 50 webhook events older than 7 days" in output
        assert "Successfully deleted 50 webhook events in total" in output

        # Verify days_to_keep=7 was passed to the service
        mock_service_instance.cleanup_old_webhook_events.assert_called_once_with(days_to_keep=7)

    @patch(
        "calendar_integration.management.commands.cleanup_webhook_events.WebhookAnalyticsService"
    )
    def test_handle_no_events_to_cleanup(self, mock_analytics_service: Mock) -> None:
        """Test command when no events need cleanup."""
        mock_service_instance = Mock()
        mock_analytics_service.return_value = mock_service_instance
        mock_service_instance.cleanup_old_webhook_events.return_value = 0

        out = StringIO()
        call_command(
            "cleanup_webhook_events",
            organization_id=self.organization.id,
            stdout=out,
        )

        output = out.getvalue()
        assert "Deleted 0 webhook events older than 30 days" in output
        assert "Successfully deleted 0 webhook events in total" in output

    @patch(
        "calendar_integration.management.commands.cleanup_webhook_events.WebhookAnalyticsService"
    )
    def test_handle_service_exception(self, mock_analytics_service: Mock) -> None:
        """Test command when service raises an exception."""
        mock_service_instance = Mock()
        mock_analytics_service.return_value = mock_service_instance
        mock_service_instance.cleanup_old_webhook_events.side_effect = Exception("Database error")

        out = StringIO()
        # The exception should propagate up - the command doesn't handle it
        with pytest.raises(Exception, match="Database error"):
            call_command(
                "cleanup_webhook_events",
                organization_id=self.organization.id,
                stdout=out,
            )

    @patch(
        "calendar_integration.management.commands.cleanup_webhook_events.WebhookAnalyticsService"
    )
    def test_handle_with_multiple_organizations_mixed_results(
        self, mock_analytics_service: Mock
    ) -> None:
        """Test command with multiple organizations having different results."""
        # Setup different return values for different calls
        mock_service_instance = Mock()
        mock_analytics_service.return_value = mock_service_instance

        # First organization returns 100, second returns 0
        mock_service_instance.cleanup_old_webhook_events.side_effect = [100, 0]

        out = StringIO()
        call_command("cleanup_webhook_events", stdout=out)

        output = out.getvalue()
        assert "Successfully deleted 100 webhook events in total" in output
        assert "Deleted 100 webhook events older than 30 days" in output
        assert "Deleted 0 webhook events older than 30 days" in output
