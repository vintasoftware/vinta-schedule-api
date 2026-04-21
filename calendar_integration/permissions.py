from dependency_injector.wiring import Provide, inject
from rest_framework.permissions import BasePermission

from calendar_integration.models import CalendarOwnership
from calendar_integration.services.calendar_permission_service import CalendarPermissionService


class CalendarEventPermission(BasePermission):
    """
    Custom permission for CalendarEvent operations.
    Only authenticated users can access calendar events.
    """

    def has_permission(self, request, view):
        # Only authenticated users can access calendar events
        return request.user.is_authenticated

    def has_object_permission(self, request, view, obj):
        # Users can only access events from calendars they have access to
        # This could be expanded based on specific business rules
        calendar = obj.calendar
        owner = (
            CalendarOwnership.objects.filter_by_organization(obj.organization_id)
            .filter(user=request.user, calendar=calendar)
            .first()
        )
        if not owner:
            return False
        return request.user.is_authenticated


class CalendarAvailabilityPermission(BasePermission):
    """
    Custom permission for calendar availability operations.
    Only authenticated users can check calendar availability.
    """

    def has_permission(self, request, view):
        # Only authenticated users can check availability
        return request.user.is_authenticated


class CalendarGroupPermission(BasePermission):
    """
    Permission for CalendarGroup REST endpoints.

    - `has_permission` requires an authenticated user with an active
      organization membership; list/create are org-scoped by the viewset.
    - `has_object_permission` delegates the "can this user manage this group"
      decision to `CalendarPermissionService.can_manage_calendar_group` so the
      rule (and future org-admin override) has a single implementation.
    """

    @inject
    def __init__(
        self,
        calendar_permission_service: "CalendarPermissionService | None" = Provide[
            "calendar_permission_service"
        ],
    ):
        self.calendar_permission_service = calendar_permission_service

    def has_permission(self, request, view):
        user = request.user
        if not user.is_authenticated:
            return False
        return getattr(user, "organization_membership", None) is not None

    def has_object_permission(self, request, view, obj):
        if obj.organization_id != request.user.organization_membership.organization_id:
            return False
        if self.calendar_permission_service is None:
            # Fallback if DI isn't wired (should not happen in normal flows).
            return (
                CalendarOwnership.objects.filter_by_organization(obj.organization_id)
                .filter(user=request.user, calendar_fk__group_slots__group_fk=obj)
                .exists()
            )
        return self.calendar_permission_service.can_manage_calendar_group(
            user=request.user, group=obj
        )
