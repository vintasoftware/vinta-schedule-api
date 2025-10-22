"""Tests for webhook health check management command."""

import json
from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from model_bakery import baker

from calendar_integration.management.commands.webhook_health_check import Command
from organizations.models import Organization


class TestWebhookHealthCheckCommand(TestCase):
    """Tests for webhook health check command."""

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
        assert "--hours-back" in call_args_list
        assert "--format" in call_args_list
        assert "--verbose" in call_args_list

    @patch("calendar_integration.management.commands.webhook_health_check.WebhookAnalyticsService")
    def test_handle_single_organization(self, mock_analytics_service: Mock) -> None:
        """Test command with single organization."""
        # Mock analytics service responses
        mock_service_instance = Mock()
        mock_analytics_service.return_value = mock_service_instance

        mock_service_instance.get_subscription_health_report.return_value = {
            "total_subscriptions": 5,
            "active_subscriptions": 4,
            "expired_subscriptions": 1,
            "expiring_soon_subscriptions": 0,
            "stale_subscriptions": 0,
            "delivery_stats": {
                "total_events": 100,
                "successful_events": 95,
                "failed_events": 5,
                "ignored_events": 0,
                "pending_events": 0,
                "success_rate": 95.0,
                "failure_rate": 5.0,
            },
        }

        mock_service_instance.get_webhook_latency_metrics.return_value = {
            "avg_latency": 1.5,
            "p95_latency": 2.8,
        }

        mock_service_instance.get_failed_webhooks.return_value = []
        mock_service_instance.generate_webhook_failure_alert.return_value = None

        out = StringIO()
        call_command(
            "webhook_health_check",
            organization_id=self.organization.id,
            stdout=out,
        )

        output = out.getvalue()
        assert "Webhook System Health Report" in output
        assert "Organizations Checked: 1" in output

    def test_handle_organization_not_found(self) -> None:
        """Test command with non-existent organization."""
        out = StringIO()
        call_command(
            "webhook_health_check",
            organization_id=99999,
            stdout=out,
        )

        output = out.getvalue()
        assert "Organization 99999 not found" in output

    @patch("calendar_integration.management.commands.webhook_health_check.WebhookAnalyticsService")
    def test_handle_all_organizations(self, mock_analytics_service: Mock) -> None:
        """Test command with all organizations."""
        mock_service_instance = Mock()
        mock_analytics_service.return_value = mock_service_instance

        mock_service_instance.get_subscription_health_report.return_value = {
            "total_subscriptions": 2,
            "active_subscriptions": 2,
            "expired_subscriptions": 0,
            "expiring_soon_subscriptions": 0,
            "stale_subscriptions": 0,
            "delivery_stats": {
                "total_events": 50,
                "successful_events": 50,
                "failed_events": 0,
                "ignored_events": 0,
                "pending_events": 0,
                "success_rate": 100.0,
                "failure_rate": 0.0,
            },
        }

        mock_service_instance.get_webhook_latency_metrics.return_value = {
            "avg_latency": 1.0,
            "p95_latency": 1.5,
        }

        mock_service_instance.get_failed_webhooks.return_value = []
        mock_service_instance.generate_webhook_failure_alert.return_value = None

        out = StringIO()
        call_command("webhook_health_check", stdout=out)

        output = out.getvalue()
        assert "Organizations Checked: 2" in output

    @patch("calendar_integration.management.commands.webhook_health_check.WebhookAnalyticsService")
    def test_handle_json_output(self, mock_analytics_service: Mock) -> None:
        """Test command with JSON output format."""
        mock_service_instance = Mock()
        mock_analytics_service.return_value = mock_service_instance

        mock_service_instance.get_subscription_health_report.return_value = {
            "total_subscriptions": 1,
            "active_subscriptions": 1,
            "expired_subscriptions": 0,
            "expiring_soon_subscriptions": 0,
            "stale_subscriptions": 0,
            "delivery_stats": {
                "total_events": 10,
                "successful_events": 10,
                "failed_events": 0,
                "ignored_events": 0,
                "pending_events": 0,
                "success_rate": 100.0,
                "failure_rate": 0.0,
            },
        }

        mock_service_instance.get_webhook_latency_metrics.return_value = {
            "avg_latency": 0.5,
            "p95_latency": 1.0,
        }

        mock_service_instance.get_failed_webhooks.return_value = []
        mock_service_instance.generate_webhook_failure_alert.return_value = None

        out = StringIO()
        call_command(
            "webhook_health_check",
            organization_id=self.organization.id,
            format="json",
            stdout=out,
        )

        output = out.getvalue()
        data = json.loads(output)

        assert data["organizations_checked"] == 1
        assert len(data["organizations"]) == 1
        assert data["organizations"][0]["organization_id"] == self.organization.id

    @patch("calendar_integration.management.commands.webhook_health_check.WebhookAnalyticsService")
    def test_check_organization_health_verbose(self, mock_analytics_service: Mock) -> None:
        """Test organization health check with verbose output."""
        # Create mock failed webhook
        mock_failed_webhook = Mock()
        mock_failed_webhook.id = 1
        mock_failed_webhook.provider = "google"
        mock_failed_webhook.event_type = "updated"
        mock_failed_webhook.created = timezone.now()
        mock_failed_webhook.error_message = "Test error message"

        mock_service_instance = Mock()
        mock_analytics_service.return_value = mock_service_instance

        mock_service_instance.get_subscription_health_report.return_value = {
            "total_subscriptions": 3,
            "active_subscriptions": 2,
            "expired_subscriptions": 1,
            "expiring_soon_subscriptions": 1,
            "stale_subscriptions": 1,
            "delivery_stats": {
                "total_events": 200,
                "successful_events": 180,
                "failed_events": 20,
                "ignored_events": 0,
                "pending_events": 0,
                "success_rate": 90.0,
                "failure_rate": 10.0,
            },
        }

        mock_service_instance.get_webhook_latency_metrics.return_value = {
            "avg_latency": 2.0,
            "p95_latency": 5.0,
        }

        mock_service_instance.get_failed_webhooks.return_value = [mock_failed_webhook]
        mock_service_instance.generate_webhook_failure_alert.return_value = {
            "message": "High failure rate detected"
        }

        report = self.command.check_organization_health(self.organization, 24, True)

        assert report["organization_id"] == self.organization.id
        assert report["subscriptions"]["total"] == 3
        assert report["subscriptions"]["expired"] == 1
        assert report["events"]["total"] == 200
        assert report["events"]["failed"] == 20
        assert report["alert"]["message"] == "High failure rate detected"
        assert "failed_webhook_details" in report
        assert len(report["failed_webhook_details"]) == 1

    def test_print_text_report(self) -> None:
        """Test printing text report."""
        report = {
            "organizations_checked": 2,
            "total_subscriptions": 10,
            "total_active_subscriptions": 8,
            "total_expired_subscriptions": 2,
            "total_events": 1000,
            "total_failed_events": 50,
            "organizations": [
                {
                    "organization_id": 1,
                    "organization_name": "Test Org",
                    "subscriptions": {
                        "total": 5,
                        "active": 4,
                        "expired": 1,
                        "expiring_soon": 0,
                        "stale": 0,
                    },
                    "events": {
                        "total": 500,
                        "successful": 475,
                        "failed": 25,
                        "ignored": 0,
                        "pending": 0,
                        "success_rate": 95.0,
                        "failure_rate": 5.0,
                    },
                    "latency": {"avg_latency": 1.5, "p95_latency": 2.8},
                    "alert": None,
                    "recent_failures": 0,
                }
            ],
        }

        out = StringIO()
        with patch.object(self.command, "stdout", out):
            self.command.print_text_report(report, False)

        output = out.getvalue()
        assert "Organizations Checked: 2" in output
        assert "Overall Success Rate: 95.0%" in output

    def test_print_organization_report_with_issues(self) -> None:
        """Test printing organization report with various issues."""
        org_data = {
            "organization_id": 1,
            "organization_name": "Problem Org",
            "subscriptions": {
                "total": 5,
                "active": 2,
                "expired": 2,
                "expiring_soon": 1,
                "stale": 1,
            },
            "events": {
                "total": 100,
                "successful": 80,
                "failed": 15,
                "ignored": 0,
                "pending": 5,
                "success_rate": 80.0,
                "failure_rate": 15.0,
            },
            "latency": {"avg_latency": 3.0, "p95_latency": 8.0},
            "alert": {"message": "Multiple issues detected"},
            "recent_failures": 3,
            "failed_webhook_details": [
                {
                    "provider": "google",
                    "event_type": "updated",
                    "created": timezone.now(),
                    "error_message": "Connection timeout",
                }
            ],
        }

        out = StringIO()
        with patch.object(self.command, "stdout", out):
            self.command.print_organization_report(org_data, True)

        output = out.getvalue()
        assert "Problem Org" in output
        assert "⚠ Expired: 2" in output
        assert "⚠ Expiring Soon: 1" in output
        assert "⚠ Stale: 1" in output
        assert "⚠ Failed: 15" in output
        assert "⚠ Pending: 5" in output
        assert "🚨 ALERT: Multiple issues detected" in output
        assert "Recent Failed Webhooks:" in output
        assert "Connection timeout" in output

    def test_print_organization_report_no_events(self) -> None:
        """Test printing organization report with no events."""
        org_data = {
            "organization_id": 1,
            "organization_name": "Empty Org",
            "subscriptions": {
                "total": 1,
                "active": 1,
                "expired": 0,
                "expiring_soon": 0,
                "stale": 0,
            },
            "events": {
                "total": 0,
                "successful": 0,
                "failed": 0,
                "ignored": 0,
                "pending": 0,
                "success_rate": 100.0,
                "failure_rate": 0.0,
            },
            "latency": {"avg_latency": 0.0, "p95_latency": 0.0},
            "alert": None,
            "recent_failures": 0,
        }

        out = StringIO()
        with patch.object(self.command, "stdout", out):
            self.command.print_organization_report(org_data, False)

        output = out.getvalue()
        assert "Empty Org" in output
        assert "Total: 0" in output
        # Should not show latency metrics for organizations with no events

    @patch("calendar_integration.management.commands.webhook_health_check.WebhookAnalyticsService")
    def test_handle_with_custom_hours_back(self, mock_analytics_service: Mock) -> None:
        """Test command with custom hours_back parameter."""
        mock_service_instance = Mock()
        mock_analytics_service.return_value = mock_service_instance

        mock_service_instance.get_subscription_health_report.return_value = {
            "total_subscriptions": 1,
            "active_subscriptions": 1,
            "expired_subscriptions": 0,
            "expiring_soon_subscriptions": 0,
            "stale_subscriptions": 0,
            "delivery_stats": {
                "total_events": 5,
                "successful_events": 5,
                "failed_events": 0,
                "ignored_events": 0,
                "pending_events": 0,
                "success_rate": 100.0,
                "failure_rate": 0.0,
            },
        }

        mock_service_instance.get_webhook_latency_metrics.return_value = {
            "avg_latency": 1.0,
            "p95_latency": 1.5,
        }

        mock_service_instance.get_failed_webhooks.return_value = []
        mock_service_instance.generate_webhook_failure_alert.return_value = None

        out = StringIO()
        call_command(
            "webhook_health_check",
            organization_id=self.organization.id,
            hours_back=48,
            stdout=out,
        )

        # Verify the analytics service was called with the correct hours_back
        mock_service_instance.get_webhook_latency_metrics.assert_called_with(hours_back=48)
        mock_service_instance.get_failed_webhooks.assert_called_with(hours_back=48, limit=5)
        mock_service_instance.generate_webhook_failure_alert.assert_called_with(hours_back=48)
