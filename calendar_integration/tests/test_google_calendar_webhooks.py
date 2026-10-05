"""Tests for the Google Calendar webhook receiver."""

import datetime
from typing import Any
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

import pytest
from allauth.socialaccount.models import SocialAccount
from model_bakery import baker
from vinta_billing.constants import BillingState
from vinta_billing.models import BillingPlan, Subscription, SubscriptionEntitlement

from calendar_integration.constants import (
    CalendarProvider,
    CalendarSyncStatus,
    CalendarSyncTriggerSource,
    IncomingWebhookProcessingStatus,
)
from calendar_integration.exceptions import (
    ServiceNotAuthenticatedError,
    WebhookIgnoredError,
    WebhookProcessingFailedError,
)
from calendar_integration.models import (
    Calendar,
    CalendarSync,
    CalendarWebhookEvent,
    CalendarWebhookSubscription,
    GoogleCalendarServiceAccount,
)
from calendar_integration.services.calendar_adapters.google_calendar_adapter import (
    GoogleCalendarAdapter,
)
from calendar_integration.services.calendar_service import CalendarService
from organizations.models import Organization
from payments.seams.resource_keys import EXTERNAL_CALENDAR_GOOGLE
from payments.seams.scopes import scope_for
from users.models import User


@override_settings(GOOGLE_CLIENT_ID="test_client_id", GOOGLE_CLIENT_SECRET="test_client_secret")
class GoogleCalendarAdapterWebhookTest(TestCase):
    """Test Google Calendar adapter webhook methods."""

    def setUp(self):
        self.credentials = {
            "token": "test_token",
            "refresh_token": "test_refresh_token",
            "account_id": "test_account",
        }

    @patch("calendar_integration.services.calendar_adapters.google_calendar_adapter.build")
    def test_validate_webhook_notification_valid(self, mock_build):
        """Test validation of valid Google webhook notification."""
        adapter = GoogleCalendarAdapter(self.credentials)

        headers = {
            "X-Goog-Resource-ID": "test-resource-id",
            "X-Goog-Resource-URI": "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            "X-Goog-Resource-State": "exists",
            "X-Goog-Channel-ID": "test-channel-id",
            "X-Goog-Channel-Token": "test-token",
        }

        result = adapter.validate_webhook_notification(headers, "")

        assert result["provider"] == "google"
        assert result["calendar_id"] == "primary"
        assert result["resource_id"] == "test-resource-id"
        assert result["event_type"] == "exists"
        assert result["channel_id"] == "test-channel-id"

    @patch("calendar_integration.services.calendar_adapters.google_calendar_adapter.build")
    def test_validate_webhook_notification_missing_headers(self, mock_build):
        """Test validation with missing required headers."""
        adapter = GoogleCalendarAdapter(self.credentials)

        headers = {
            "X-Goog-Resource-ID": "test-resource-id",
            # Missing other required headers
        }

        with pytest.raises(
            WebhookProcessingFailedError, match="Missing required Google webhook headers"
        ):
            adapter.validate_webhook_notification(headers, "")

    @patch("calendar_integration.services.calendar_adapters.google_calendar_adapter.build")
    def test_validate_webhook_notification_invalid_resource_uri(self, mock_build):
        """Test validation with invalid resource URI."""
        adapter = GoogleCalendarAdapter(self.credentials)

        headers = {
            "X-Goog-Resource-ID": "test-resource-id",
            "X-Goog-Resource-URI": "https://invalid-uri.com/not-calendar",
            "X-Goog-Resource-State": "exists",
            "X-Goog-Channel-ID": "test-channel-id",
            "X-Goog-Channel-Token": "test-token",
        }

        with pytest.raises(WebhookProcessingFailedError, match="Could not extract calendar ID"):
            adapter.validate_webhook_notification(headers, "")

    def test_validate_webhook_notification_static(self):
        """Test static validation method."""
        headers = {
            "X-Goog-Resource-ID": "test-resource-id",
            "X-Goog-Resource-URI": "https://www.googleapis.com/calendar/v3/calendars/test-calendar/events",
            "X-Goog-Resource-State": "exists",
            "X-Goog-Channel-ID": "test-channel-id",
            "X-Goog-Channel-Token": "test-token",
        }

        result = GoogleCalendarAdapter.validate_webhook_notification_static(headers, "")

        assert result["provider"] == "google"
        assert result["calendar_id"] == "test-calendar"
        assert result["event_type"] == "exists"

    def test_validate_webhook_notification_sync_ignored(self):
        """Test that sync notifications are ignored."""
        headers = {
            "X-Goog-Resource-ID": "test-resource-id",
            "X-Goog-Resource-URI": "https://www.googleapis.com/calendar/v3/calendars/test-calendar/events",
            "X-Goog-Resource-State": "sync",
            "X-Goog-Channel-ID": "test-channel-id",
            "X-Goog-Channel-Token": "test-token",
        }

        with pytest.raises(WebhookIgnoredError, match="Skip sync notification"):
            GoogleCalendarAdapter.validate_webhook_notification_static(headers, "")

    @patch("calendar_integration.services.calendar_adapters.google_calendar_adapter.build")
    @patch(
        "calendar_integration.services.calendar_adapters.google_calendar_adapter.write_quote_limiter"
    )
    def test_create_webhook_subscription_with_tracking(self, mock_limiter, mock_build):
        """Test creating webhook subscription with tracking."""
        mock_client = Mock()
        mock_events = Mock()
        mock_client.events.return_value = mock_events
        mock_watch = Mock()
        mock_events.watch.return_value = mock_watch
        mock_watch.execute.return_value = {
            "id": "test-channel-id",
            "resourceId": "test-resource-id",
            "resourceUri": "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            "expiration": "1234567890000",
        }
        mock_build.return_value = mock_client

        adapter = GoogleCalendarAdapter(self.credentials)

        result = adapter.create_webhook_subscription_with_tracking(
            resource_id="primary",
            callback_url="https://example.com/webhook",
            tracking_params={"ttl_seconds": 3600},
        )

        assert result["channel_id"] == "test-channel-id"
        assert result["resource_id"] == "test-resource-id"
        assert result["calendar_id"] == "primary"
        assert result["callback_url"] == "https://example.com/webhook"


