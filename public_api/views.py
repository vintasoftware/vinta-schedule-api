import logging

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from organizations.models import get_active_organization_membership
from organizations.permissions import IsOrganizationAdmin
from public_api.models import SystemUser
from public_api.serializers import (
    SystemUserTokenCreateSerializer,
    SystemUserTokenResponseSerializer,
    SystemUserTokenSerializer,
)


logger = logging.getLogger(__name__)


class SystemUserTokenViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    """Admin-only viewset for managing public-API tokens (SystemUser + ResourceAccess rows).

    Phase 12: create only.  Phase 13: list + retrieve.
    List / retrieve / revoke / edit-grants are supported phases.

    ``POST /public-api-tokens/`` creates a new ``SystemUser`` for the caller's
    organisation, persists the requested ``ResourceAccess`` rows, and returns the
    plaintext token **once**.  The token is never recoverable after this response.

    ``GET /public-api-tokens/`` lists the caller's org tokens without secrets.
    ``GET /public-api-tokens/{id}/`` retrieves a single token without secrets.
    """

    permission_classes = (IsOrganizationAdmin,)
    serializer_class = SystemUserTokenCreateSerializer

    def get_queryset(self):  # type: ignore[override]
        """Org-scoped queryset with prefetched ResourceAccess rows for list/retrieve.

        Prefetches available_resources (related_name on ResourceAccess.system_user FK)
        to avoid N+1 queries when serializing available_resources.
        """
        user = self.request.user
        if not user.is_authenticated:
            return SystemUser.objects.none()
        membership = get_active_organization_membership(user)
        if membership:
            return SystemUser.objects.filter(
                organization_id=membership.organization_id
            ).prefetch_related("available_resources")
        return SystemUser.objects.none()

    def get_serializer_class(self):  # type: ignore[override]
        """Use SystemUserTokenSerializer for list/retrieve; create-response uses SystemUserTokenResponseSerializer."""
        if self.action == "create":
            return SystemUserTokenCreateSerializer
        return SystemUserTokenSerializer

    @extend_schema(
        request=SystemUserTokenCreateSerializer,
        responses={201: SystemUserTokenResponseSerializer},
    )
    def create(self, request: Request, *args, **kwargs) -> Response:
        """Create a SystemUser and ResourceAccess rows; return the plaintext token once.

        Returns HTTP 201 on success.  The response body includes ``id``,
        ``integration_name``, ``is_active``, ``available_resources``, and a
        write-once ``token`` field — never ``long_lived_token_hash``.

        HTTP 400 is returned for:
        - Invalid or unknown ``available_resources`` values.
        - Empty ``available_resources`` list.
        - Duplicate ``integration_name`` (unique constraint).

        HTTP 403 is returned for non-admin callers; HTTP 401 for unauthenticated.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        system_user = serializer.save()

        response_serializer = SystemUserTokenResponseSerializer(
            system_user, context={"request": request}
        )
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    @extend_schema(responses={200: SystemUserTokenSerializer})
    @action(detail=True, methods=["post"], url_path="revoke")
    def revoke(self, request: Request, pk=None) -> Response:
        """Revoke a public-API token by setting its SystemUser.is_active to False.

        The token will no longer authenticate requests via check_system_user_token.
        This is idempotent: revoking an already-revoked token is a 200 no-op.

        Returns HTTP 200 with the updated token serialized via SystemUserTokenSerializer.
        HTTP 403 is returned for non-admin callers; HTTP 404 if the token does not
        exist or belongs to another organization; HTTP 401 for unauthenticated.
        """
        system_user = self.get_object()
        system_user.is_active = False
        system_user.save(update_fields=["is_active"])

        serializer = SystemUserTokenSerializer(system_user, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)
