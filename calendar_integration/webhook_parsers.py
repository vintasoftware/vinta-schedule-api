"""
Webhook payload parsing strategies for different calendar providers.
"""

import json
import logging
import re
from typing import Any

from calendar_integration.constants import CalendarProvider


logger = logging.getLogger(__name__)


class BaseWebhookParser:
    """Base class for provider-specific webhook parsers."""

    def parse(self, headers: dict[str, str], payload: dict[str, Any] | str) -> tuple[str, str]:
        """
        Parse webhook headers and payload to extract event information.

        Args:
            headers: HTTP headers from webhook request
            payload: Webhook payload (dict or string)

        Returns:
            Tuple of (event_type, external_calendar_id)
        """
        raise NotImplementedError


class GoogleWebhookParser(BaseWebhookParser):
    """Parser for Google Calendar webhooks."""

    def parse(self, headers: dict[str, str], payload: dict[str, Any] | str) -> tuple[str, str]:
        """Parse Google Calendar webhook to extract event info."""
        # Extract event type from headers
        event_type = headers.get("X-Goog-Resource-State", "unknown")

        # Extract calendar ID from resource URI
        resource_uri = headers.get("X-Goog-Resource-URI", "")
        if not resource_uri:
            logger.warning("Google webhook missing 'X-Goog-Resource-URI' header")
            calendar_id = "unknown"
        else:
            calendar_id = self._extract_google_calendar_id(resource_uri)
            if not calendar_id:
                logger.warning(
                    "Failed to extract Google calendar ID from malformed URI: %s", resource_uri
                )
                calendar_id = "unknown"

        logger.debug(
            "Parsed Google webhook: event_type=%s, calendar_id=%s", event_type, calendar_id
        )
        return event_type, calendar_id

    def _extract_google_calendar_id(self, resource_uri: str) -> str:
        """
        Extract calendar ID from Google Calendar resource URI.

        Args:
            resource_uri: Google Calendar resource URI

        Returns:
            Calendar ID or empty string if not found
        """
        if not resource_uri:
            return ""

        # Google Calendar resource URI format:
        # https://www.googleapis.com/calendar/v3/calendars/{calendarId}/events?alt=json
        match = re.search(r"/calendars/([^/]+)/events", resource_uri)
        return match[1] if match else ""


class MicrosoftWebhookParser(BaseWebhookParser):
    """Parser for Microsoft Graph webhooks."""

    def parse(self, headers: dict[str, str], payload: dict[str, Any] | str) -> tuple[str, str]:
        """Parse Microsoft Graph webhook to extract event info."""
        # For now, Microsoft webhooks default to notification type
        # In Phase 2, we'll implement proper payload parsing
        event_type = "notification"

        # Try to extract calendar ID from payload
        calendar_id = "unknown"
        try:
            if isinstance(payload, dict):
                # Look for calendar ID in various possible locations
                calendar_id = (
                    payload.get("calendar_id")
                    or payload.get("calendarId")
                    or payload.get("resource", {}).get("id", "unknown")
                )
            elif isinstance(payload, str):
                # Try to parse as JSON
                payload_dict = json.loads(payload)
                calendar_id = (
                    payload_dict.get("calendar_id")
                    or payload_dict.get("calendarId")
                    or payload_dict.get("resource", {}).get("id", "unknown")
                )
        except (json.JSONDecodeError, AttributeError, KeyError) as exc:
            logger.warning("Failed to parse Microsoft webhook payload: %s", exc)
            logger.debug("Raw payload: %s", payload)

        logger.debug(
            "Parsed Microsoft webhook: event_type=%s, calendar_id=%s", event_type, calendar_id
        )
        return event_type, calendar_id


# Registry of parsers by provider
WEBHOOK_PARSERS = {
    CalendarProvider.GOOGLE: GoogleWebhookParser(),
    CalendarProvider.MICROSOFT: MicrosoftWebhookParser(),
}
