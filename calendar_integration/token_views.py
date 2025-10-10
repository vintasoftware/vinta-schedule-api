"""
Token-based ViewSets for calendar event management.
"""

import base64
from typing import Annotated

from dependency_injector.wiring import Provide, inject
from rest_framework.exceptions import PermissionDenied
from rest_framework.request import Request

from calendar_integration.exceptions import InvalidTokenError
from calendar_integration.models import CalendarEvent, CalendarManagementToken
from calendar_integration.serializers import CalendarEventSerializer
from calendar_integration.services.calendar_service import CalendarService
from common.utils.view_utils import NoListVintaScheduleModelViewSet
from organizations.models import Organization


class TokenAuthenticationMixin:
    """
    Mixin that provides token-based authentication for calendar management.
    Expects the class using this mixin to have calendar_service attribute.
    """

    calendar_service: "CalendarService | None"

    def extract_token_from_header(self, request: Request) -> tuple[str, int]:
        """
        Extract and validate basic token format from Authorization header.
        Returns (token_str_base64, organization_id) from the URL path.
        """
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            raise PermissionDenied(
                "Missing or invalid Authorization header. Expected: Bearer <token>"
            )

        token_str_base64 = auth_header[7:]  # Remove 'Bearer ' prefix
        if not token_str_base64:
            raise PermissionDenied("Missing token in Authorization header")

        # Extract organization_id from URL path
        if not request.resolver_match or not request.resolver_match.kwargs:
            raise PermissionDenied("Organization ID not found in URL path")

        organization_id = request.resolver_match.kwargs.get("organization_id")
        if not organization_id:
            raise PermissionDenied("Organization ID not found in URL path")

        return token_str_base64, int(organization_id)

    def get_calendar_service(self, request: Request, organization) -> "CalendarService":
        """
        Initialize and return a CalendarService with the token from the request.
        """
        if not self.calendar_service:
            raise ValueError("Calendar service not available")

        try:
            token_str_base64, _ = self.extract_token_from_header(request)
        except PermissionDenied:
            # Re-raise PermissionDenied as-is
            raise

        calendar_service = self.calendar_service
        calendar_service.initialize_without_provider(
            user_or_token=token_str_base64, organization=organization
        )
        return calendar_service


