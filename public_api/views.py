import logging

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from organizations.models import get_active_organization_membership
from organizations.permissions import IsOrganizationAdmin
from public_api.models import SystemUser
from public_api.serializers import (
    SystemUserTokenCreateSerializer,
    SystemUserTokenResponseSerializer,
)


logger = logging.getLogger(__name__)


class SystemUserTokenViewSet(GenericViewSet):
    """Admin-only viewset for creating public-API tokens (SystemUser + ResourceAccess rows).

    Phase 12: create only.  List / retrieve / revoke / edit-grants are future phases.

    ``POST /public-api-tokens/`` creates a new ``SystemUser`` for the caller's
    organisation, persists the requested ``ResourceAccess`` rows, and returns the
    plaintext token **once**.  The token is never recoverable after this response.
    """

    permission_classes = (IsOrganizationAdmin,)
    serializer_class = SystemUserTokenCreateSerializer

    def get_queryset(self):  # type: ignore[override]
        """Org-scoped queryset — used for router introspection."""
        user = self.request.user
        if not user.is_authenticated:
            return SystemUser.objects.none()
        membership = get_active_organization_membership(user)
        if membership:
            return SystemUser.objects.filter(organization_id=membership.organization_id)
        return SystemUser.objects.none()

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
