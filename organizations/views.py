from typing import Annotated

from dependency_injector.wiring import Provide, inject
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import generics, status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.utils.view_utils import (
    NoListVintaScheduleModelViewSet,
    NoUpdateVintaScheduleModelViewSet,
)
from organizations.exceptions import (
    DuplicateInvitationError,
    InvalidInvitationTokenError,
    InvitationNotFoundError,
    UserAlreadyHasMembershipError,
)
from organizations.filtersets import OrganizationInvitationFilterSet
from organizations.models import Organization, OrganizationInvitation
from organizations.permissions import (
    OrganizationInvitationPermission,
    OrganizationManagementPermission,
)
from organizations.serializers import (
    AcceptInvitationSerializer,
    CurrentMembershipSerializer,
    OrganizationInvitationSerializer,
    OrganizationSerializer,
)
from organizations.services import OrganizationService


class OrganizationViewSet(NoListVintaScheduleModelViewSet):
    """
    A viewset for managing organizations.
    """

    queryset = Organization.objects.all()
    serializer_class = OrganizationSerializer
    permission_classes = (IsAuthenticated, OrganizationManagementPermission)

    def get_queryset(self):
        user = self.request.user
        if hasattr(user, "organization_membership") and user.organization_membership:
            return Organization.objects.filter(id=user.organization_membership.organization_id)
        return Organization.objects.none()

    @extend_schema(
        summary="Current organization + role for the authenticated user",
        responses={
            200: CurrentMembershipSerializer,
            404: OpenApiResponse(description="No organization membership (gated user)"),
        },
    )
    @action(detail=False, methods=["get"], url_path="current", permission_classes=[IsAuthenticated])
    def current(self, request):
        """Return the caller's organization and role.

        HTTP 200 — the user is onboarded (has a membership).
        HTTP 404 — the user is gated (no membership yet).
        """
        membership = getattr(request.user, "organization_membership", None)
        if membership is None:
            raise NotFound(detail="No organization membership.")
        serializer = CurrentMembershipSerializer(membership, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)


class OrganizationInvitationViewSet(NoUpdateVintaScheduleModelViewSet):
    """
    A viewset for managing organization invitations.
    """

    queryset = OrganizationInvitation.objects.all()
    serializer_class = OrganizationInvitationSerializer
    permission_classes = (OrganizationInvitationPermission,)
    filterset_class = OrganizationInvitationFilterSet

    @inject
    def __init__(
        self,
        *args,
        organization_service: Annotated[OrganizationService, Provide["organization_service"]],
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.organization_service = organization_service

    def get_queryset(self):
        """Filter invitations by the user's organization."""
        user = self.request.user
        if hasattr(user, "organization_membership") and user.organization_membership:
            return OrganizationInvitation.objects.filter(
                organization_id=user.organization_membership.organization_id
            )
        # Return empty queryset for users without membership
        return OrganizationInvitation.objects.none()

    def get_serializer_context(self):
        """Add organization to serializer context."""
        context = super().get_serializer_context()
        user = self.request.user
        if hasattr(user, "organization_membership") and user.organization_membership:
            context["organization"] = user.organization_membership.organization
        return context

    def perform_destroy(self, instance):
        """Revoke invitation by calling the service method."""
        self.organization_service.revoke_invitation(str(instance.id))


class AcceptInvitationView(generics.CreateAPIView):
    """
    Public endpoint for accepting organization invitations.
    """

    serializer_class = AcceptInvitationSerializer
    permission_classes = (IsAuthenticated,)

    def create(self, request, *args, **kwargs):
        """Accept invitation and return success response."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            membership = serializer.create(serializer.validated_data)
        except UserAlreadyHasMembershipError:
            return Response(
                {"error": UserAlreadyHasMembershipError.default_detail},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except InvalidInvitationTokenError as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except DuplicateInvitationError as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_409_CONFLICT,
            )
        except InvitationNotFoundError as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {
                "message": "Invitation accepted successfully",
                "organization_id": membership.organization_id,
                "organization_name": membership.organization.name,
            },
            status=status.HTTP_201_CREATED,
        )
