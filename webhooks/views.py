from typing import Annotated

from dependency_injector.wiring import Provide, inject
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from common.utils.view_utils import ReadOnlyVintaScheduleModelViewSet, VintaScheduleModelViewSet
from webhooks.constants import WebhookStatus
from webhooks.models import WebhookConfiguration, WebhookEvent
from webhooks.serializers import WebhookConfigurationSerializer, WebhookEventSerializer
from webhooks.services import WebhookService


class WebhookConfigurationViewSet(VintaScheduleModelViewSet):
    """
    ViewSet for managing webhook configurations.
    Provides full CRUD operations: create, retrieve, update, partial_update, destroy, and list.
    """

    queryset = WebhookConfiguration.objects.all()
    serializer_class = WebhookConfigurationSerializer

    def get_queryset(self):
        """Filter configurations by current user's organization and exclude deleted ones."""
        user = self.request.user
        if hasattr(user, "organization_membership"):
            return self.queryset.filter(
                organization=user.organization_membership.organization, deleted_at__isnull=True
            )
        return self.queryset.none()

    @inject
    def perform_destroy(
        self,
        instance: WebhookConfiguration,
        webhook_service: Annotated["WebhookService | None", Provide["webhook_service"]] = None,
    ):
        """Handle the destruction of a webhook configuration."""
        if not webhook_service:
            raise ValueError("WebhookService wasn't injected. Please add it to the container.")

        webhook_service.delete_configuration(instance)


class WebhookEventViewSet(ReadOnlyVintaScheduleModelViewSet):
    """
    ViewSet for webhook events.
    Provides read-only operations (list, retrieve) and a custom retry action.
    """

    queryset = WebhookEvent.objects.all()
    serializer_class = WebhookEventSerializer

    @inject
    def __init__(
        self,
        *args,
        webhook_service: Annotated["WebhookService | None", Provide["webhook_service"]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.webhook_service = webhook_service

    def get_queryset(self):
        """Filter events by current user's organization."""
        user = self.request.user
        if hasattr(user, "organization_membership"):
            return (
                self.queryset.filter(organization=user.organization_membership.organization)
                .select_related("configuration", "main_event")
                .order_by("-created")
            )
        return self.queryset.none()

    @extend_schema(
        description="Retry a failed webhook event",
        request=None,
        responses={
            200: WebhookEventSerializer,
            400: {"description": "Bad request - event cannot be retried"},
            404: {"description": "Event not found"},
        },
    )
    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        """
        Retry a failed webhook event.
        Creates a new webhook event as a retry with exponential backoff.
        """
        event = self.get_object()

        # Check if the event can be retried
        if event.status not in [WebhookStatus.FAILED]:
            return Response(
                {"error": "Only failed events can be retried"}, status=status.HTTP_400_BAD_REQUEST
            )

        # Use skip_backoff=True for manual retries to allow immediate retry
        retry_event = self.webhook_service.schedule_event_retry(
            event=event,
            use_current_configuration=True,  # Use current config for manual retries
            is_manual=True,  # Skip exponential backoff for manual retries
        )

        if retry_event is None:
            return Response(
                {"error": "Maximum retry limit reached"}, status=status.HTTP_400_BAD_REQUEST
            )

        serializer = self.get_serializer(retry_event)
        return Response(serializer.data, status=status.HTTP_200_OK)
