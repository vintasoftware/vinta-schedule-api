import logging
from typing import Annotated

from django.http import HttpRequest, HttpResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from dependency_injector.wiring import Provide, inject

from calendar_integration.services.calendar_service import CalendarService
from organizations.models import Organization


logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class GoogleCalendarWebhookView(View):
    """
    Webhook endpoint for Google Calendar notifications.

    Handles incoming webhook notifications from Google Calendar and triggers
    calendar synchronization using the existing CalendarService infrastructure.
    """

    @inject
    def post(
        self,
        request: HttpRequest,
        calendar_service: Annotated[CalendarService, Provide["calendar_service"]],
    ) -> HttpResponse:
        """
        Handle Google Calendar webhook notifications.

        Expected headers:
        - X-Goog-Channel-ID: Channel ID for the subscription
        - X-Goog-Resource-State: State of the resource (sync, exists, not_exists)
        - X-Goog-Resource-ID: Resource ID
        - X-Goog-Resource-URI: Resource URI

        Returns:
        - 200: Webhook processed successfully
        - 400: Invalid webhook payload
        - 500: Internal server error
        """
        try:
            # Extract webhook headers
            headers = {
                "X-Goog-Channel-ID": request.headers.get("X-Goog-Channel-ID", ""),
                "X-Goog-Resource-ID": request.headers.get("X-Goog-Resource-ID", ""),
                "X-Goog-Resource-URI": request.headers.get("X-Goog-Resource-URI", ""),
                "X-Goog-Resource-State": request.headers.get("X-Goog-Resource-State", ""),
                "X-Goog-Channel-Token": request.headers.get("X-Goog-Channel-Token", ""),
            }

            # Log incoming webhook for debugging
            logger.info(
                "Google Calendar webhook received",
                extra={
                    "headers": headers,
                    "channel_id": headers.get("X-Goog-Channel-ID"),
                    "resource_state": headers.get("X-Goog-Resource-State"),
                },
            )

            # Skip 'sync' state notifications (initial subscription verification)
            resource_state = headers.get("X-Goog-Resource-State")
            if resource_state == "sync":
                logger.info("Received Google Calendar sync notification, acknowledging")
                return HttpResponse(status=200)

            # Find organization from channel ID or resource URI
            # For now, we'll extract from the channel ID pattern or use a default
            # In production, you might want to encode organization info in the callback URL
            organization = self._get_organization_from_webhook(headers)
            if not organization:
                logger.warning("Could not determine organization from webhook headers")
                return HttpResponse(status=400)

            # Initialize calendar service for the organization
            if not calendar_service:
                logger.error("Calendar service not available")
                return HttpResponse(status=500)

            # Set up service with organization context (without authentication for webhook processing)
            calendar_service.organization = organization

            # Process webhook notification
            result = calendar_service.process_webhook_notification(
                provider="google",
                headers=headers,
                payload=None,  # Google Calendar webhooks don't have body payload
            )

            logger.info(
                "Google Calendar webhook processed successfully",
                extra={
                    "webhook_event_id": result.id if hasattr(result, "id") else None,
                    "organization_id": organization.id,
                },
            )

            return HttpResponse(status=200)

        except ValueError as e:
            logger.warning("Invalid Google Calendar webhook: %s", e)
            return HttpResponse(status=400)
        except Exception as e:
            logger.exception("Error processing Google Calendar webhook: %s", e)
            return HttpResponse(status=500)

    def _get_organization_from_webhook(self, headers: dict[str, str]) -> Organization | None:
        """
        Extract organization from webhook headers.

        In production, you might encode organization ID in the callback URL or channel token.
        For now, this is a placeholder that returns the first organization.

        Args:
            headers: Webhook headers

        Returns:
            Organization instance or None if not found
        """
        # TODO: Implement proper organization detection
        # This could be done by:
        # 1. Encoding organization ID in the callback URL path
        # 2. Storing organization mapping in the channel token
        # 3. Looking up subscription by channel ID in CalendarWebhookSubscription model

        channel_id = headers.get("X-Goog-Channel-ID")
        if not channel_id:
            return None

        # Try to find organization from existing webhook subscription
        from calendar_integration.models import CalendarWebhookSubscription

        try:
            # Use original_manager to bypass organization filtering
            subscription = CalendarWebhookSubscription.original_manager.select_related(
                "organization"
            ).get(
                channel_id=channel_id,
                is_active=True,
            )
            return subscription.organization
        except CalendarWebhookSubscription.DoesNotExist:
            pass

        # Fallback: return first organization (for development/testing)
        try:
            return Organization.objects.first()
        except Organization.DoesNotExist:
            return None


@method_decorator(csrf_exempt, name="dispatch")
class MicrosoftCalendarWebhookView(View):
    """
    Webhook endpoint for Microsoft Calendar notifications.

    This will be implemented in Phase 3 of the plan.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        """Handle Microsoft Graph webhook notifications."""
        # Check for validation token (subscription setup)
        validation_token = request.GET.get("validationToken")
        if validation_token:
            # Return validation token as plain text for subscription verification
            return HttpResponse(validation_token, content_type="text/plain")

        # TODO: Implement Microsoft webhook processing in Phase 3
        logger.info("Microsoft Calendar webhook received (not yet implemented)")
        return HttpResponse(status=200)
