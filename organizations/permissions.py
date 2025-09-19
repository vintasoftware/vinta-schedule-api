from django.core.exceptions import ObjectDoesNotExist

from rest_framework.permissions import BasePermission

from organizations.models import Organization, OrganizationInvitation, OrganizationModel


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
