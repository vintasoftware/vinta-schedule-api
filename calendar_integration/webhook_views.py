import logging
import re
from typing import Annotated

from django.http import HttpRequest, HttpResponse
from django.utils.decorators import method_decorator
from django.utils.html import escape
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from dependency_injector.wiring import Provide, inject

from calendar_integration.constants import CalendarProvider
from calendar_integration.exceptions import WebhookProcessingFailedError
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

        Returns:
        - 200: Webhook processed successfully
        - 400: Invalid webhook payload
        - 404: Organization not found
        - 500: Internal server error
        """
        try:
            logger.info(
                "Google Calendar webhook received", extra={"organization_id": organization_id}
            )

            result = calendar_service.handle_webhook(CalendarProvider.GOOGLE, request)

            # None result means sync notification was skipped
            if result is None:
                logger.info("Received Google Calendar sync notification, acknowledging")

            logger.info("Google Calendar webhook processed successfully")
            return HttpResponse(status=200)

        except ValueError as e:
            # This handles organization not found errors
            error_msg = str(e)
            if "Organization not found" in error_msg:
                logger.warning("Organization not found: %s", organization_id)
                return HttpResponse(status=404)
            logger.warning("Invalid Google Calendar webhook: %s", error_msg)
            return HttpResponse(status=400)
        except WebhookProcessingFailedError as e:
            # This handles webhook validation errors
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
            # Sanitize validation token to prevent XSS attacks
            # Microsoft validation tokens are UUIDs, so we can validate the format
            if re.match(
                r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
                validation_token,
                re.IGNORECASE,
            ):
                # Escape the validation token to prevent any potential XSS
                # Even though it's validated as UUID format, we escape for security
                escaped_token = escape(validation_token)
                return HttpResponse(escaped_token, content_type="text/plain")
            else:
                logger.warning(
                    "Invalid validation token format received",
                    extra={"organization_id": organization_id},
                )
                return HttpResponse(status=400)

        # TODO: Implement Microsoft webhook processing in Phase 3
        logger.info(
            "Microsoft Calendar webhook received (not yet implemented)",
            extra={"organization_id": organization_id},
        )
        return HttpResponse(status=200)
