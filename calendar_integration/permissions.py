from rest_framework.permissions import BasePermission

from calendar_integration.models import CalendarOwnership


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
    - `has_object_permission` additionally requires the user to own at least
      one calendar inside the group's slots — matching the plan's "owns at
      least one calendar in the group" heuristic. An org-admin override is a
      follow-up.
    """

    def has_permission(self, request, view):
        user = request.user
        if not user.is_authenticated:
            return False
        return getattr(user, "organization_membership", None) is not None

    def has_object_permission(self, request, view, obj):
        if obj.organization_id != request.user.organization_membership.organization_id:
            return False
        return (
            CalendarOwnership.objects.filter_by_organization(obj.organization_id)
            .filter(
                user=request.user,
                calendar__group_slots__group_fk=obj,
            )
            .exists()
        )
