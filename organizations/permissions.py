from typing import TYPE_CHECKING

from rest_framework.permissions import BasePermission

from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationModel,
    OrganizationRole,
    get_active_organization_membership,
)
from payments.billing_constants import Entitlement
from payments.entitlement_cache import has_entitlement_cached
from public_api.capabilities import is_target_in_subtree


if TYPE_CHECKING:
    from users.models import User


def is_branding_eligible_organization(organization: Organization | None) -> bool:
    """Shared branding-eligibility gate: ``organization`` has no parent AND holds
    the ``white_label_branding`` entitlement.

    Two conditions today (Organization Auth-Area Branding plan, Phase 2b) --
    this is the gate's first caller (the ``branding_logos`` S3Direct destination's
    ``auth`` callable, via ``user_administers_branding_eligible_organization``
    below, and the GraphQL logo-signing mutation). Phase 3 extends this into the
    full write gate by adding a third condition (the organization ends the write
    with a ``slug`` set). Written as an early-return chain rather than a single
    boolean expression so that extension is a one-line addition, not a rewrite.

    The entitlement check goes through the same deferred-DI-container / fail-closed
    pattern as ``organizations.models.resolve_branding_for_display`` (see that
    function's docstring for why the import is function-body-local): an
    unresolvable entitlement service denies rather than admits.
    """
    if organization is None or organization.parent_id is not None:
        return False

    from di_core.containers import container

    if container is None:
        return False
    return has_entitlement_cached(
        container.entitlement_service(), organization, Entitlement.WHITE_LABEL_BRANDING
    )


def user_administers_branding_eligible_organization(user: "User | None") -> bool:
    """True iff ``user`` holds an active ADMIN membership in at least one
    branding-eligible organization (see ``is_branding_eligible_organization``).

    User-granularity rather than organization-granularity, deliberately: this is
    the ``auth`` callable for the ``branding_logos`` S3Direct destination
    (``vinta_schedule_api.settings.base.S3DIRECT_DESTINATIONS``), and s3direct's
    signing view only ever calls a destination's ``auth`` callable with the
    request user -- it has no notion of "the acting organization" the way our
    own views/resolvers do. So this can only authorize "this user administers
    SOME eligible organization", not "acting for this specific organization".
    Accepted per the plan's Logo upload path guiding decision: the generated
    object key is unique per upload (``generate_s3direct_file_name``) and the
    object only becomes visible once a branding row references it -- an admin of
    one eligible org obtaining a key another admin could theoretically also
    obtain is not a meaningful information disclosure.

    The GraphQL logo-signing mutation does NOT use this function -- it checks
    ``is_branding_eligible_organization`` directly against the acting
    organization, which it always knows, so it gets the tighter, org-specific
    check instead of this coarser one.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    admin_memberships = OrganizationMembership.objects.active_for_user(user).filter(
        role=OrganizationRole.ADMIN
    )
    return any(
        is_branding_eligible_organization(membership.organization)
        for membership in admin_memberships
    )


class OrganizationManagementPermission(BasePermission):
    def has_permission(self, request, view):
        # Anonymous / unauthenticated users have no user attribute at all.
        try:
            user = request.user
        except AttributeError:
            return True

        # Any authenticated user may create an additional organisation
        # (they become its admin via a fresh membership). Restricting create to
        # membership-less users would block the "create additional org" use-case.
        # All other actions still require the user to have no active membership
        # (the onboarding check).
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
         there). Reuses ``public_api.capabilities.is_target_in_subtree`` — the
         boolean form of the same subtree-membership walk the reseller bundle's
         GraphQL mutations use (via ``assert_target_in_subtree``) — rather than
         re-deriving it a second time or coupling this REST layer to a GraphQL
         error type.

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
        return is_target_in_subtree(membership.organization, target_organization)
