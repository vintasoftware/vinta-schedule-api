from typing import Annotated

from dependency_injector.wiring import Provide, inject
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from calendar_integration.services.google_write_access_service import GoogleWriteAccessService
from common.feature_flags import RESOURCE_CALENDAR_PROVIDER_SYNC, is_enabled
from common.utils.view_utils import TenantScopedViewMixin
from organizations.permissions import IsOrganizationAdmin


class GoogleWriteAccessResultSerializer(serializers.Serializer):
    """Response body of ``POST /calendar/google-service-account/verify-write-access/``."""

    write_enabled = serializers.BooleanField()
    write_verified_at = serializers.DateTimeField(allow_null=True)
    error = serializers.CharField(allow_blank=True)


class GoogleServiceAccountWriteAccessViewSet(TenantScopedViewMixin, GenericViewSet):
    """Org admins verify that the organization's Google service account can write rooms.

    Returns 404 while the ``resource_calendar_provider_sync`` flag is off for the
    organization, so the surface is not advertised.
    """

    permission_classes = (IsOrganizationAdmin,)
    serializer_class = GoogleWriteAccessResultSerializer

    @extend_schema(
        summary="Verify Google room write access",
        description=(
            "Builds a client for the organization-level Google service account with the "
            "admin.directory.resource.calendar scope and makes one Directory call. On "
            "success write_enabled is set; otherwise error says how to fix it. Admin only."
        ),
        request=None,
        responses={
            200: GoogleWriteAccessResultSerializer,
            404: OpenApiResponse(description="Room provider sync is off for this organization."),
        },
    )
    @action(
        methods=["post"],
        detail=False,
        url_path="verify-write-access",
        url_name="verify-write-access",
    )
    @inject
    def verify_write_access(
        self,
        request: Request,
        google_write_access_service: Annotated[
            GoogleWriteAccessService, Provide["google_write_access_service"]
        ] = None,  # type: ignore[assignment]
    ) -> Response:
        """POST /calendar/google-service-account/verify-write-access/."""
        organization = request.organization  # type: ignore[attr-defined]
        if not is_enabled(RESOURCE_CALENDAR_PROVIDER_SYNC, organization.id):
            raise NotFound()

        result = google_write_access_service.verify(organization)
        return Response(
            GoogleWriteAccessResultSerializer(result).data,
            status=status.HTTP_200_OK,
        )
