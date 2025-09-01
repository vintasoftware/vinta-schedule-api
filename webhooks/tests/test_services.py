import datetime
from unittest.mock import Mock, patch

import pytest
import requests
from model_bakery import baker

from organizations.models import Organization
from webhooks.constants import WebhookEventType, WebhookStatus
from webhooks.models import WebhookConfiguration, WebhookEvent
from webhooks.services import WebhookService


@pytest.mark.django_db
class TestWebhookService:
    """Test suite for WebhookService."""

    @pytest.fixture
    def organization(self):
        """Create a test organization."""
        return baker.make(Organization, name="Test Organization")

    @pytest.fixture
    def webhook_configuration(self, organization):
        """Create a test webhook configuration."""
        return baker.make(
            WebhookConfiguration,
            organization=organization,
            event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
            url="https://example.com/webhook",
            headers={"Authorization": "Bearer token123"},
        )

    @pytest.fixture
    def webhook_event(self, organization, webhook_configuration):
        """Create a test webhook event."""
        return baker.make(
            WebhookEvent,
            organization=organization,
            configuration=webhook_configuration,
            event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
            url="https://example.com/webhook",
            headers={"Authorization": "Bearer token123"},
            payload={"event": "test"},
            status=WebhookStatus.PENDING,
        )

    @pytest.fixture
    def webhook_service(self):
        """Create WebhookService instance."""
        return WebhookService()

    def test_create_configuration(self, organization, webhook_service):
        """Test creating a webhook configuration."""
        event_type = WebhookEventType.CALENDAR_EVENT_CREATED
        url = "https://example.com/webhook"
        headers = {"Authorization": "Bearer token123"}

        configuration = webhook_service.create_configuration(
            organization=organization, event_type=event_type, url=url, headers=headers
        )

        assert configuration.organization == organization
        assert configuration.event_type == event_type
        assert configuration.url == url
        assert configuration.headers == headers
        assert configuration.deleted_at is None

    def test_update_configuration(self, webhook_configuration, webhook_service):
        """Test updating a webhook configuration."""
        new_event_type = WebhookEventType.CALENDAR_EVENT_UPDATED
        new_url = "https://newexample.com/webhook"
        new_headers = {"Authorization": "Bearer newtoken123"}

        updated_configuration = webhook_service.update_configuration(
            configuration=webhook_configuration,
            event_type=new_event_type,
            url=new_url,
            headers=new_headers,
        )

        assert updated_configuration == webhook_configuration
        webhook_configuration.refresh_from_db()
        assert webhook_configuration.event_type == new_event_type
        assert webhook_configuration.url == new_url
        assert webhook_configuration.headers == new_headers

    def test_delete_configuration(self, webhook_configuration, webhook_service):
        """Test soft deleting a webhook configuration."""
        result = webhook_service.delete_configuration(webhook_configuration)

        assert result is True
        webhook_configuration.refresh_from_db()
        assert webhook_configuration.deleted_at is not None

    @patch("webhooks.services.process_webhook_event.delay")
    def test_send_events(self, mock_task, organization, webhook_configuration, webhook_service):
        """Test sending webhook events to all configured URLs."""
        # Create another configuration for the same event type
        baker.make(
            WebhookConfiguration,
            organization=organization,
            event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
            url="https://example2.com/webhook",
            headers={"Authorization": "Bearer token456"},
        )

        payload = {"event": "calendar_created", "data": {"id": 123}}

        webhook_events = webhook_service.send_events(
            organization=organization,
            event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
            payload=payload,
        )

        assert len(webhook_events) == 2
        for event in webhook_events:
            assert event.organization == organization
            assert event.event_type == WebhookEventType.CALENDAR_EVENT_CREATED
            assert event.payload == payload
            assert event.status == WebhookStatus.PENDING

        # Verify that tasks were scheduled
        assert mock_task.call_count == 2

    @patch("webhooks.services.process_webhook_event.delay")
    def test_send_events_excludes_deleted_configurations(
        self, mock_task, organization, webhook_service
    ):
        """Test that deleted configurations are excluded from sending events."""
        # Create a deleted configuration
        baker.make(
            WebhookConfiguration,
            organization=organization,
            event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
            url="https://deleted.com/webhook",
            deleted_at=datetime.datetime.now(tz=datetime.UTC),
        )

        payload = {"event": "calendar_created"}

        webhook_events = webhook_service.send_events(
            organization=organization,
            event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
            payload=payload,
        )

        assert len(webhook_events) == 0
        mock_task.assert_not_called()

    @patch("webhooks.services.process_webhook_event.apply_async")
    def test_schedule_event_retry_automatic(self, mock_apply_async, webhook_event, webhook_service):
        """Test automatic retry scheduling (from failure processing)."""
        retry_event = webhook_service.schedule_event_retry(
            event=webhook_event, use_current_configuration=False, is_manual=False
        )

        assert retry_event is not None
        assert retry_event.organization == webhook_event.organization
        assert retry_event.configuration == webhook_event.configuration
        assert retry_event.event_type == webhook_event.event_type
        assert retry_event.url == webhook_event.url
        assert retry_event.headers == webhook_event.headers
        assert retry_event.payload == webhook_event.payload
        assert retry_event.status == WebhookStatus.PENDING
        assert retry_event.main_event == webhook_event
        assert retry_event.retry_number == 1
        assert retry_event.send_after > datetime.datetime.now(tz=datetime.UTC)

        # Verify task was scheduled with backoff
        mock_apply_async.assert_called_once()
        call_kwargs = mock_apply_async.call_args[1]
        assert call_kwargs["kwargs"]["event_id"] == retry_event.pk
        assert call_kwargs["kwargs"]["organization_id"] == retry_event.organization_id
        assert call_kwargs["countdown"] == 1  # 2^(1-1) = 1 second backoff

    @patch("webhooks.services.process_webhook_event.apply_async")
    def test_schedule_event_retry_manual(self, mock_apply_async, webhook_event, webhook_service):
        """Test manual retry scheduling (immediate retry, no backoff)."""
        retry_event = webhook_service.schedule_event_retry(
            event=webhook_event, use_current_configuration=True, is_manual=True
        )

        assert retry_event is not None
        assert retry_event.url == webhook_event.configuration.url
        assert retry_event.headers == webhook_event.configuration.headers
        assert retry_event.retry_number == 1
        # Manual retry should have immediate send_after
        assert retry_event.send_after <= datetime.datetime.now(
            tz=datetime.UTC
        ) + datetime.timedelta(seconds=1)

        # Verify task was scheduled without backoff
        mock_apply_async.assert_called_once()
        call_kwargs = mock_apply_async.call_args[1]
        assert call_kwargs["countdown"] == 0  # No backoff for manual retries

    def test_schedule_event_retry_max_retries_reached(self, webhook_event, webhook_service):
        """Test that retries are not scheduled when max retries are reached."""
        webhook_event.retry_number = 5  # MAX_WEBHOOK_RETRIES
        webhook_event.save()

        retry_event = webhook_service.schedule_event_retry(
            event=webhook_event, use_current_configuration=False, is_manual=False
        )

        assert retry_event is None

    @patch("webhooks.services.process_webhook_event.apply_async")
    def test_schedule_event_retry_manual_ignores_max_retries(
        self, mock_apply_async, webhook_event, webhook_service
    ):
        """Test that manual retries ignore max retry limit."""
        webhook_event.retry_number = 10  # Well over MAX_WEBHOOK_RETRIES
        webhook_event.save()

        retry_event = webhook_service.schedule_event_retry(
            event=webhook_event, use_current_configuration=False, is_manual=True
        )

        assert retry_event is not None
        assert retry_event.retry_number == 11
        mock_apply_async.assert_called_once()

    @patch("webhooks.services.process_webhook_event.apply_async")
    def test_schedule_event_retry_with_main_event(
        self, mock_apply_async, webhook_event, webhook_service
    ):
        """Test retry scheduling preserves main_event reference."""
        main_event = baker.make(
            WebhookEvent,
            organization=webhook_event.organization,
            configuration=webhook_event.configuration,
            event_type=webhook_event.event_type,
        )
        webhook_event.main_event = main_event
        webhook_event.save()

        retry_event = webhook_service.schedule_event_retry(
            event=webhook_event, use_current_configuration=False, is_manual=False
        )

        assert retry_event.main_event == main_event

    @patch("requests.post")
    def test_process_webhook_event_success(self, mock_post, webhook_event, webhook_service):
        """Test successful webhook event processing."""
        # Mock successful response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True}
        mock_response.headers = {"Content-Type": "application/json"}
        mock_post.return_value = mock_response

        result_event = webhook_service.process_webhook_event(webhook_event)

        assert result_event == webhook_event
        webhook_event.refresh_from_db()
        assert webhook_event.status == WebhookStatus.SUCCESS
        assert webhook_event.response_status == 200
        assert webhook_event.response_body == {"body": {"success": True}}
        assert webhook_event.response_headers == {"Content-Type": "application/json"}

        # Verify the request was made correctly
        mock_post.assert_called_once_with(
            webhook_event.url,
            headers=webhook_event.headers,
            json=webhook_event.payload,
            timeout=60,
        )

    @patch("requests.post")
    def test_process_webhook_event_success_non_serializable_response(
        self, mock_post, webhook_event, webhook_service
    ):
        """Test successful webhook event processing."""
        # Mock successful response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = "Success"
        mock_response.headers = {"Content-Type": "application/json"}
        mock_post.return_value = mock_response

        result_event = webhook_service.process_webhook_event(webhook_event)

        assert result_event == webhook_event
        webhook_event.refresh_from_db()
        assert webhook_event.status == WebhookStatus.SUCCESS
        assert webhook_event.response_status == 200
        assert webhook_event.response_body == {"body": "Success"}
        assert webhook_event.response_headers == {"Content-Type": "application/json"}

        # Verify the request was made correctly
        mock_post.assert_called_once_with(
            webhook_event.url,
            headers=webhook_event.headers,
            json=webhook_event.payload,
            timeout=60,
        )

    @patch("requests.post")
    @patch.object(WebhookService, "schedule_event_retry")
    def test_process_webhook_event_failure_triggers_retry(
        self, mock_retry, mock_post, webhook_event, webhook_service
    ):
        """Test that failed webhook events trigger automatic retry."""
        # Mock failed response
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.json.return_value = {"error": "Internal Server Error"}
        mock_response.headers = {"Content-Type": "application/json"}
        mock_post.return_value = mock_response

        result_event = webhook_service.process_webhook_event(webhook_event)

        assert result_event == webhook_event
        webhook_event.refresh_from_db()
        assert webhook_event.status == WebhookStatus.FAILED
        assert webhook_event.response_status == 500

        # Verify retry was scheduled
        mock_retry.assert_called_once_with(event=webhook_event)

    @patch("requests.post")
    @patch.object(WebhookService, "schedule_event_retry")
    def test_process_webhook_event_failure_max_retries_no_retry(
        self, mock_retry, mock_post, webhook_event, webhook_service
    ):
        """Test that failed events at max retries don't trigger more retries."""
        webhook_event.retry_number = 5  # MAX_WEBHOOK_RETRIES
        webhook_event.save()

        # Mock failed response
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.json.return_value = {"error": "Internal Server Error"}
        mock_response.headers = {"Content-Type": "application/json"}
        mock_post.return_value = mock_response

        webhook_service.process_webhook_event(webhook_event)

        # Verify no retry was scheduled
        mock_retry.assert_not_called()

    @patch("requests.post")
    def test_process_webhook_event_handles_exceptions(
        self, mock_post, webhook_event, webhook_service
    ):
        """Test that webhook processing handles request exceptions."""
        # Mock request that raises an exception
        mock_post.side_effect = requests.exceptions.RequestException("Network error")

        event = webhook_service.process_webhook_event(webhook_event)
        assert event.status == WebhookStatus.FAILED
        assert event.response_body == {"error": "Network error"}

    def test_process_webhook_event_status_code_boundaries(self, webhook_event, webhook_service):
        """Test webhook status determination for various HTTP status codes."""
        test_cases = [
            (199, WebhookStatus.FAILED),
            (200, WebhookStatus.SUCCESS),
            (201, WebhookStatus.SUCCESS),
            (299, WebhookStatus.SUCCESS),
            (300, WebhookStatus.FAILED),
            (400, WebhookStatus.FAILED),
            (404, WebhookStatus.FAILED),
            (500, WebhookStatus.FAILED),
        ]

        for status_code, expected_status in test_cases:
            with patch("requests.post") as mock_post:
                mock_response = Mock()
                mock_response.status_code = status_code
                mock_response.json.return_value = {}
                mock_response.headers = {}
                mock_post.return_value = mock_response

                # Reset webhook event
                webhook_event.status = WebhookStatus.PENDING
                webhook_event.save()

                webhook_service.process_webhook_event(webhook_event)
                webhook_event.refresh_from_db()

                assert (
                    webhook_event.status == expected_status
                ), f"Status code {status_code} should result in {expected_status}"

    @patch("webhooks.services.process_webhook_event.delay")
    def test_schedule_webhook_event_private_method(
        self, mock_delay, webhook_event, webhook_service
    ):
        """Test the private _schedule_webhook_event method."""
        webhook_service._schedule_webhook_event(webhook_event)

        mock_delay.assert_called_once_with(
            event_id=webhook_event.pk, organization_id=webhook_event.organization_id
        )

    def test_exponential_backoff_calculation(self, webhook_event, webhook_service):
        """Test exponential backoff calculation for retries."""
        test_cases = [
            (0, 1),  # 2^(1-1) = 1
            (1, 2),  # 2^(2-1) = 2
            (2, 4),  # 2^(3-1) = 4
            (3, 8),  # 2^(4-1) = 8
            (4, 16),  # 2^(5-1) = 16
        ]

        for initial_retry_number, expected_backoff in test_cases:
            webhook_event.retry_number = initial_retry_number
            webhook_event.save()

            with patch("webhooks.services.process_webhook_event.apply_async") as mock_apply_async:
                webhook_service.schedule_event_retry(
                    event=webhook_event, use_current_configuration=False, is_manual=False
                )

                call_kwargs = mock_apply_async.call_args[1]
                assert (
                    call_kwargs["countdown"] == expected_backoff
                ), f"Retry {initial_retry_number} should have {expected_backoff}s backoff"
