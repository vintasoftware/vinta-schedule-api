import logging
from typing import Any

from django.utils import timezone

from calendar_integration.constants import CalendarProvider, IncomingWebhookProcessingStatus
from calendar_integration.exceptions import WebhookAuthenticationError, WebhookValidationError
from calendar_integration.models import CalendarWebhookEvent, CalendarWebhookSubscription
from calendar_integration.webhook_parsers import WEBHOOK_PARSERS
from calendar_integration.webhook_validators import WEBHOOK_VALIDATORS
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
        provider: CalendarProvider,
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
        validator = WEBHOOK_VALIDATORS.get(provider)
        if not validator:
            raise WebhookValidationError(f"Unsupported webhook provider: {provider}")

        return validator.validate(headers, body, subscription_id, self.organization.id)

    def process_webhook_notification(
        self,
        provider: CalendarProvider,
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

        # Validate the webhook signature before parsing event data
        try:
            self.validate_webhook_signature(provider, headers, b"", None)
        except (WebhookAuthenticationError, WebhookValidationError) as e:
            logger.error("Webhook validation failed: %s", e)
            raise

        # Parse event information after successful validation
        event_type, external_calendar_id = self._parse_webhook_content(provider, headers, payload)

        # Look up associated subscription and set subscription field
        subscription = None
        if provider == CalendarProvider.GOOGLE:
            channel_id = headers.get("X-Goog-Channel-ID")
            if channel_id:
                subscription = self._get_subscription_by_channel_id(channel_id)
        elif provider == CalendarProvider.MICROSOFT:
            # For Microsoft, we would extract subscription_id from headers or payload
            # This is a placeholder for Phase 2 implementation
            pass

        # Create webhook event record with parsed values and subscription
        webhook_event = CalendarWebhookEvent.objects.create(
            organization=self.organization,
            provider=provider,
            event_type=event_type,
            external_calendar_id=external_calendar_id,
            raw_payload=payload if isinstance(payload, dict) else {"raw": str(payload)},
            headers=headers,
            processing_status=IncomingWebhookProcessingStatus.PENDING,
            subscription=subscription,
        )

        # Process the webhook - for now, just mark as processed
        # TODO: Implement webhook processing logic in Phase 2
        try:
            self._process_webhook_content(webhook_event)
            logger.info(
                "Processed webhook for provider %s, calendar %s", provider, external_calendar_id
            )
            return webhook_event

        except Exception as e:
            webhook_event.processing_status = IncomingWebhookProcessingStatus.FAILED
            webhook_event.processed_at = timezone.now()
            webhook_event.save()
            logger.exception("Failed to process webhook: %s", e)
            raise

    def _parse_webhook_content(
        self, provider: CalendarProvider, headers: dict[str, str], payload: dict[str, Any] | str
    ) -> tuple[str, str]:
        """Parse webhook headers and payload to extract event information."""
        parser = WEBHOOK_PARSERS.get(provider)
        if not parser:
            logger.warning("No parser available for provider: %s", provider)
            return "unknown", "unknown"

        try:
            return parser.parse(headers, payload)
        except (ValueError, KeyError, AttributeError) as exc:
            logger.error("Error parsing webhook content: %s", exc)
            logger.info("Raw payload: %s", payload)
            logger.info("Headers: %s", headers)
            return "unknown", "unknown"

    def _get_subscription_by_channel_id(
        self, channel_id: str
    ) -> CalendarWebhookSubscription | None:
        """Get webhook subscription by Google Calendar channel ID."""
        try:
            return CalendarWebhookSubscription.objects.get(
                organization=self.organization,
                provider=CalendarProvider.GOOGLE,
                channel_id=channel_id,
                is_active=True,
            )
        except CalendarWebhookSubscription.DoesNotExist:
            logger.warning("No subscription found for channel ID: %s", channel_id)
            return None

    def _process_webhook_content(self, webhook_event: CalendarWebhookEvent) -> None:
        """Process webhook content and update event status."""
        # Mark as processed for now - actual processing logic will be in Phase 2
        webhook_event.processing_status = IncomingWebhookProcessingStatus.PROCESSED
        webhook_event.processed_at = timezone.now()
        webhook_event.save()

    def get_webhook_subscription(
        self, provider: CalendarProvider, external_subscription_id: str
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
        self, provider: CalendarProvider, external_subscription_id: str
    ) -> None:
        """
        Update the last notification timestamp for a subscription.
        """
        if subscription := self.get_webhook_subscription(provider, external_subscription_id):
            subscription.last_notification_at = timezone.now()
            subscription.save(update_fields=["last_notification_at"])
        else:
            logger.warning(
                "No subscription found for provider '%s' and external_subscription_id '%s' when updating last notification",
                provider,
                external_subscription_id,
            )
