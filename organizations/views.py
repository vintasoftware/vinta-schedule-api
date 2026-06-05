from typing import Annotated

from dependency_injector.wiring import Provide, inject
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import generics, status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from common.utils.view_utils import (
    NoListVintaScheduleModelViewSet,
    NoUpdateVintaScheduleModelViewSet,
    ReadOnlyVintaScheduleModelViewSet,
)
from organizations.exceptions import (
    DuplicateInvitationError,
    InvalidInvitationTokenError,
    InvitationNotFoundError,
    UserAlreadyHasMembershipError,
)
from organizations.filtersets import OrganizationInvitationFilterSet
from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    get_active_organization_membership,
)
from organizations.permissions import (
    IsOrganizationAdmin,
    OrganizationInvitationPermission,
    OrganizationManagementPermission,
)
from organizations.serializers import (
    AcceptInvitationSerializer,
    CurrentMembershipSerializer,
    OrganizationInvitationSerializer,
    OrganizationMembershipSerializer,
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
        membership = get_active_organization_membership(user)
        if membership:
            return Organization.objects.filter(id=membership.organization_id)
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
        membership = get_active_organization_membership(request.user)
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
        membership = get_active_organization_membership(user)
        if membership:
            return OrganizationInvitation.objects.filter(organization_id=membership.organization_id)
        # Return empty queryset for users without an active membership
        return OrganizationInvitation.objects.none()

    def get_serializer_context(self):
        """Add organization to serializer context."""
        context = super().get_serializer_context()
        user = self.request.user
        membership = get_active_organization_membership(user)
        if membership:
            context["organization"] = membership.organization
        return context

    def perform_destroy(self, instance):
        """Revoke invitation by calling the service method."""
        self.organization_service.revoke_invitation(str(instance.id))


class OrganizationMembershipViewSet(ReadOnlyVintaScheduleModelViewSet):
    """
    A viewset for listing, retrieving, and managing organization members.

    Admin-only endpoint — lists both active and inactive members of the caller's
    organization, suitable for a datatable view. Non-admin members get 403.

    Actions:
    - `deactivate`: POST to disable a member (prevent self-deactivation and
      protect the last active admin).
    - `reactivate`: POST to re-enable a member.
    """

    queryset = OrganizationMembership.objects.select_related("user", "user__profile")
    serializer_class = OrganizationMembershipSerializer
    permission_classes = (IsOrganizationAdmin,)

    def get_queryset(self):
        """Org-scoped queryset: return members of the caller's organization only."""
        user = self.request.user
        membership = get_active_organization_membership(user)
        if membership:
            return (
                OrganizationMembership.objects.filter(organization_id=membership.organization_id)
                .select_related("user", "user__profile")
                .order_by("id")
            )
        return OrganizationMembership.objects.none()

    @extend_schema(
        summary="Deactivate an organization member",
        responses={
            200: OrganizationMembershipSerializer,
            400: OpenApiResponse(description="Cannot deactivate self or last active admin"),
            403: OpenApiResponse(description="Not an admin"),
            404: OpenApiResponse(description="Member not found or cross-org"),
        },
    )
    @action(detail=True, methods=["post"], url_path="deactivate")
    def deactivate(self, request, pk=None):
        """Deactivate a member (set is_active=False).

        Guards:
        - Cannot deactivate own membership (self-lockout prevention).
        - Cannot deactivate the last active admin (org lockout prevention).

        Idempotency: deactivating an already-inactive member is a no-op success.
        """
        target = (
            self.get_object()
        )  # Permission checks via IsOrganizationAdmin.has_object_permission
        user = request.user

        # Guard: prevent self-deactivation
        if target.user_id == user.id:
            raise PermissionDenied(detail="Cannot deactivate your own membership.")

        # Guard: prevent deactivating the last active admin
        # Count OTHER active admins; if this is the last one, refuse
        if target.is_admin:
            org_id = target.organization_id
            other_active_admin_count = (
                OrganizationMembership.objects.filter(
                    organization_id=org_id,
                    role=target.role,  # Same role filter (ADMIN)
                    is_active=True,
                )
                .exclude(id=target.id)  # Exclude the target itself
                .count()
            )
            if other_active_admin_count == 0:
                raise ValidationError(
                    detail="Cannot deactivate the last active admin of the organization."
                )

        # Deactivate (idempotent: no-op if already inactive)
        target.is_active = False
        target.save(update_fields=["is_active"])

        # Return the updated membership
        serializer = self.get_serializer(target)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Reactivate an organization member",
        responses={
            200: OrganizationMembershipSerializer,
            403: OpenApiResponse(description="Not an admin"),
            404: OpenApiResponse(description="Member not found or cross-org"),
        },
    )
    @action(detail=True, methods=["post"], url_path="reactivate")
    def reactivate(self, request, pk=None):
        """Reactivate a member (set is_active=True).

        No guards — re-enabling is always safe.

        Idempotency: reactivating an already-active member is a no-op success.
        """
        target = (
            self.get_object()
        )  # Permission checks via IsOrganizationAdmin.has_object_permission

        # Reactivate (idempotent: no-op if already active)
        target.is_active = True
        target.save(update_fields=["is_active"])

        # Return the updated membership
        serializer = self.get_serializer(target)
        return Response(serializer.data, status=status.HTTP_200_OK)


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
