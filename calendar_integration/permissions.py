from dependency_injector.wiring import Provide, inject
from rest_framework.permissions import BasePermission

from calendar_integration.models import CalendarOwnership
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from organizations.models import get_active_organization_membership


class BookingPolicyPermission(BasePermission):
    """Permission for ``BookingPolicyViewSet``.

    Requires an authenticated user.  Membership-less (gated) users are allowed
    through: ``get_queryset()`` returns an empty queryset for them so the list
    action returns 200+[] rather than 403, which is the consistent pattern used
    by ``CalendarEventViewSet`` and ``BlockedTimeViewSet``.

    Write operations (create, update, destroy) that reach a membership-less user
    will fail gracefully because ``_build_service()`` gets ``None`` from
    ``get_active_organization_membership`` and will not initialize the service
    with a tenant — but in practice the ``TenantScopedViewMixin`` will have
    already returned 400/403 if the user has no org context.

    Org-admin gating is intentionally **not** applied here — the plan does not
    restrict policy management to admins only.
    """

    def has_permission(self, request, view) -> bool:
        """Allow any authenticated user through (membership check happens in get_queryset)."""
        return bool(request.user.is_authenticated)


class ExternalEventChangeRequestPermission(BasePermission):
    """Permission for ``ExternalEventChangeRequestViewSet``.

    Requires an authenticated user with an active organization membership.
    The eligibility scoping (member-attendee vs. admin) is applied in
    ``get_queryset()`` and the individual approve/reject actions — not here.
    This class is the first gate: unauthenticated users are refused outright
    and membership-less (gated) users see an empty queryset rather than a 403.
    """

    def has_permission(self, request, view) -> bool:
        """Allow access only to authenticated users with an active membership."""
        if not request.user.is_authenticated:
            return False
        return get_active_organization_membership(request.user) is not None


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
            .filter(membership_user_id=request.user.id, calendar=calendar)
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
        return get_active_organization_membership(user) is not None

    def has_object_permission(self, request, view, obj):
        membership = get_active_organization_membership(request.user)
        if membership is None or obj.organization_id != membership.organization_id:
            return False
        if self.calendar_permission_service is None:
            # Fallback if DI isn't wired (should not happen in normal flows).
            return (
                CalendarOwnership.objects.filter_by_organization(obj.organization_id)
                .filter(membership_user_id=request.user.id, calendar_fk__group_slots__group_fk=obj)
                .exists()
            )
        return self.calendar_permission_service.can_manage_calendar_group(
            user=request.user, group=obj
        )
