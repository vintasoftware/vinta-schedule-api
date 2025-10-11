import logging
from typing import Any

from django.utils import timezone

from calendar_integration.constants import CalendarProvider, IncomingWebhookProcessingStatus
from calendar_integration.exceptions import WebhookAuthenticationError, WebhookValidationError
from calendar_integration.models import CalendarWebhookEvent, CalendarWebhookSubscription
from organizations.models import Organization


logger = logging.getLogger(__name__)


class CalendarIncomingWebhookService:
    """
    Service for handling incoming webhook notifications from calendar providers.

    This service coordinates webhook validation, processing, and triggering
    calendar synchronization using the existing CalendarService infrastructure.
    """

    def __init__(self, organization: Organization):
        self.organization = organization

    def validate_webhook_signature(
        self,
        provider: str,
        headers: dict[str, str],
        body: bytes,
        subscription_id: str | None = None,
    ) -> bool:
        """
        Validate webhook signature based on provider-specific requirements.

        Args:
            provider: Calendar provider (google, microsoft)
            headers: HTTP headers from webhook request
            body: Raw request body
            subscription_id: Optional subscription ID for lookup

        Returns:
            True if signature is valid

        Raises:
            WebhookAuthenticationError: If signature validation fails
        """
        if provider == CalendarProvider.GOOGLE:
            return self._validate_google_webhook_signature(headers, body)
        elif provider == CalendarProvider.MICROSOFT:
            return self._validate_microsoft_webhook_signature(headers, body, subscription_id)
        else:
            raise WebhookValidationError(f"Unsupported provider: {provider}")

    def _validate_google_webhook_signature(self, headers: dict[str, str], body: bytes) -> bool:
        """
        Validate Google Calendar webhook signature.

        Google Calendar webhooks don't use signature validation but rely on:
        1. HTTPS callback URLs
        2. Channel ID validation
        3. Resource ID validation
        """
        # Google Calendar webhooks are validated by checking required headers
        required_headers = ["X-Goog-Channel-ID", "X-Goog-Resource-ID", "X-Goog-Resource-State"]

        for header in required_headers:
            if header not in headers:
                raise WebhookAuthenticationError(
                    f"Missing required Google webhook header: {header}"
                )

        return True

    def _validate_microsoft_webhook_signature(
        self, headers: dict[str, str], body: bytes, subscription_id: str | None = None
    ) -> bool:
        """
        Validate Microsoft Graph webhook signature.

        Microsoft Graph webhooks use validation tokens and client state validation.
        """
        # For validation requests, we just need to return the validation token
        validation_token = headers.get("validationToken")
        if validation_token:
            return True

        # For actual notifications, validate the client state if we have a subscription
        if subscription_id:
            try:
                CalendarWebhookSubscription.objects.get(
                    organization=self.organization,
                    external_subscription_id=subscription_id,
                    provider=CalendarProvider.MICROSOFT,
                    is_active=True,
                )
                # Additional validation could be added here
                return True
            except CalendarWebhookSubscription.DoesNotExist:
                raise WebhookAuthenticationError(
                    f"Unknown subscription: {subscription_id}"
                ) from None

        return True

    def process_webhook_notification(
        self,
        provider: str,
        headers: dict[str, str],
        payload: dict[str, Any] | str,
        validation_token: str | None = None,
    ) -> CalendarWebhookEvent | str:
        """
        Process incoming webhook notification.

        Args:
            provider: Calendar provider (google, microsoft)
            headers: HTTP headers from request
            payload: Webhook payload
            validation_token: Validation token for Microsoft webhook setup

        Returns:
            CalendarWebhookEvent for notifications or validation token string for validation requests
        """
        # Handle Microsoft validation requests
        if provider == CalendarProvider.MICROSOFT and validation_token:
            logger.info("Microsoft webhook validation request: %s", validation_token)
            return validation_token

        # Validate the webhook
        try:
            self.validate_webhook_signature(provider, headers, b"", None)
        except (WebhookAuthenticationError, WebhookValidationError) as e:
            logger.error("Webhook validation failed: %s", e)
            raise

        # Create initial webhook event record
        webhook_event = CalendarWebhookEvent.objects.create(
            organization=self.organization,
            provider=provider,
            event_type="unknown",  # Will be updated after parsing
            external_calendar_id="unknown",  # Will be updated after parsing
            raw_payload=payload if isinstance(payload, dict) else {"raw": str(payload)},
            headers=headers,
            processing_status=IncomingWebhookProcessingStatus.PENDING,
        )

        # Process the webhook - for now, just mark as processed
        # TODO: Implement webhook processing logic in Phase 2
        try:
            # Parse basic webhook information to update the event record
            if provider == CalendarProvider.GOOGLE:
                event_type = headers.get("X-Goog-Resource-State", "unknown")
                calendar_id = self._extract_google_calendar_id(
                    headers.get("X-Goog-Resource-URI", "")
                )
            elif provider == CalendarProvider.MICROSOFT:
                event_type = "notification"
                calendar_id = "unknown"  # Will be extracted from payload in Phase 2
            else:
                event_type = "unknown"
                calendar_id = "unknown"

            # Update webhook event with parsed information
            webhook_event.event_type = event_type
            webhook_event.external_calendar_id = calendar_id
            webhook_event.processing_status = IncomingWebhookProcessingStatus.PROCESSED
            webhook_event.processed_at = timezone.now()
            webhook_event.save()

            logger.info("Processed webhook for provider %s, calendar %s", provider, calendar_id)
            return webhook_event

        except Exception as e:
            webhook_event.processing_status = IncomingWebhookProcessingStatus.FAILED
            webhook_event.processed_at = timezone.now()
            webhook_event.save()
            logger.exception("Failed to process webhook: %s", e)
            raise

    def _extract_google_calendar_id(self, resource_uri: str) -> str:
        """Extract calendar ID from Google Calendar resource URI."""
        if not resource_uri:
            return "unknown"

        # Format: https://www.googleapis.com/calendar/v3/calendars/{calendarId}/events
        import re

        match = re.search(r"/calendars/([^/]+)/events", resource_uri)
        return match.group(1) if match else "unknown"

    def get_webhook_subscription(
        self, provider: str, external_subscription_id: str
    ) -> CalendarWebhookSubscription | None:
        """
        Get webhook subscription by provider and external subscription ID.
        """
        try:
            return CalendarWebhookSubscription.objects.get(
                organization=self.organization,
                provider=provider,
                external_subscription_id=external_subscription_id,
                is_active=True,
            )
        except CalendarWebhookSubscription.DoesNotExist:
            return None

    def update_subscription_last_notification(
        self, provider: str, external_subscription_id: str
    ) -> None:
        """
        Update the last notification timestamp for a subscription.
        """
        subscription = self.get_webhook_subscription(provider, external_subscription_id)
        if subscription:
            subscription.last_notification_at = timezone.now()
            subscription.save(update_fields=["last_notification_at"])