class CalendarServiceWebhookTest(TestCase):
    """Test CalendarService webhook methods."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.calendar = Calendar.objects.create(
            name="Test Calendar",
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            external_id="test-calendar-id",
        )
        self.service = CalendarService()
        self.service.organization = self.organization

    def test_request_webhook_triggered_sync_calendar_not_found(self):
        """Test webhook sync when calendar not found."""
        webhook_event = CalendarWebhookEvent.objects.create(
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            event_type="exists",
            external_calendar_id="nonexistent-calendar",
            external_event_id="",
            raw_payload={"raw": ""},
        )

        result = self.service.request_webhook_triggered_sync(
            external_calendar_id="nonexistent-calendar", webhook_event=webhook_event
        )

        assert result is None

    @patch("calendar_integration.tasks.sync_calendar_task.delay")
    def test_request_webhook_triggered_sync_success(self, mock_sync_task):
        """Test successful webhook-triggered sync."""
        webhook_event = CalendarWebhookEvent.objects.create(
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            event_type="exists",
            external_calendar_id="test-calendar-id",
            external_event_id="",
            raw_payload={"raw": ""},
        )

        # Mock the calendar service as authenticated
        self.service.account = Mock()
        self.service.account.id = 1
        self.service.calendar_adapter = Mock()

        result = self.service.request_webhook_triggered_sync(
            external_calendar_id="test-calendar-id", webhook_event=webhook_event
        )

        assert result is not None
        assert isinstance(result, CalendarSync)
        assert result.calendar == self.calendar

        # Refresh webhook event to check updates
        webhook_event.refresh_from_db()
        assert webhook_event.processing_status == IncomingWebhookProcessingStatus.PROCESSED
        assert webhook_event.calendar_sync == result

    @patch("calendar_integration.tasks.sync_calendar_task.delay")
    def test_request_webhook_triggered_sync_recent_sync_exists(self, mock_sync_task):
        """Test webhook sync when recent sync already exists."""
        # Create a recent sync
        recent_sync = CalendarSync.objects.create(
            calendar=self.calendar,
            organization=self.organization,
            start_datetime=datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(hours=12),
            end_datetime=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(hours=12),
            status=CalendarSyncStatus.SUCCESS,
            should_update_events=True,
        )

        webhook_event = CalendarWebhookEvent.objects.create(
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            event_type="exists",
            external_calendar_id="test-calendar-id",
            external_event_id="",
            raw_payload={"raw": ""},
        )

        # Mock the calendar service as authenticated
        self.service.account = Mock()
        self.service.calendar_adapter = Mock()

        result = self.service.request_webhook_triggered_sync(
            external_calendar_id="test-calendar-id", webhook_event=webhook_event
        )

        # Should return the existing sync, not create a new one
        assert result == recent_sync

        # Check that no new sync was created
        assert CalendarSync.objects.filter_by_organization(self.organization).count() == 1

        # Verify webhook event was updated
        webhook_event.refresh_from_db()
        assert webhook_event.processing_status == IncomingWebhookProcessingStatus.PROCESSED
        assert webhook_event.calendar_sync == recent_sync

    @override_settings(GOOGLE_CLIENT_ID="test_client_id", GOOGLE_CLIENT_SECRET="test_client_secret")
    @patch("calendar_integration.services.calendar_adapters.google_calendar_adapter.build")
    @patch(
        "calendar_integration.services.calendar_adapters.google_calendar_adapter.write_quote_limiter"
    )
    def test_create_calendar_webhook_subscription_google(self, mock_limiter, mock_build):
        """Test creating Google Calendar webhook subscription."""
        # Mock Google API response
        mock_client = Mock()
        mock_events = Mock()
        mock_client.events.return_value = mock_events
        mock_watch = Mock()
        mock_events.watch.return_value = mock_watch
        mock_watch.execute.return_value = {
            "id": "test-channel-id",
            "resourceId": "test-resource-id",
            "resourceUri": "https://www.googleapis.com/calendar/v3/calendars/test-calendar-id/events",
            "expiration": "1234567890000",
        }
        mock_build.return_value = mock_client

        # Mock authenticated service
        self.service.account = Mock()
        self.service.calendar_adapter = Mock()
        self.service.calendar_adapter.create_webhook_subscription_with_tracking.return_value = {
            "channel_id": "test-channel-id",
            "resource_id": "test-resource-id",
            "resource_uri": "https://www.googleapis.com/calendar/v3/calendars/test-calendar-id/events",
            "expiration": "1234567890000",
            "calendar_id": "test-calendar-id",
            "callback_url": "https://example.com/webhook",
            "channel_token": "test-verification-token",
        }

        result = self.service.create_calendar_webhook_subscription(
            calendar=self.calendar, callback_url="https://example.com/webhook", expiration_hours=24
        )

        assert isinstance(result, CalendarWebhookSubscription)
        assert result.calendar == self.calendar
        assert result.provider == CalendarProvider.GOOGLE
        assert result.callback_url == "https://example.com/webhook"
        assert result.channel_id == "test-channel-id"

    def test_create_calendar_webhook_subscription_not_authenticated(self):
        """Test creating webhook subscription when not authenticated."""
        # Create a fresh service instance that's truly not authenticated
        unauthenticated_service = CalendarService()
        # Don't set organization, account, or adapter

        with pytest.raises(
            ServiceNotAuthenticatedError, match="Calendar service is not authenticated"
        ):
            unauthenticated_service.create_calendar_webhook_subscription(
                calendar=self.calendar, callback_url="https://example.com/webhook"
            )

    @patch.object(CalendarService, "request_webhook_triggered_sync")
    def test_process_webhook_notification_google(self, mock_sync):
        """Test processing Google webhook notification."""
        mock_sync.return_value = Mock(id=123)

        headers = {
            "X-Goog-Resource-ID": "test-resource-id",
            "X-Goog-Resource-URI": "https://www.googleapis.com/calendar/v3/calendars/test-calendar-id/events",
            "X-Goog-Resource-State": "exists",
            "X-Goog-Channel-ID": "test-channel-id",
            "X-Goog-Channel-Token": "test-token",
        }

        result = self.service.process_webhook_notification(
            provider="google", calendar_external_id="test-calendar-id", headers=headers
        )

        assert isinstance(result, CalendarWebhookEvent)
        assert result.provider == "google"
        assert result.event_type == "exists"
        assert result.external_calendar_id == "test-calendar-id"


class GoogleCalendarWebhookViewTest(TestCase):
    """Test Google Calendar webhook view."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.calendar = Calendar.objects.create(
            name="Test Calendar",
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            external_id="test-calendar-id",
        )
        self.webhook_url = reverse(
            "calendar_integration:google_webhook", kwargs={"organization_id": self.organization.id}
        )

    def test_google_webhook_sync_notification_ignored(self):
        """Test that sync notifications are ignored."""
        headers = {
            "HTTP_X_GOOG_CHANNEL_ID": "test-channel-id",
            "HTTP_X_GOOG_RESOURCE_ID": "test-resource-id",
            "HTTP_X_GOOG_RESOURCE_URI": "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            "HTTP_X_GOOG_RESOURCE_STATE": "sync",
            "HTTP_X_GOOG_CHANNEL_TOKEN": "test-token",
        }

        response = self.client.post(self.webhook_url, **headers)

        assert response.status_code == 200
        # No webhook event should be created for sync notifications
        assert CalendarWebhookEvent.objects.filter_by_organization(self.organization).count() == 0

    @patch("calendar_integration.services.calendar_service.CalendarService.handle_webhook")
    def test_google_webhook_exists_notification(self, mock_handle_webhook):
        """Test processing exists notification."""
        # Mock the service to return a webhook event
        mock_webhook_event = Mock()
        mock_webhook_event.id = 1
        mock_handle_webhook.return_value = mock_webhook_event

        headers = {
            "HTTP_X_GOOG_CHANNEL_ID": "test-channel-id",
            "HTTP_X_GOOG_RESOURCE_ID": "test-resource-id",
            "HTTP_X_GOOG_RESOURCE_URI": "https://www.googleapis.com/calendar/v3/calendars/test-calendar-id/events",
            "HTTP_X_GOOG_RESOURCE_STATE": "exists",
            "HTTP_X_GOOG_CHANNEL_TOKEN": "test-token",
        }

        response = self.client.post(self.webhook_url, **headers)

        assert response.status_code == 200
        mock_handle_webhook.assert_called_once()
        # Verify the call was made with "google" as provider and HttpRequest as second argument
        args = mock_handle_webhook.call_args[0]
        assert args[0] == CalendarProvider.GOOGLE
        assert hasattr(args[1], "META")  # Check it's an HttpRequest object

    @patch("calendar_integration.services.calendar_service.CalendarService.handle_webhook")
    def test_google_webhook_missing_headers(self, mock_handle_webhook):
        """Test webhook with missing required headers."""
        # Mock the service to raise a validation error
        mock_handle_webhook.side_effect = WebhookProcessingFailedError(
            "Missing required Google webhook headers"
        )

        headers = {
            "HTTP_X_GOOG_CHANNEL_ID": "test-channel-id",
        }

        response = self.client.post(self.webhook_url, **headers)

        assert response.status_code == 400

    def test_google_webhook_no_organization_found(self):
        """Test webhook when organization cannot be determined."""
        # Test with an invalid organization ID in the URL
        invalid_webhook_url = reverse(
            "calendar_integration:google_webhook", kwargs={"organization_id": 99999}
        )

        headers = {
            "HTTP_X_GOOG_CHANNEL_ID": "nonexistent-channel-id",
            "HTTP_X_GOOG_RESOURCE_ID": "test-resource-id",
            "HTTP_X_GOOG_RESOURCE_URI": "https://www.googleapis.com/calendar/v3/calendars/test-calendar-id/events",
            "HTTP_X_GOOG_RESOURCE_STATE": "exists",
            "HTTP_X_GOOG_CHANNEL_TOKEN": "test-token",
        }

        response = self.client.post(invalid_webhook_url, **headers)

        # Should return 404 when organization is not found
        assert response.status_code == 404


