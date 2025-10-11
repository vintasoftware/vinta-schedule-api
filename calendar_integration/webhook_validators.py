"""
Webhook validation strategies for different calendar providers.
"""

import logging
from typing import ClassVar

from calendar_integration.constants import CalendarProvider
from calendar_integration.exceptions import WebhookAuthenticationError


logger = logging.getLogger(__name__)


class BaseWebhookValidator:
    """Base class for provider-specific webhook validators."""

    def validate(
        self,
        headers: dict[str, str],
        body: bytes,
        subscription_id: str | None = None,
        organization_id: int | None = None,
    ) -> bool:
        """
        Validate webhook signature and headers.

        Args:
            headers: HTTP headers from webhook request
            body: Raw request body
            subscription_id: Optional subscription ID for lookup
            organization_id: Organization ID for subscription lookup

        Returns:
            True if validation passes

        Raises:
            WebhookAuthenticationError: If validation fails
        """
        raise NotImplementedError


class GoogleWebhookValidator(BaseWebhookValidator):
    """Validator for Google Calendar webhooks."""

    REQUIRED_HEADERS: ClassVar[list[str]] = [
        "X-Goog-Channel-ID",
        "X-Goog-Resource-ID",
        "X-Goog-Resource-State",
    ]

    def validate(
        self,
        headers: dict[str, str],
        body: bytes,
        subscription_id: str | None = None,
        organization_id: int | None = None,
    ) -> bool:
        """Validate Google Calendar webhook headers."""
        # Normalize header keys to lower-case for case-insensitive validation
        normalized_headers = {k.lower(): v for k, v in headers.items()}
        for header in self.REQUIRED_HEADERS:
            if header.lower() not in normalized_headers:
                logger.warning("Google webhook missing required header: %s", header)
                raise WebhookAuthenticationError(f"Missing Google header: {header}")

        # Additional validation could be added here (e.g., signature verification)
        logger.debug("Google webhook validation successful")
        return True


class MicrosoftWebhookValidator(BaseWebhookValidator):
    """Validator for Microsoft Graph webhooks."""

    def validate(
        self,
        headers: dict[str, str],
        body: bytes,
        subscription_id: str | None = None,
        organization_id: int | None = None,
    ) -> bool:
        """Validate Microsoft Graph webhook."""
        # Normalize header keys to handle case variations
        normalized_headers = {k.lower(): v for k, v in headers.items()}

        # For validation requests, check for validation token
        if validation_token := normalized_headers.get("validationtoken"):
            logger.debug("Microsoft webhook validation token found: %s", validation_token)
            return True

        # For actual notifications, validate subscription exists and is active
        if subscription_id and organization_id:
            from calendar_integration.models import CalendarWebhookSubscription

            if not CalendarWebhookSubscription.objects.filter(
                organization_id=organization_id,
                external_subscription_id=subscription_id,
                provider=CalendarProvider.MICROSOFT,
                is_active=True,
            ).exists():
                logger.warning(
                    "Microsoft webhook subscription not found: %s for org %s",
                    subscription_id,
                    organization_id,
                )
                raise WebhookAuthenticationError(f"Unknown subscription: {subscription_id}")

        logger.debug("Microsoft webhook validation successful")
        return True


# Registry of validators by provider
WEBHOOK_VALIDATORS = {
    CalendarProvider.GOOGLE: GoogleWebhookValidator(),
    CalendarProvider.MICROSOFT: MicrosoftWebhookValidator(),
}
