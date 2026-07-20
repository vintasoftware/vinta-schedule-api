from typing import TYPE_CHECKING

from graphql import GraphQLError
from rest_framework.permissions import BasePermission

from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationModel,
    get_active_organization_membership,
)
from public_api.capabilities import assert_target_in_subtree


if TYPE_CHECKING:
    from users.models import User


class OrganizationManagementPermission(BasePermission):
    def has_permission(self, request, view):
        # Anonymous / unauthenticated users have no user attribute at all.
        try:
            user = request.user
        except AttributeError:
            return True

        # Phase 5: any authenticated user may create an additional organisation
        # (they become its admin via a fresh membership). Gating create to
        # membership-less users would block the "create additional org" use-case.
        # All other actions still require the user to have no active membership
        # (the onboarding gate).
        if view.action == "create":
            return bool(user and user.is_authenticated)

        # get_active_organization_membership handles both missing membership
        # (RelatedObjectDoesNotExist) and inactive membership, returning None
        # for both cases.  Only membership-LESS (or inactive) users may reach
        # the remaining onboarding endpoints on this viewset.
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
            # Handle SystemUser and other objects with an organization FK
            if hasattr(obj, "organization_id"):
                obj_organization_id = obj.organization_id
            else:
                return False

        # Membership org must match object org; user must be an admin
        if membership.organization_id != obj_organization_id:
            return False

        return user.is_organization_admin(membership.organization_id)


class IsBillingOwnerOrAdmin(BasePermission):
    """Permission for the billing-management endpoints (``payments/billing_views.py``):
    change plan, purchase/cancel an add-on, cancel the subscription.

    Split across ``has_permission``/``has_object_permission`` rather than doing
    everything in ``has_permission``, deliberately: ``TenantScopedViewMixin.initial()``
    calls ``super().initial()`` (which runs DRF's ``check_permissions()``, and
    therefore every ``has_permission``) **before** it resolves and stashes
    ``request.organization`` — the same ordering ``IsOrganizationAdmin`` above
    already works around by never reading ``request.organization`` in
    ``has_permission``. ``request.organization`` only becomes reliable once the
    view body itself runs, which is exactly when ``has_object_permission`` runs
    too (views call ``check_object_permissions`` explicitly against the
    resolved billing-root ``Organization``; see ``SubscriptionViewSet`` /
    ``AddOnViewSet``).

    - ``has_permission``: coarse gate -- an active membership that is ``ADMIN``
      **or** has ``is_billing_owner=True``, in *some* organization. Does not by
      itself decide *which* organization; that is ``has_object_permission``'s job.
    - ``has_object_permission``: the real gate, against ``obj`` (an
      ``Organization`` -- the resolved billing root). Grants access when either:

      1. The caller's active membership is in ``obj`` itself and is
         ``ADMIN``-or-billing-owner — the two roles the plan names as allowed to
         manage billing.
      2. An **acting reseller root**: the caller's active membership is
         ``ADMIN``-or-billing-owner in some *other* organization that both (a)
         can invite/create organizations (``can_invite_organizations``) and (b)
         has ``obj`` within its subtree — the same subtree relationship
         ``resolve_billing_root`` pools usage against, so a root that pays for a
         descendant's capacity may also manage its billing, even when the
         caller's ``X-Organization-Id``-scoped membership is to the descendant
         itself (e.g. a support/account-manager membership with no elevated role
         there). Reuses ``public_api.capabilities.assert_target_in_subtree`` —
         the same subtree-membership check the reseller bundle's GraphQL
         mutations use — rather than re-deriving the walk a second time.

    Read-only billing endpoints (usage, plan catalog, subscription detail) are
    intentionally **not** gated by this class — they stay open to any
    authenticated member, mirroring ``BillingProfileViewSet``'s reads-open,
    writes-gated split.
    """

    def has_permission(self, request, view) -> bool:
        user: User = request.user
        if not user or not user.is_authenticated:
            return False
        membership = get_active_organization_membership(user)
        return membership is not None and (membership.is_admin or membership.is_billing_owner)

    def has_object_permission(self, request, view, obj) -> bool:
        user: User = request.user
        membership = get_active_organization_membership(user)
        if membership is None:
            return False
        target_organization = self._resolve_target_organization(obj)
        if target_organization is None:
            return False

        if membership.organization_id == target_organization.id and (
            membership.is_admin or membership.is_billing_owner
        ):
            return True

        return self._acting_reseller_root_permits(membership, target_organization)

    def _resolve_target_organization(self, obj) -> Organization | None:
        """``obj`` is either the ``Organization`` (billing root) directly, or a
        model carrying one -- one hop (``obj.organization``) for most billing
        models, two (``obj.subscription.organization``) for a
        ``SubscriptionAddOn``, whose own FK is to the subscription, not the
        organization."""
        if isinstance(obj, Organization):
            return obj
        organization = getattr(obj, "organization", None)
        if organization is not None:
            return organization
        subscription = getattr(obj, "subscription", None)
        return getattr(subscription, "organization", None)

    def _acting_reseller_root_permits(
        self, membership: OrganizationMembership, target_organization: Organization
    ) -> bool:
        if not (membership.is_admin or membership.is_billing_owner):
            return False
        if not membership.organization.can_invite_organizations:
            return False
        try:
            assert_target_in_subtree(membership.organization, target_organization)
        except GraphQLError:
            return False
        return True
