from typing import TYPE_CHECKING

from rest_framework.permissions import BasePermission

from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationModel,
    get_active_organization_membership,
)


if TYPE_CHECKING:
    from users.models import User


class OrganizationManagementPermission(BasePermission):
    def has_permission(self, request, view):
        # Anonymous / unauthenticated users have no user attribute at all.
        try:
            user = request.user
        except AttributeError:
            return True

        # get_active_organization_membership handles both missing membership
        # (RelatedObjectDoesNotExist) and inactive membership, returning None
        # for both cases.  Only membership-LESS (or inactive) users may reach
        # onboarding endpoints such as POST /organizations/.
        membership = get_active_organization_membership(user)
        return membership is None

    def has_object_permission(self, request, view, obj):
        # Anonymous / unauthenticated users propagate to here only in edge
        # cases; treat them the same as membership-less (allow the framework
        # to deny them via IsAuthenticated first).
        try:
            user = request.user
        except AttributeError:
            return True

        membership = get_active_organization_membership(user)
        if membership is None:
            # Membership-less OR inactive members never have object-level
            # access (they can only CREATE an org — handled in has_permission).
            return False

        return view.action != "create" and (
            (isinstance(obj, Organization) and membership.organization_id == obj.id)
            or (
                isinstance(obj, OrganizationModel)
                and membership.organization_id == obj.organization_id
            )
        )


class OrganizationInvitationPermission(BasePermission):
    """
    Permission class for managing organization invitations.
    Only users who are members of an organization can manage its invitations.
    """

    def has_permission(self, request, view):
        # User must be authenticated
        if not request.user or not request.user.is_authenticated:
            return False

        # User must have an active organization membership
        return get_active_organization_membership(request.user) is not None

    def has_object_permission(self, request, view, obj):
        # User must have an active organization membership
        membership = get_active_organization_membership(request.user)
        if not membership:
            return False

        # User can only manage invitations for their own organization
        if isinstance(obj, OrganizationInvitation):
            return membership.organization_id == obj.organization_id
        return False


class IsOrganizationAdmin(BasePermission):
    """
    Permission for admin-only endpoints within an organization.

    - `has_permission`: requires an authenticated user with an active ADMIN organization
      membership. This gate enforces the admin role at the collection level (list, create).
    - `has_object_permission`: additionally enforces that the object's organization matches
      the membership organization and delegates the "is this user an admin of this object's org"
      decision to `User.is_organization_admin(organization_id)` so the rule has a single
      implementation. Handles both Organization instances and OrganizationModel subclasses.
    """

    def has_permission(self, request, view) -> bool:
        user: User = request.user
        if not user or not user.is_authenticated:
            return False
        membership = get_active_organization_membership(user)
        return membership is not None and membership.is_admin

    def has_object_permission(self, request, view, obj) -> bool:
        user: User = request.user
        membership = get_active_organization_membership(user)
        if membership is None:
            return False

        # Determine the object's organization_id
        if isinstance(obj, Organization):
            obj_organization_id = obj.id
        elif isinstance(obj, OrganizationMembership):
            obj_organization_id = obj.organization_id
        elif isinstance(obj, OrganizationModel):
            obj_organization_id = obj.organization_id
        else:
            return False

        # Membership org must match object org; user must be an admin
        if membership.organization_id != obj_organization_id:
            return False

        return user.is_organization_admin(membership.organization_id)