class TokenCalendarEventViewSet(TokenAuthenticationMixin, NoListVintaScheduleModelViewSet):
    """
    ViewSet for calendar event management using management tokens.
    """

    serializer_class = CalendarEventSerializer
    queryset = CalendarEvent.objects.all()
    authentication_classes = tuple()  # Disable default authentication - we handle it manually
    permission_classes = tuple()  # Disable default permissions - we handle it manually

    @inject
    def __init__(
        self,
        calendar_service: Annotated["CalendarService | None", Provide["calendar_service"]] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.calendar_service = calendar_service

    def get_serializer_context(self):
        """
        Provide serializer context with token information for authentication.
        """
        context = super().get_serializer_context()

        # Only try to extract token if there's a proper Authorization header
        auth_header = self.request.headers.get("authorization", "")
        if auth_header.startswith("Bearer ") and len(auth_header) > 7:
            try:
                organization_id = self.kwargs.get("organization_id")
                if not organization_id:
                    return context

                organization = Organization.objects.get(id=organization_id)

                # Get the token and extract the token object
                token_str_base64, _ = self.extract_token_from_header(self.request)
                decoded_token = base64.b64decode(token_str_base64).decode()
                token_id, _ = decoded_token.split(":", 1)

                # Get the token object with organization filter
                token = CalendarManagementToken.objects.filter_by_organization(organization.id).get(
                    id=token_id
                )

                # Check if token is revoked
                if token.revoked_at is not None:
                    return context  # Don't include token info for revoked tokens

                # Pass token string and organization to serializer context
                context["token"] = token
                context[
                    "token_str_base64"
                ] = token_str_base64  # For calendar service authentication
                context["organization"] = organization

            except (
                ValueError,
                CalendarManagementToken.DoesNotExist,
                Organization.DoesNotExist,
                UnicodeDecodeError,
                PermissionDenied,
            ):
                # If token is invalid, silently continue without token context
                # The individual HTTP methods will handle authentication and return proper error responses
                pass

        return context

    def get_queryset(self):
        """
        Filter events based on organization from the URL.
        """
        organization_id = self.kwargs.get("organization_id")
        if organization_id:
            return CalendarEvent.objects.filter_by_organization(organization_id)
        return CalendarEvent.objects.none()

    def create(self, request, *args, **kwargs):
        """
        Create an event with token-based authentication checked first.
        """
        try:
            # Check authentication before any serializer validation
            organization_id = self.kwargs.get("organization_id")
            if not organization_id:
                raise PermissionDenied("Organization ID not found in URL path")

            organization = Organization.objects.get(id=organization_id)
            self.get_calendar_service(request, organization)

            # If we get here, authentication is valid, proceed with normal create flow
            return super().create(request, *args, **kwargs)
        except (PermissionDenied, Organization.DoesNotExist) as e:
            raise PermissionDenied(str(e)) from e

    def update(self, request, *args, **kwargs):
        """
        Update an event with token-based authentication checked first.
        """
        try:
            # Check authentication before any serializer validation
            instance = self.get_object()
            self.get_calendar_service(request, instance.organization)

            # If we get here, authentication is valid, proceed with normal update flow
            return super().update(request, *args, **kwargs)
        except PermissionDenied as e:
            raise PermissionDenied(str(e)) from e

    def partial_update(self, request, *args, **kwargs):
        """
        Partial update an event with token-based authentication checked first.
        """
        try:
            # Check authentication before any serializer validation
            instance = self.get_object()
            self.get_calendar_service(request, instance.organization)

            # If we get here, authentication is valid, proceed with normal partial update flow
            return super().partial_update(request, *args, **kwargs)
        except PermissionDenied as e:
            raise PermissionDenied(str(e)) from e

    def destroy(self, request, *args, **kwargs):
        """
        Delete an event with token-based authentication checked first.
        """
        try:
            # Check authentication before any other processing
            instance = self.get_object()
            self.get_calendar_service(request, instance.organization)

            # If we get here, authentication is valid, proceed with normal destroy flow
            return super().destroy(request, *args, **kwargs)
        except PermissionDenied as e:
            raise PermissionDenied(str(e)) from e

    def list(self, request, *args, **kwargs):  # noqa: A003
        """
        List events with token-based authentication.
        """
        try:
            organization_id = self.kwargs.get("organization_id")
            if not organization_id:
                raise PermissionDenied("Organization ID not found in URL path")

            organization = Organization.objects.get(id=organization_id)
            self.get_calendar_service(request, organization)

            return super().list(request, *args, **kwargs)
        except (PermissionDenied, Organization.DoesNotExist) as e:
            raise PermissionDenied(str(e)) from e

    def retrieve(self, request, *args, **kwargs):
        """
        Retrieve a single event with token-based authentication.
        """
        try:
            instance = self.get_object()
            self.get_calendar_service(request, instance.organization)

            return super().retrieve(request, *args, **kwargs)
        except PermissionDenied as e:
            raise PermissionDenied(str(e)) from e

    def perform_create(self, serializer):
        """
        Create an event with token-based permissions.
        CalendarService will handle permission validation and raise PermissionDenied if unauthorized.
        """
        try:
            # Extract organization from URL
            organization_id = self.kwargs.get("organization_id")
            if not organization_id:
                raise InvalidTokenError("Organization ID not found in URL path")

            organization = Organization.objects.get(id=organization_id)

            # Initialize calendar service with token - this will validate permissions
            self.get_calendar_service(self.request, organization)

            # The calendar service is now initialized and will handle permissions
            # Let the serializer handle the creation
            serializer.save()

        except (InvalidTokenError, PermissionDenied) as e:
            raise PermissionDenied(str(e)) from e
        except Organization.DoesNotExist as e:
            raise PermissionDenied("Invalid organization") from e

    def perform_update(self, serializer):
        """
        Update an event with token-based permissions.
        CalendarService will handle permission validation and raise PermissionDenied if unauthorized.
        """
        try:
            # Initialize calendar service with token - this will validate permissions
            self.get_calendar_service(self.request, serializer.instance.organization)

            # The calendar service is now initialized and will handle permissions
            # Let the serializer handle the update
            serializer.save()

        except (InvalidTokenError, PermissionDenied) as e:
            raise PermissionDenied(str(e)) from e

    def perform_destroy(self, instance):
        """
        Delete an event with token-based permissions.
        CalendarService will handle permission validation and raise PermissionDenied if unauthorized.
        """
        try:
            # Initialize calendar service with token - this will validate permissions
            calendar_service = self.get_calendar_service(self.request, instance.organization)

            # Use calendar service to delete the event - it will handle permissions
            delete_series = (
                self.request.query_params.get("delete_series", "false").lower() == "true"
            )
            calendar_service.delete_event(
                calendar_id=instance.calendar_fk_id,
                event_id=instance.id,
                delete_series=delete_series,
            )

        except (InvalidTokenError, PermissionDenied) as e:
            raise PermissionDenied(str(e)) from e