class GoogleCalendarWebhookIntegrationTest(TestCase):
    """Integration tests for Google Calendar webhook functionality."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.calendar = Calendar.objects.create(
            name="Test Calendar",
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            external_id="test-calendar-id",
        )

    @patch("calendar_integration.tasks.sync_calendar_task.delay")
    @patch("calendar_integration.services.calendar_service.CalendarService.handle_webhook")
    def test_end_to_end_webhook_processing(self, mock_handle_webhook, mock_sync_task):
        """Test complete webhook processing flow."""
        # Create webhook subscription
        CalendarWebhookSubscription.objects.create(
            calendar=self.calendar,
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            external_subscription_id="test-sub-id",
            channel_id="test-channel-id",
            callback_url="https://example.com/webhook",
            verification_token="test-token",
        )

        # Mock CalendarService
        mock_webhook_event = Mock()
        mock_webhook_event.id = 1
        mock_handle_webhook.return_value = mock_webhook_event

        webhook_url = reverse(
            "calendar_integration:google_webhook", kwargs={"organization_id": self.organization.id}
        )

        headers = {
            "HTTP_X_GOOG_CHANNEL_ID": "test-channel-id",
            "HTTP_X_GOOG_RESOURCE_ID": "test-resource-id",
            "HTTP_X_GOOG_RESOURCE_URI": "https://www.googleapis.com/calendar/v3/calendars/test-calendar-id/events",
            "HTTP_X_GOOG_RESOURCE_STATE": "exists",
            "HTTP_X_GOOG_CHANNEL_TOKEN": "test-token",
        }

        response = self.client.post(webhook_url, **headers)

        assert response.status_code == 200

        # Verify service was called correctly
        mock_handle_webhook.assert_called_once()
        # Verify the call was made with "google" as provider and HttpRequest as second argument
        args = mock_handle_webhook.call_args[0]
        assert args[0] == CalendarProvider.GOOGLE
        assert hasattr(args[1], "META")  # Check it's an HttpRequest object


class GoogleCalendarWebhookOverLimitEntitlementTest(TestCase):
    """An ``OverLimitError`` raised by the entitlement-gated
    ``_get_write_adapter_for_calendar`` must not 500 the webhook endpoint.

    ``_get_write_adapter_for_calendar`` (the second, calendar-provider-scoped
    entitlement gate) raises ``OverLimitError`` when the organization has lost the
    calendar's provider entitlement. The webhook flow's only job on that path is to
    fall back to static validation and still record the ``CalendarWebhookEvent`` --
    exactly what it already does for ``ServiceNotAuthenticatedError`` /
    ``Calendar.DoesNotExist``. Before the fix, ``OverLimitError`` was not in that
    caught tuple, so it escaped to ``webhook_views.py``'s bare ``except Exception``
    and turned into an HTTP 500, with no ``CalendarWebhookEvent`` row recorded --
    exactly what Google/Microsoft retry against until the channel expires.
    """

    # This test builds its own Subscription row (OneToOne with Organization), so it
    # opts out of conftest's autouse `provision_default_subscription`.
    pytestmark = pytest.mark.no_auto_subscription

    def setUp(self):
        self.organization = baker.make(Organization, parent=None, can_invite_organizations=False)
        now = timezone.now()
        subscription = baker.make(
            Subscription,
            scope=scope_for(self.organization),
            plan=baker.make(BillingPlan, is_default_for_new_scopes=False),
            billing_state=BillingState.FREE,
            current_period_start=now,
            current_period_end=now + datetime.timedelta(days=30),
        )
        baker.make(
            SubscriptionEntitlement,
            subscription=subscription,
            entitlement_key=EXTERNAL_CALENDAR_GOOGLE,
            is_enabled=False,
        )
        self.calendar = Calendar.objects.create(
            name="Entitlement Test Calendar",
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            external_id="entitlement-test-calendar-id",
        )
        self.webhook_url = reverse(
            "calendar_integration:google_webhook", kwargs={"organization_id": self.organization.id}
        )

    def test_over_limit_error_when_authenticating_does_not_500(self):
        """The channel is verified, but authenticating as its account raises
        ``OverLimitError`` because the organization lost the Google entitlement. A
        provider push has no user to show a 402 to, and Google would only retry a 500,
        so the notification is recorded and nothing is synced."""
        user = User.objects.create_user(email="overlimit@example.com", password="pass")  # noqa: S106
        social_account = SocialAccount.objects.create(
            user=user, provider=CalendarProvider.GOOGLE, uid="overlimit-uid"
        )
        CalendarWebhookSubscription.objects.create(
            calendar=self.calendar,
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            external_subscription_id="test-channel-id",
            external_resource_id="test-resource-id",
            channel_id="test-channel-id",
            callback_url="https://example.com/webhook",
            verification_token=CalendarWebhookSubscription.hash_verification_token("test-token"),
            social_account=social_account,
        )

        headers = {
            "HTTP_X_GOOG_CHANNEL_ID": "test-channel-id",
            "HTTP_X_GOOG_RESOURCE_ID": "test-resource-id",
            "HTTP_X_GOOG_RESOURCE_URI": (
                "https://www.googleapis.com/calendar/v3/calendars/"
                "entitlement-test-calendar-id/events"
            ),
            "HTTP_X_GOOG_RESOURCE_STATE": "exists",
            "HTTP_X_GOOG_CHANNEL_TOKEN": "test-token",
        }

        response = self.client.post(self.webhook_url, **headers)

        assert response.status_code == 200
        assert CalendarWebhookEvent.objects.filter_by_organization(self.organization).exists()
        assert not CalendarSync.objects.filter_by_organization(self.organization).exists()


class GoogleCalendarPushNotificationSyncTest(TestCase):
    """A real notification through the real view queues a sync of the channel's
    calendar, as the account that opened the channel.

    Before this, the webhook path never authenticated: every notification was stored
    as ``PENDING`` and nothing ever synced. Only the provider adapter and the Celery
    dispatch are faked here.
    """

    def setUp(self):
        self.organization = Organization.objects.create(name="Push Org")
        self.user = User.objects.create_user(email="push@example.com", password="pass")  # noqa: S106
        self.social_account = SocialAccount.objects.create(
            user=self.user, provider=CalendarProvider.GOOGLE, uid="push-uid"
        )
        self.calendar = Calendar.objects.create(
            name="Push Calendar",
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            external_id="push@example.com",
        )
        self.subscription = CalendarWebhookSubscription.objects.create(
            calendar=self.calendar,
            organization=self.organization,
            provider=CalendarProvider.GOOGLE,
            external_subscription_id="calendar-0123abcd",
            external_resource_id="opaque-resource-id",
            channel_id="calendar-0123abcd",
            callback_url="https://example.com/webhook",
            verification_token=CalendarWebhookSubscription.hash_verification_token("s3cret"),
            social_account=self.social_account,
        )
        self.webhook_url = reverse(
            "calendar_integration:google_webhook", kwargs={"organization_id": self.organization.id}
        )
        adapter = Mock()
        adapter.provider = CalendarProvider.GOOGLE
        adapter.validate_webhook_notification.side_effect = (
            lambda headers, body, expected_channel_id=None: (
                GoogleCalendarAdapter.validate_webhook_notification_static(headers, body)
            )
        )
        self.adapter = adapter

    def _headers(self, token: str = "s3cret", state: str = "exists") -> dict[str, Any]:
        return {
            "HTTP_X_GOOG_CHANNEL_ID": "calendar-0123abcd",
            # The watched resource's opaque id -- not a calendar id.
            "HTTP_X_GOOG_RESOURCE_ID": "opaque-resource-id",
            "HTTP_X_GOOG_RESOURCE_URI": (
                "https://www.googleapis.com/calendar/v3/calendars/push%40example.com/events"
                "?alt=json"
            ),
            "HTTP_X_GOOG_RESOURCE_STATE": state,
            "HTTP_X_GOOG_CHANNEL_TOKEN": token,
        }

    def _post(self, headers: dict[str, Any]):
        with (
            patch.object(
                CalendarService,
                "get_calendar_adapter_for_account",
                return_value=(self.adapter, self.social_account),
            ) as get_adapter,
            patch("calendar_integration.tasks.sync_calendar_task.delay") as delay,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(self.webhook_url, **headers)
        return response, get_adapter, delay

    def test_notification_queues_a_sync_of_the_channel_calendar(self):
        response, get_adapter, delay = self._post(self._headers())

        assert response.status_code == 200
        get_adapter.assert_called_once_with(self.social_account)
        calendar_sync = CalendarSync.objects.filter_by_organization(self.organization).get()
        assert (calendar_sync.calendar, calendar_sync.trigger_source) == (
            self.calendar,
            CalendarSyncTriggerSource.WEBHOOK,
        )
        delay.assert_called_once_with(
            "social_account", self.social_account.id, calendar_sync.id, self.organization.id
        )
        event = CalendarWebhookEvent.objects.filter_by_organization(self.organization).get()
        assert (
            event.subscription,
            event.calendar_sync,
            event.processing_status,
            event.external_calendar_id,
        ) == (
            self.subscription,
            calendar_sync,
            IncomingWebhookProcessingStatus.PROCESSED,
            "push@example.com",
        )

    def test_forged_token_is_rejected_without_recording_or_syncing(self):
        response, get_adapter, delay = self._post(self._headers(token="forged"))

        assert response.status_code == 400
        get_adapter.assert_not_called()
        delay.assert_not_called()
        assert not CalendarWebhookEvent.objects.filter_by_organization(self.organization).exists()

    def test_channel_of_another_organization_is_rejected(self):
        other = Organization.objects.create(name="Other Org")
        other_url = reverse(
            "calendar_integration:google_webhook", kwargs={"organization_id": other.id}
        )
        with patch("calendar_integration.tasks.sync_calendar_task.delay") as delay:
            response = self.client.post(other_url, **self._headers())

        assert response.status_code == 400
        delay.assert_not_called()

    def test_sync_handshake_is_acknowledged_and_ignored(self):
        response, get_adapter, delay = self._post(self._headers(state="sync"))

        assert response.status_code == 200
        get_adapter.assert_not_called()
        delay.assert_not_called()
        assert not CalendarWebhookEvent.objects.filter_by_organization(self.organization).exists()

    def test_room_channel_syncs_as_the_service_account_that_opened_it(self):
        service_account = GoogleCalendarServiceAccount.objects.create(
            organization=self.organization,
            email="sa@project.iam.gserviceaccount.com",
            private_key_id="key-id",
            private_key="key",
        )
        self.subscription.social_account = None
        self.subscription.google_service_account = service_account
        self.subscription.save()

        with (
            patch.object(
                CalendarService,
                "get_calendar_adapter_for_account",
                return_value=(self.adapter, service_account),
            ) as get_adapter,
            patch("calendar_integration.tasks.sync_calendar_task.delay") as delay,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(self.webhook_url, **self._headers())

        assert response.status_code == 200
        get_adapter.assert_called_once_with(service_account)
        calendar_sync = CalendarSync.objects.filter_by_organization(self.organization).get()
        delay.assert_called_once_with(
            "google_service_account", service_account.id, calendar_sync.id, self.organization.id
        )
