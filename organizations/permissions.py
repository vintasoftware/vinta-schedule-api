from django.core.exceptions import ObjectDoesNotExist

from rest_framework.permissions import BasePermission

from organizations.models import Organization, OrganizationModel


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
