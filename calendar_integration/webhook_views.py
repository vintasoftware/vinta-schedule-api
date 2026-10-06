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
from calendar_integration.services.calendar_webhook_service import (
    MicrosoftRoomWebhookService,
    RoomNotificationOutcome,
)


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

    This is not implemented yet.
    """

    @inject
    def post(
        self,
        request: HttpRequest,
        organization_id: int,
        calendar_service: Annotated[CalendarService, Provide["calendar_service"]],
    ) -> HttpResponse:
        """
        Handle Microsoft Graph webhook notifications.

        Args:
            request: HTTP request object
            organization_id: Organization ID from URL path
            calendar_service: Injected calendar service

        Returns:
        - 200: Webhook processed successfully or validation token returned
        - 400: Invalid webhook payload or validation token
        - 404: Organization not found
        - 500: Internal server error
        """
        logger.info(
            "Microsoft Calendar webhook received", extra={"organization_id": organization_id}
        )

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

        # Process Microsoft webhook notification
        try:
            logger.info(
                "Microsoft Calendar webhook received", extra={"organization_id": organization_id}
            )

            result = calendar_service.handle_webhook(CalendarProvider.MICROSOFT, request)

            # None result means notification was processed but no sync was needed
            if result is None:
                logger.info("Microsoft Calendar webhook processed successfully (no sync needed)")

            logger.info("Microsoft Calendar webhook processed successfully")
            return HttpResponse(status=200)

        except ValueError as e:
            # This handles organization not found errors and validation failures
            error_msg = str(e)
            if "Organization not found" in error_msg:
                logger.warning("Organization not found: %s", organization_id)
                return HttpResponse(status=404)
            logger.warning("Invalid Microsoft Calendar webhook: %s", error_msg)
            return HttpResponse(status=400)
        except WebhookProcessingFailedError as e:
            # This handles webhook validation errors
            logger.warning("Invalid Microsoft Calendar webhook: %s", str(e))
            return HttpResponse(status=400)
        except Exception as e:
            logger.exception("Error processing Microsoft Calendar webhook: %s", str(e))
            return HttpResponse(status=500)


#: Longest ``validationToken`` echoed back. Graph's tokens are far shorter.
MAX_VALIDATION_TOKEN_LENGTH = 1024

_ROOM_NOTIFICATION_STATUS = {
    RoomNotificationOutcome.ACCEPTED: 202,
    RoomNotificationOutcome.FORBIDDEN: 403,
    RoomNotificationOutcome.INVALID: 400,
}


@method_decorator(csrf_exempt, name="dispatch")
class MicrosoftRoomWebhookView(View):
    """Graph change notifications for Microsoft rooms synced with app-only credentials.

    Unauthenticated, like every provider webhook: a notification is trusted only when
    it carries the ``clientState`` stored for its subscription. This is a plain
    ``View``, so it binds no organization; the service reads the subscriptions with
    ``filter_by_organization`` on the URL's organization id.
    """

    @inject
    def post(
        self,
        request: HttpRequest,
        organization_id: int,
        microsoft_room_webhook_service: Annotated[
            MicrosoftRoomWebhookService, Provide["microsoft_room_webhook_service"]
        ],
    ) -> HttpResponse:
        """Answer the subscription handshake, or enqueue a sync per notified room.

        Returns:
        - 200 with the ``validationToken``, in plain text, while Graph creates a
          subscription;
        - 202 when the notifications were accepted (unknown subscriptions ignored);
        - 403 when a notification's ``clientState`` does not match;
        - 400 when the body is not a Graph notification payload.
        """
        validation_token = request.GET.get("validationToken")
        if validation_token is not None:
            if not validation_token or len(validation_token) > MAX_VALIDATION_TOKEN_LENGTH:
                return HttpResponse(status=400)
            # Graph requires the token back unchanged; plain text is never rendered.
            return HttpResponse(validation_token, content_type="text/plain")

        outcome = microsoft_room_webhook_service.handle_room_notifications(
            organization_id, request.body
        )
        return HttpResponse(status=_ROOM_NOTIFICATION_STATUS[outcome])
