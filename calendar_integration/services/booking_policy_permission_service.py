"""Centralized authorization for managing ``BookingPolicy`` rows.

Both API surfaces funnel their "may this actor manage a policy for target T?"
decision through this one service, so the rule lives in a single place instead of
being duplicated across the REST permission class and the GraphQL resolvers:

- **Private REST** (``BookingPolicyPermission``): the actor is an internal
  ``User`` + their ``OrganizationMembership``. Privileged == ``membership.is_admin``.
- **Public GraphQL** (booking-policy mutations / query): the actor is a
  ``SystemUser`` token. Privileged == the token is **organization-wide**
  (``scoped_to_membership_user_id is None``); a token scoped to a membership is
  treated exactly like that member.

Both reduce to the same primitive — an *acting membership user id* plus an
*is-privileged* flag — evaluated against the policy target by
``_can_manage_target``:

- **Privileged** actors (org admins / org-wide tokens) may manage **any** target.
- Everyone else may manage only their **own** personal policies: a ``calendar``
  they own (an active ``CalendarOwnership`` links their membership to it) or their
  **own** membership. ``calendar_group`` and ``is_organization_default`` targets
  are privileged-only.
"""

from typing import TYPE_CHECKING

from django.db.models import Q

from calendar_integration.models import BookingPolicy, CalendarOwnership


if TYPE_CHECKING:
    from calendar_integration.querysets import BookingPolicyQuerySet
    from organizations.models import OrganizationMembership
    from public_api.models import SystemUser
    from users.models import User


class BookingPolicyPermissionService:
    """Single source of truth for booking-policy management authorization."""

    # ------------------------------------------------------------------
    # Core decision
    # ------------------------------------------------------------------

    @staticmethod
    def _can_manage_target(
        *,
        organization_id: int,
        acting_membership_user_id: int | None,
        is_privileged: bool,
        calendar_id=None,
        membership_user_id=None,
        calendar_group_id=None,
        is_organization_default: bool = False,
    ) -> bool:
        """Evaluate the rule for a resolved ``(actor, target)`` pair.

        ``acting_membership_user_id`` is the ``membership_user_id`` the actor acts
        as (``user.id`` for a member, ``scoped_to_membership_user_id`` for a scoped
        token). ``is_privileged`` short-circuits to allow-all (org admin / org-wide
        token). Non-privileged actors reach a grant only for a calendar they own or
        their own membership; group / org-default targets fall through to ``False``.
        """
        if is_privileged:
            return True
        if acting_membership_user_id is None:
            # Non-privileged actor with no membership identity manages nothing.
            return False

        if calendar_id is not None:
            return (
                CalendarOwnership.objects.filter_by_organization(organization_id)
                .filter(membership_user_id=acting_membership_user_id, calendar_fk_id=calendar_id)
                .exists()
            )
        if membership_user_id is not None:
            return int(membership_user_id) == acting_membership_user_id

        # calendar_group / is_organization_default (or no target) → privileged-only.
        return False

    @staticmethod
    def _policy_membership_user_id(policy: BookingPolicy) -> int | None:
        # Denormalized column contributed by OrganizationMembershipForeignKey;
        # real at runtime but invisible to django-stubs.
        return policy.membership_user_id  # type: ignore[attr-defined]

    @staticmethod
    def _token_scoped_user_id(system_user: "SystemUser") -> int | None:
        # Denormalized column contributed by OrganizationMembershipForeignKey.
        return system_user.scoped_to_membership_user_id  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Member (private REST) adapters
    # ------------------------------------------------------------------

    def can_member_manage_target(
        self,
        *,
        user: "User",
        membership: "OrganizationMembership | None",
        organization_id: int,
        calendar_id=None,
        membership_user_id=None,
        calendar_group_id=None,
        is_organization_default: bool = False,
    ) -> bool:
        """Whether an internal member may manage a policy for the given target."""
        if membership is None:
            return False
        return self._can_manage_target(
            organization_id=organization_id,
            acting_membership_user_id=user.id,
            is_privileged=membership.is_admin,
            calendar_id=calendar_id,
            membership_user_id=membership_user_id,
            calendar_group_id=calendar_group_id,
            is_organization_default=is_organization_default,
        )

    def can_member_manage_policy(
        self,
        *,
        user: "User",
        membership: "OrganizationMembership | None",
        policy: BookingPolicy,
    ) -> bool:
        """Object-level variant: read the target from an existing policy row."""
        if membership is None or policy.organization_id != membership.organization_id:
            return False
        return self.can_member_manage_target(
            user=user,
            membership=membership,
            organization_id=policy.organization_id,
            calendar_id=policy.calendar_fk_id,
            membership_user_id=self._policy_membership_user_id(policy),
            calendar_group_id=policy.calendar_group_fk_id,
            is_organization_default=policy.is_organization_default,
        )

    # ------------------------------------------------------------------
    # SystemUser (public GraphQL) adapters
    # ------------------------------------------------------------------

    def can_system_user_manage_target(
        self,
        *,
        system_user: "SystemUser | None",
        organization_id: int,
        calendar_id=None,
        membership_user_id=None,
        calendar_group_id=None,
        is_organization_default: bool = False,
    ) -> bool:
        """Whether a token may manage a policy for the given target.

        An organization-wide token (``scoped_to_membership_user_id is None``) is
        privileged and may manage any target — preserving the pre-existing
        behavior. A membership-scoped token acts exactly as that member. A missing
        token (no authenticated principal) manages nothing.
        """
        if system_user is None:
            return False
        scoped_uid = self._token_scoped_user_id(system_user)
        return self._can_manage_target(
            organization_id=organization_id,
            acting_membership_user_id=scoped_uid,
            is_privileged=scoped_uid is None,
            calendar_id=calendar_id,
            membership_user_id=membership_user_id,
            calendar_group_id=calendar_group_id,
            is_organization_default=is_organization_default,
        )

    def can_system_user_manage_policy(
        self,
        *,
        system_user: "SystemUser | None",
        policy: BookingPolicy,
    ) -> bool:
        """Object-level variant: read the target from an existing policy row."""
        return self.can_system_user_manage_target(
            system_user=system_user,
            organization_id=policy.organization_id,
            calendar_id=policy.calendar_fk_id,
            membership_user_id=self._policy_membership_user_id(policy),
            calendar_group_id=policy.calendar_group_fk_id,
            is_organization_default=policy.is_organization_default,
        )

    # ------------------------------------------------------------------
    # Read scoping (public GraphQL list query)
    # ------------------------------------------------------------------

    def scope_policies_for_system_user(
        self,
        queryset: "BookingPolicyQuerySet",
        *,
        system_user: "SystemUser | None",
        organization_id: int,
    ) -> "BookingPolicyQuerySet":
        """Restrict a policy queryset to what ``system_user`` may see.

        Org-wide tokens see everything (queryset returned unchanged). A
        membership-scoped token sees only the policies it may manage — those for
        calendars it owns and its own membership; group and org-default policies
        are excluded. A missing token sees nothing.
        """
        if system_user is None:
            return queryset.none()
        scoped_uid = self._token_scoped_user_id(system_user)
        if scoped_uid is None:
            return queryset

        owned_calendar_ids = (
            CalendarOwnership.objects.filter_by_organization(organization_id)
            .filter(membership_user_id=scoped_uid)
            .values_list("calendar_fk_id", flat=True)
        )
        return queryset.filter(
            Q(calendar_fk_id__in=owned_calendar_ids) | Q(membership_user_id=scoped_uid)
        )
