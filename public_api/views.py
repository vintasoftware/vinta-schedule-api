import logging
from typing import Annotated

from django.db import IntegrityError, transaction

from dependency_injector.wiring import Provide, inject
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from organizations.models import get_active_organization_membership
from organizations.permissions import IsOrganizationAdmin
from public_api.models import ResourceAccess, SystemUser
from public_api.serializers import (
    SystemUserTokenCreateSerializer,
    SystemUserTokenResponseSerializer,
)
from public_api.services import PublicAPIAuthService


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

    @inject
    def __init__(
        self,
        *args,
        public_api_auth_service: Annotated[
            PublicAPIAuthService, Provide["public_api_auth_service"]
        ],
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.public_api_auth_service = public_api_auth_service

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
        membership = get_active_organization_membership(request.user)  # type: ignore[arg-type]
        if membership is None:
            # IsOrganizationAdmin.has_permission already guards this; defensive fallback.
            return Response(
                {"detail": "No active organisation membership."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = SystemUserTokenCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        integration_name: str = serializer.validated_data["integration_name"]
        available_resources: list[str] = serializer.validated_data["available_resources"]
        organization = membership.organization

        try:
            with transaction.atomic():
                system_user, plaintext_token = self.public_api_auth_service.create_system_user(
                    integration_name=integration_name,
                    organization=organization,
                )
        except IntegrityError:
            return Response(
                {
                    "integration_name": [
                        f"A token with integration_name '{integration_name}' already exists."
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Persist one ResourceAccess row per requested resource.
        for resource_name in available_resources:
            ResourceAccess.objects.create(system_user=system_user, resource_name=resource_name)

        # Build the response data: include the plaintext token ONCE via a pseudo-attribute.
        system_user.token = plaintext_token  # type: ignore[attr-defined]
        response_serializer = SystemUserTokenResponseSerializer(
            system_user, context={"request": request}
        )
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)
