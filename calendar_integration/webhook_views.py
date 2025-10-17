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
        organization_id: int,
        calendar_service: Annotated[CalendarService, Provide["calendar_service"]],
    ) -> HttpResponse:
        """
        Handle Google Calendar webhook notifications.

        Args:
            request: HTTP request object
            organization_id: Organization ID from URL path
            calendar_service: Injected calendar service

        Expected headers:
        - X-Goog-Channel-ID: Channel ID for the subscription
        - X-Goog-Resource-State: State of the resource (sync, exists, not_exists)
        - X-Goog-Resource-ID: Resource ID
        - X-Goog-Resource-URI: Resource URI

        Returns:
        - 200: Webhook processed successfully
        - 400: Invalid webhook payload
        - 404: Organization not found
        - 500: Internal server error
        """
        try:
            logger.info("Google Calendar webhook received")

            # Get organization from URL parameter
            try:
                organization = Organization.objects.get(id=organization_id)
            except Organization.DoesNotExist:
                logger.warning("Organization not found: %s", organization_id)
                return HttpResponse(status=404)

            # Extract webhook headers
            headers = {
                "X-Goog-Channel-ID": request.headers.get("X-Goog-Channel-ID", ""),
                "X-Goog-Resource-State": request.headers.get("X-Goog-Resource-State", ""),
                "X-Goog-Resource-ID": request.headers.get("X-Goog-Resource-ID", ""),
                "X-Goog-Resource-URI": request.headers.get("X-Goog-Resource-URI", ""),
                "X-Goog-Channel-Token": request.headers.get("X-Goog-Channel-Token", ""),
            }

            # Log incoming webhook for debugging
            logger.info(
                "Google Calendar webhook received",
                extra={
                    "headers": headers,
                    "channel_id": headers.get("X-Goog-Channel-ID"),
                    "resource_state": headers.get("X-Goog-Resource-State"),
                    "organization_id": organization_id,
                },
            )

            # Skip 'sync' state notifications (initial subscription verification)
            resource_state = headers.get("X-Goog-Resource-State")
            if resource_state == "sync":
                logger.info("Received Google Calendar sync notification, acknowledging")
                return HttpResponse(status=200)

            # Set organization context on the service
            calendar_service.organization = organization

            # Process the webhook notification
            calendar_service.process_webhook_notification(
                provider="google",
                headers=headers,
            )

            logger.info("Google Calendar webhook processed successfully")
            return HttpResponse(status=200)

        except ValueError as e:
            logger.warning("Invalid Google Calendar webhook: %s", str(e))
            return HttpResponse(status=400)
        except Exception as e:
            logger.exception("Error processing Google Calendar webhook: %s", str(e))
            return HttpResponse(status=500)


@method_decorator(csrf_exempt, name="dispatch")
class MicrosoftCalendarWebhookView(View):
    """
    Webhook endpoint for Microsoft Calendar notifications.

    This will be implemented in Phase 3 of the plan.
    """

    def post(self, request: HttpRequest, organization_id: int) -> HttpResponse:
        """
        Handle Microsoft Graph webhook notifications.

        Args:
            request: HTTP request object
            organization_id: Organization ID from URL path
        """
        # Get organization from URL parameter
        try:
            organization = Organization.objects.get(id=organization_id)  # noqa: F841
        except Organization.DoesNotExist:
            logger.warning("Organization not found: %s", organization_id)
            return HttpResponse(status=404)

        # Check for validation token (subscription setup)
        validation_token = request.GET.get("validationToken")
        if validation_token:
            # Return validation token as plain text for subscription verification
            return HttpResponse(validation_token, content_type="text/plain")

        # TODO: Implement Microsoft webhook processing in Phase 3
        logger.info(
            "Microsoft Calendar webhook received (not yet implemented)",
            extra={"organization_id": organization_id},
        )
        return HttpResponse(status=200)
