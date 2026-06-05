from typing import TYPE_CHECKING

from django.core.exceptions import ObjectDoesNotExist

from rest_framework.permissions import BasePermission

from organizations.models import Organization, OrganizationInvitation, OrganizationModel


if TYPE_CHECKING:
    from users.models import User


class OrganizationManagementPermission(BasePermission):
    def has_permission(self, request, view):
        try:
            membership = request.user.organization_membership
            return membership is None
        except ObjectDoesNotExist:
            return True
        except AttributeError:
            return True

    def has_object_permission(self, request, view, obj):
        try:
            membership = request.user.organization_membership
            return view.action != "create" and (
                (isinstance(obj, Organization) and membership.organization_id == obj.id)
                or (
                    isinstance(obj, OrganizationModel)
                    and membership.organization_id == obj.organization_id
                )
            )
        except ObjectDoesNotExist:
            return True
        except AttributeError:
            return True


class OrganizationInvitationPermission(BasePermission):
    """
    Permission class for managing organization invitations.
    Only users who are members of an organization can manage its invitations.
    """

    def has_permission(self, request, view):
        # User must be authenticated
        if not request.user or not request.user.is_authenticated:
            return False

        # User must have an organization membership
        # Use the same pattern as OrganizationViewSet
        return (
            hasattr(request.user, "organization_membership")
            and request.user.organization_membership is not None
        )

    def has_object_permission(self, request, view, obj):
        # User must have an organization membership
        if (
            not hasattr(request.user, "organization_membership")
            or not request.user.organization_membership
        ):
            return False

        # User can only manage invitations for their own organization
        if isinstance(obj, OrganizationInvitation):
            return request.user.organization_membership.organization_id == obj.organization_id
        return False


class IsOrganizationAdmin(BasePermission):
    """
    Permission for admin-only endpoints within an organization.

    - `has_permission`: requires an authenticated user with an active
      organization membership (using safe `getattr` to handle membership-less users).
    - `has_object_permission`: delegates the "is this user an admin of this object's org"
      decision to `User.is_organization_admin(organization_id)` so the rule has a single
      implementation. Handles both Organization instances and OrganizationModel subclasses.
    """

    def has_permission(self, request, view) -> bool:
        user: User = request.user
        if not user or not user.is_authenticated:
            return False
        return getattr(user, "organization_membership", None) is not None

    def has_object_permission(self, request, view, obj) -> bool:
        user: User = request.user
        membership = getattr(user, "organization_membership", None)
        if membership is None:
            return False

        # Determine the object's organization_id
        if isinstance(obj, Organization):
            obj_organization_id = obj.id
        elif isinstance(obj, OrganizationModel):
            obj_organization_id = obj.organization_id
        else:
            return False

        # Membership org must match object org; user must be an admin
        if membership.organization_id != obj_organization_id:
            return False

        return user.is_organization_admin(membership.organization_id)
