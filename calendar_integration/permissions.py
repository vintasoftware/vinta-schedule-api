from django.http import Http404

from dependency_injector.wiring import Provide, inject
from rest_framework.permissions import SAFE_METHODS, BasePermission

from calendar_integration.models import CalendarGroupSlot, CalendarOwnership
from calendar_integration.services.booking_policy_permission_service import (
    BookingPolicyPermissionService,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService


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
        # Not `| None`: `@inject` always supplies it, and the two `has_permission`
        # branches below dereference it unconditionally. The optional annotation only
        # ever described the pre-injection instant, which no caller observes.
        booking_policy_permission_service: "BookingPolicyPermissionService" = Provide[
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

        membership = request.organization_membership
        if membership is None:
            return False
        # ``organizations.manage_members`` -- the same capability
        # ``User.is_organization_admin`` reads -- replaced ``membership.is_admin``.
        # The organization is named explicitly rather than left to the ambient
        # binding, because that is what the attribute it replaces meant: a
        # statement about ``membership.organization``.
        is_privileged = request.user.is_organization_admin(membership.organization)
        if is_privileged:
            return True

        # Detail writes (update/delete) are gated per-object in
        # ``has_object_permission`` — allow a non-admin member to proceed to it.
        if request.method not in ("POST",):
            return True

        # Create: the target lives in the request body. ``is_privileged`` is
        # handed to the service rather than re-derived there from
        # ``membership.is_admin``: two derivations could disagree, and a caller
        # admitted here by one and refused later by the other would be able to
        # create a policy they then could not edit.
        return self.booking_policy_permission_service.can_member_manage_target(
            user=request.user,
            membership=membership,
            organization_id=membership.organization_id,
            is_privileged=is_privileged,
            calendar_id=request.data.get("calendar"),
            membership_user_id=request.data.get("membership_user_id"),
            calendar_group_id=request.data.get("calendar_group"),
            is_organization_default=bool(request.data.get("is_organization_default", False)),
        )

    def has_object_permission(self, request, view, obj) -> bool:
        """Detail writes: admins always; members only for their own target."""
        if request.method in SAFE_METHODS:
            return True
        membership = request.organization_membership
        return self.booking_policy_permission_service.can_member_manage_policy(
            user=request.user,
            membership=membership,
            # The same capability ``has_permission`` short-circuits on, computed
            # the same way -- see the comment there.
            is_privileged=membership is not None
            and request.user.is_organization_admin(membership.organization),
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
        return request.organization_membership is not None


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
      organization membership for every action. For the `create` action
      (no object exists yet to gate in `has_object_permission`), it
      additionally requires the caller to be an org admin -- creating a
      CalendarGroup is an admin-only structural change (see
      `CalendarPermissionService.can_manage_calendar_group`).
    - `has_object_permission` splits on `view.action`:
      - `update` / `partial_update` / `destroy` (mutating the group's own
        structure) delegate to `CalendarPermissionService.can_manage_calendar_group`
        -- admin only.
      - every other object-level action (`retrieve`, and the custom
        booking/read actions like `create_event`, `list_events`,
        `availability`, `bookable-slots`) delegates to
        `CalendarPermissionService.can_view_calendar_group` -- admin, or any
        member who owns a calendar somewhere in the group's slots. This
        preserves member access to book/read against a group they
        participate in; `get_queryset()` already keeps a non-participant
        member from ever reaching a group to retrieve/act on in the first
        place (404, not 403 -- see the viewset).
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
        membership = request.organization_membership
        if membership is None:
            return False
        if getattr(view, "action", None) == "create":
            return user.is_organization_admin(membership.organization_id)
        return True

    def has_object_permission(self, request, view, obj):
        membership = request.organization_membership
        if membership is None or obj.organization_id != membership.organization_id:
            return False

        is_manage_action = getattr(view, "action", None) in (
            "update",
            "partial_update",
            "destroy",
        )

        if self.calendar_permission_service is None:
            # Fallback if DI isn't wired (should not happen in normal flows).
            is_admin = request.user.is_organization_admin(obj.organization_id)
            if is_manage_action:
                return is_admin
            return is_admin or (
                CalendarOwnership.objects.filter_by_organization(obj.organization_id)
                .filter(membership_user_id=request.user.id, calendar_fk__group_slots__group_fk=obj)
                .exists()
            )

        if is_manage_action:
            return self.calendar_permission_service.can_manage_calendar_group(
                user=request.user, group=obj
            )
        return self.calendar_permission_service.can_view_calendar_group(
            user=request.user, group=obj
        )


class GroupScopedAvailabilityWindowPermission(BasePermission):
    """Route-level group-visibility gate for the group-scoped availability
    window routes nested under a group's slot
    (``calendar-groups/<group_id>/slots/<slot_id>/availability-windows/...``).

    ``has_permission`` resolves the ``(group_id, slot_id)`` pair from the URL
    once, org-scoped, and stashes it on the view as ``view.group_slot`` so the
    viewset doesn't repeat the query. Visibility uses the same "can this user
    see the group at all" rule as ``CalendarGroupPermission``
    (``CalendarPermissionService.can_view_calendar_group`` — admin, or owns
    ANY calendar in ANY slot of the group; matches the calendar group
    service's behavior, where owning a calendar in a *different* slot of the
    same group is enough to see the group but not enough to manage a calendar
    the caller doesn't own). A caller who cannot see the ``(group, slot)`` —
    because it genuinely doesn't exist, belongs to another organization, or
    they own no calendar anywhere in the group and are not an org admin —
    gets the exact same ``Http404`` as a caller hitting a URL for a slot that
    never existed; there is no separate 403 branch to compare it against
    (spec: "a member cannot learn about groups they are not part of through
    error messages or listings").

    This is a coarse, route-level gate only. The fine-grained decision — may
    *this* user write *this specific* calendar's config within the slot —
    lives in ``CalendarGroupService``, which re-checks
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
        membership = request.organization_membership
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
            self.calendar_permission_service.can_view_calendar_group(
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
    (``calendar-groups/<group_id>/slots/<slot_id>/blocked-times/...``).

    Identical in every respect to ``GroupScopedAvailabilityWindowPermission``
    -- same coarse route-level gate (``can_view_calendar_group``), same
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
        membership = request.organization_membership
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
            self.calendar_permission_service.can_view_calendar_group(
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
    (``calendar-groups/<group_id>/slots/<slot_id>/quota-rules/...``).

    Identical in every respect to ``GroupScopedAvailabilityWindowPermission``/
    ``GroupScopedBlockedTimePermission`` -- same coarse route-level gate
    (``can_view_calendar_group``), same non-disclosure ``Http404`` on a
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
        membership = request.organization_membership
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
            self.calendar_permission_service.can_view_calendar_group(
                user=user, group=group_slot.group
            )
        ):
            # Same not-found shape as a genuinely missing slot -- a member must
            # not learn this group/slot exists through a distinguishable 403.
            raise Http404()

        view.group_slot = group_slot
        return True


class BookingCodePermission(BasePermission):
    """Permission for ``BookingCodeViewSet`` (``POST`` / ``DELETE /booking-codes/``).

    ``has_permission`` only requires an authenticated user with an active
    organization membership. Unlike ``CalendarGroupPermission``, the finer
    owner-or-org-admin decision does **not** live in ``has_object_permission``:
    on ``create`` the target (``calendar`` or ``calendar_group``) arrives in the
    request BODY, not as a URL-routed object DRF could resolve before this class
    runs, and on ``destroy`` the target is the token being revoked, which is
    idempotent by design (revoking a non-existent or foreign-org id is a
    no-op ``204``, never a permission question). Both authorization decisions
    happen in ``BookingCodeViewSet`` itself, where the target has actually been
    resolved -- mirroring the split ``CalendarGroupPermission`` documents between
    admin-only "manage" and owner-or-participant "view/book", applied here to
    "mint a code for this calendar/group".
    """

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not user.is_authenticated:
            return False
        return request.organization_membership is not None
