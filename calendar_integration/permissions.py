from django.http import Http404

from dependency_injector.wiring import Provide, inject
from rest_framework.permissions import SAFE_METHODS, BasePermission

from calendar_integration.models import CalendarGroupSlot, CalendarOwnership
from calendar_integration.services.booking_policy_permission_service import (
    BookingPolicyPermissionService,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from organizations.models import get_active_organization_membership


class BookingPolicyPermission(BasePermission):
    """Permission for ``BookingPolicyViewSet``.

    Reads (GET/HEAD/OPTIONS — list/retrieve) are open to any authenticated user;
    ``get_queryset()`` already restricts visibility to the caller's org.

    Writes (POST/PUT/PATCH/DELETE — create/update/destroy) are **self-service**:

    - Org admins may manage policies for any target (calendar, membership,
      calendar group, or the organization default).
    - Non-admin members may manage only their **own** personal policies — a
      policy targeting a calendar they own, or their own membership. Policies for
      calendar groups and the organization default stay **admin only**.

    The per-target decision is centralized in ``BookingPolicyPermissionService``
    (shared with the public GraphQL surface). Create reads the target from the
    request body here; update/delete read it from the existing policy row in
    ``has_object_permission``.

    Membership-less (gated) users are allowed through on safe methods: the
    queryset returns [] rather than 403, which is the consistent pattern used by
    ``CalendarEventViewSet`` and ``BlockedTimeViewSet``.
    """

    @inject
    def __init__(
        self,
        booking_policy_permission_service: "BookingPolicyPermissionService | None" = Provide[
            "booking_policy_permission_service"
        ],
    ):
        self.booking_policy_permission_service = booking_policy_permission_service

    def has_permission(self, request, view) -> bool:
        """Safe methods: any authenticated user. Unsafe methods: self or admin."""
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in SAFE_METHODS:
            return True

        membership = get_active_organization_membership(request.user)
        if membership is None:
            return False
        if membership.is_admin:
            return True

        # Detail writes (update/delete) are gated per-object in
        # ``has_object_permission`` — allow a non-admin member to proceed to it.
        if request.method not in ("POST",):
            return True

        # Create: the target lives in the request body.
        return self.booking_policy_permission_service.can_member_manage_target(
            user=request.user,
            membership=membership,
            organization_id=membership.organization_id,
            calendar_id=request.data.get("calendar"),
            membership_user_id=request.data.get("membership_user_id"),
            calendar_group_id=request.data.get("calendar_group"),
            is_organization_default=bool(request.data.get("is_organization_default", False)),
        )

    def has_object_permission(self, request, view, obj) -> bool:
        """Detail writes: admins always; members only for their own target."""
        if request.method in SAFE_METHODS:
            return True
        membership = get_active_organization_membership(request.user)
        return self.booking_policy_permission_service.can_member_manage_policy(
            user=request.user,
            membership=membership,
            policy=obj,
        )


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


class GroupScopedAvailabilityWindowPermission(BasePermission):
    """Route-level group-visibility gate for the group-scoped availability
    window routes nested under a group's slot
    (``calendar-groups/<group_id>/slots/<slot_id>/availability-windows/...``,
    CALENDAR_GROUP_SCOPED_AVAILABILITY Phase 1c).

    ``has_permission`` resolves the ``(group_id, slot_id)`` pair from the URL
    once, org-scoped, and stashes it on the view as ``view.group_slot`` so the
    viewset doesn't repeat the query. Visibility uses the same "can this user
    see the group at all" rule as ``CalendarGroupPermission``
    (``CalendarPermissionService.can_manage_calendar_group`` — admin, or owns
    ANY calendar in ANY slot of the group; matches the Phase 1a service tests,
    where owning a calendar in a *different* slot of the same group is enough
    to see the group but not enough to manage a calendar the caller doesn't
    own). A caller who cannot see the ``(group, slot)`` — because it genuinely
    doesn't exist, belongs to another organization, or they own no calendar
    anywhere in the group and are not an org admin — gets the exact same
    ``Http404`` as a caller hitting a URL for a slot that never existed; there
    is no separate 403 branch to compare it against (spec: "a member cannot
    learn about groups they are not part of through error messages or
    listings").

    This is a coarse, route-level gate only. The fine-grained decision — may
    *this* user write *this specific* calendar's config within the slot —
    stays where Phase 1a put it: ``CalendarGroupService`` re-checks
    ``can_manage_group_scoped_calendar_config`` on every write and raises the
    identically-shaped ``CalendarGroupSlotConfigNotFoundError`` (mapped to the
    same 404 by the view) when a visible member targets a calendar they don't
    own. Both gates matter: this one stops a stranger from even proving the
    slot exists; the service one stops a legitimate group member from writing
    a calendar that isn't theirs.
    """

    @inject
    def __init__(
        self,
        calendar_permission_service: "CalendarPermissionService | None" = Provide[
            "calendar_permission_service"
        ],
    ):
        self.calendar_permission_service = calendar_permission_service

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not user.is_authenticated:
            return False
        membership = get_active_organization_membership(user)
        if membership is None:
            return False

        group_id = view.kwargs.get("group_id")
        slot_id = view.kwargs.get("slot_id")
        if group_id is None or slot_id is None:
            return False

        try:
            group_slot = (
                CalendarGroupSlot.objects.filter_by_organization(membership.organization_id)
                .select_related("group")
                .get(id=slot_id, group_fk_id=group_id)
            )
        except CalendarGroupSlot.DoesNotExist:
            raise Http404() from None

        if self.calendar_permission_service is None or not (
            self.calendar_permission_service.can_manage_calendar_group(
                user=user, group=group_slot.group
            )
        ):
            # Same not-found shape as a genuinely missing slot -- a member must
            # not learn this group/slot exists through a distinguishable 403.
            raise Http404()

        view.group_slot = group_slot
        return True


class GroupScopedBlockedTimePermission(BasePermission):
    """Route-level group-visibility gate for the group-scoped blocked time
    routes nested under a group's slot
    (``calendar-groups/<group_id>/slots/<slot_id>/blocked-times/...``,
    CALENDAR_GROUP_SCOPED_AVAILABILITY Phase 2b).

    Identical in every respect to ``GroupScopedAvailabilityWindowPermission``
    -- same coarse route-level gate (``can_manage_calendar_group``), same
    non-disclosure ``Http404`` on a stranger, a cross-organization slot, or
    a slot belonging to a different group than the one in the URL. See that
    class's docstring for the full rationale; only the resource it guards
    differs (blocks instead of windows).
    """

    @inject
    def __init__(
        self,
        calendar_permission_service: "CalendarPermissionService | None" = Provide[
            "calendar_permission_service"
        ],
    ):
        self.calendar_permission_service = calendar_permission_service

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not user.is_authenticated:
            return False
        membership = get_active_organization_membership(user)
        if membership is None:
            return False

        group_id = view.kwargs.get("group_id")
        slot_id = view.kwargs.get("slot_id")
        if group_id is None or slot_id is None:
            return False

        try:
            group_slot = (
                CalendarGroupSlot.objects.filter_by_organization(membership.organization_id)
                .select_related("group")
                .get(id=slot_id, group_fk_id=group_id)
            )
        except CalendarGroupSlot.DoesNotExist:
            raise Http404() from None

        if self.calendar_permission_service is None or not (
            self.calendar_permission_service.can_manage_calendar_group(
                user=user, group=group_slot.group
            )
        ):
            # Same not-found shape as a genuinely missing slot -- a member must
            # not learn this group/slot exists through a distinguishable 403.
            raise Http404()

        view.group_slot = group_slot
        return True


class GroupScopedQuotaRulePermission(BasePermission):
    """Route-level group-visibility gate for the group-scoped quota rule
    routes nested under a group's slot
    (``calendar-groups/<group_id>/slots/<slot_id>/quota-rules/...``,
    CALENDAR_GROUP_SCOPED_AVAILABILITY Phase 3c).

    Identical in every respect to ``GroupScopedAvailabilityWindowPermission``/
    ``GroupScopedBlockedTimePermission`` -- same coarse route-level gate
    (``can_manage_calendar_group``), same non-disclosure ``Http404`` on a
    stranger, a cross-organization slot, or a slot belonging to a different
    group than the one in the URL. See ``GroupScopedAvailabilityWindowPermission``'s
    docstring for the full rationale; only the resource it guards differs
    (quota rules instead of windows).
    """

    @inject
    def __init__(
        self,
        calendar_permission_service: "CalendarPermissionService | None" = Provide[
            "calendar_permission_service"
        ],
    ):
        self.calendar_permission_service = calendar_permission_service

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not user.is_authenticated:
            return False
        membership = get_active_organization_membership(user)
        if membership is None:
            return False

        group_id = view.kwargs.get("group_id")
        slot_id = view.kwargs.get("slot_id")
        if group_id is None or slot_id is None:
            return False

        try:
            group_slot = (
                CalendarGroupSlot.objects.filter_by_organization(membership.organization_id)
                .select_related("group")
                .get(id=slot_id, group_fk_id=group_id)
            )
        except CalendarGroupSlot.DoesNotExist:
            raise Http404() from None

        if self.calendar_permission_service is None or not (
            self.calendar_permission_service.can_manage_calendar_group(
                user=user, group=group_slot.group
            )
        ):
            # Same not-found shape as a genuinely missing slot -- a member must
            # not learn this group/slot exists through a distinguishable 403.
            raise Http404()

        view.group_slot = group_slot
        return True
