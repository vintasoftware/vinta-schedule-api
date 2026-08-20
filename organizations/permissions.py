import enum
from collections.abc import Sequence
from typing import TYPE_CHECKING, Annotated

from dependency_injector.wiring import Provide, inject
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission
from vinta_billing.entitlement_cache import has_entitlement_cached
from vinta_billing.services.entitlement_service import EntitlementService
from vinta_orgs.mixins import SingleOrganizationModelMixin

from organizations.authorization import has_organization_permission
from organizations.exceptions import (
    BrandingEntitlementRequiredError,
    OrganizationHasParentBrandingError,
)
from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
)
from organizations.permission_catalog import (
    MANAGE_BILLING,
    MANAGE_BRANDING,
    MANAGE_MEMBERS,
    MANAGE_ORGANIZATION,
)
from payments.seams.resource_keys import WHITE_LABEL_BRANDING
from public_api.capabilities import is_target_in_subtree


if TYPE_CHECKING:
    from users.models import User


@inject
def _organization_holds_white_label_branding(
    organization: Organization,
    entitlement_service: Annotated[EntitlementService, Provide["entitlement_service"]] = None,  # type: ignore[assignment]
) -> bool:
    """The entitlement half of the branding gate, factored out so both
    ``is_branding_eligible_organization`` (below) and
    ``evaluate_branding_write_gate`` (further below) share one entitlement check
    rather than each re-deriving it. Both are two-condition gates now that the
    write gate's slug condition is retired.

    ``entitlement_service`` is DI-injected via ``@inject``/``Provide`` (the
    established pattern -- see ``audit/services.py``,
    ``accounts/account_adapters.py``); the ``organizations`` package is already
    wired via ``container.wire(packages=INTERNAL_INSTALLED_APPS)`` (see
    ``di_core/apps.py``), so callers never pass it explicitly. It carries a
    ``= None`` default rather than being required so the gate stays fail-closed
    when wiring hasn't run (e.g. an import path that reaches this function
    before app startup completes): an unresolvable entitlement service denies
    rather than admits.
    """
    if entitlement_service is None:
        return False
    return has_entitlement_cached(entitlement_service, organization, WHITE_LABEL_BRANDING)


@inject
def _organizations_hold_white_label_branding(
    organizations: Sequence[Organization],
    entitlement_service: Annotated[EntitlementService, Provide["entitlement_service"]] = None,  # type: ignore[assignment]
) -> dict[int, bool]:
    """Bulk sibling of ``_organization_holds_white_label_branding``, backing
    ``is_branding_eligible_organizations``. Same ``@inject``/``Provide``
    fail-closed pattern: an unresolvable entitlement service denies every
    organization in the batch rather than admitting any of it.

    Deliberately does not go through ``has_entitlement_cached`` — the point of
    this function is to answer for the whole batch in two queries, which
    already beats what the per-organization request memo achieves for a batch
    of distinct organizations.
    """
    if entitlement_service is None or not organizations:
        return {}
    return entitlement_service.has_entitlement_for_organizations(
        organizations, WHITE_LABEL_BRANDING
    )


def is_branding_eligible_organization(organization: Organization | None) -> bool:
    """Shared branding-eligibility gate: ``organization`` has no parent AND holds
    the ``white_label_branding`` entitlement.

    Two conditions -- this is the ``branding_logos`` S3Direct destination's
    ``auth`` callable (via ``user_administers_branding_eligible_organization``
    below) and the GraphQL logo-signing mutation's gate.

    ``evaluate_branding_write_gate`` below composes on top of this function
    rather than replacing it. It once added a third condition -- "and has picked
    a public slug" -- which is why the two were kept separate; that condition is
    retired (see it), so the two now admit the same set. The split is kept
    because the two answer different questions and a future condition may again
    apply to only one of them.
    """
    if organization is None or organization.parent_id is not None:
        return False
    return _organization_holds_white_label_branding(organization)


def is_branding_eligible_organizations(organizations: Sequence[Organization]) -> dict[int, bool]:
    """Bulk sibling of ``is_branding_eligible_organization``: the same
    parentless-and-entitled check for many organizations, in two queries total
    instead of two per organization.

    Built for ``MyMembershipSerializer.get_can_manage_branding``, which
    computes this per membership row on ``GET /organizations/mine/`` — one call
    to ``is_branding_eligible_organization`` per row would pay a subscription
    fetch plus entitlement-row fetch per distinct organization the caller
    belongs to. Organizations with a parent are excluded from the entitlement
    batch (same short-circuit as the single-organization function above) since
    their answer is always ``False`` without needing an entitlement lookup at
    all. See ``EntitlementService.has_entitlement_for_organizations`` for what
    the batching itself looks like.

    Returns ``{organization.pk: bool}`` for every organization passed in.
    """
    parentless = [organization for organization in organizations if organization.parent_id is None]
    entitled_by_pk = _organizations_hold_white_label_branding(parentless)
    return {
        organization.pk: (
            organization.parent_id is None and entitled_by_pk.get(organization.pk, False)
        )
        for organization in organizations
    }


class BrandingWriteGateReason(enum.Enum):
    """Distinguishable outcome of ``evaluate_branding_write_gate``.

    ``OK`` means the write is admitted; every other member names exactly the
    one condition that failed, so each of the three write surfaces
    (``OrganizationBrandingView``, ``update_branding``,
    ``OrganizationBrandingAdmin``) can render its own error idiom without
    re-deriving *why* the gate refused:

    - ``HAS_PARENT`` -- permanent. Branding within a hierarchy belongs to the
      reseller alone (spec Use-case 5); no message for this reason should read
      as fixable by the organization itself.
    - ``NOT_ENTITLED`` -- a billing state. The organization's plan does not
      include white-label branding; fixable by upgrading.

    There used to be a third failure reason. ``NO_SLUG`` -- "otherwise eligible
    but has not picked a public slug yet" -- was retired as a product rule when
    ``slug`` became NOT NULL with a ``save()``-time fallback and the
    ``organization_slug_not_blank`` check constraint made it unreachable for any
    organization that can exist; it was kept dead-with-reason for a while because
    it was still part of this enum's published contract, and has since been
    deleted along with its ``BRANDING_GATE_EXCEPTIONS`` entry and
    ``OrganizationSlugRequiredForBrandingError``.
    """

    OK = "ok"
    HAS_PARENT = "has_parent"
    NOT_ENTITLED = "not_entitled"


def evaluate_branding_write_gate(organization: Organization | None) -> BrandingWriteGateReason:
    """Two-condition branding **write** gate: the acting organization must be
    parentless and hold the ``white_label_branding`` entitlement. Replaces
    ``is_reseller()`` on every write surface.

    Composes on top of, rather than duplicating, the two-condition
    ``is_branding_eligible_organization`` above by sharing
    ``_organization_holds_white_label_branding`` -- see that function's
    docstring for why the logo-signing surface stays on the two-condition
    helper instead of this one.

    Checked in this order -- parent, then entitlement -- so the permanent case
    is never masked by a fixable one.

    **A third condition, "and has picked a public slug", was retired** as a
    product rule, and its last remnants -- the branch,
    ``BrandingWriteGateReason.NO_SLUG``, its ``BRANDING_GATE_EXCEPTIONS`` entry
    and ``OrganizationSlugRequiredForBrandingError`` -- have since been deleted.
    ``Organization.slug`` is NOT NULL with a ``save()``-time fallback and an
    ``organization_slug_not_blank`` check constraint, so no organization that
    exists can fail it and no write surface -- supported or not, including a raw
    ``queryset.update(slug="")`` -- can manufacture one that does.
    """
    if organization is None or organization.parent_id is not None:
        return BrandingWriteGateReason.HAS_PARENT
    if not _organization_holds_white_label_branding(organization):
        return BrandingWriteGateReason.NOT_ENTITLED
    return BrandingWriteGateReason.OK


BRANDING_GATE_EXCEPTIONS: dict[BrandingWriteGateReason, type[PermissionDenied]] = {
    BrandingWriteGateReason.HAS_PARENT: OrganizationHasParentBrandingError,
    BrandingWriteGateReason.NOT_ENTITLED: BrandingEntitlementRequiredError,
}


def check_branding_read_eligibility(organization: Organization | None) -> None:
    """Two-condition branding **eligibility** gate (parentless, entitled),
    shared by every branding read-adjacent surface: ``OrganizationBrandingView.get``
    and ``OrganizationBrandingLogoUploadParamsView.post`` (``organizations/views.py``).

    Derives its reason from ``evaluate_branding_write_gate``: anything but
    ``OK`` raises. It used to additionally admit ``NO_SLUG``, which kept a
    read-side surface open to an organization the write gate refused; that
    reason is gone (see the write gate), so the two now admit exactly the same
    set. The split is kept because the two answer different questions and a
    future condition may again apply to only one of them.
    """
    reason = evaluate_branding_write_gate(organization)
    if reason is BrandingWriteGateReason.OK:
        return
    raise BRANDING_GATE_EXCEPTIONS[reason]()


def user_administers_branding_eligible_organization(user: "User | None") -> bool:
    """True iff ``user`` holds an active membership carrying
    ``organizations.manage_branding`` in at least one branding-eligible
    organization (see ``is_branding_eligible_organization``).

    User-granularity rather than organization-granularity, deliberately: this is
    the ``auth`` callable for the ``branding_logos`` S3Direct destination
    (``vinta_schedule_api.settings.base.S3DIRECT_DESTINATIONS``), and s3direct's
    signing view only ever calls a destination's ``auth`` callable with the
    request user -- it has no notion of "the acting organization" the way our
    own views/resolvers do. So this can only authorize "this user administers
    SOME eligible organization", not "acting for this specific organization".
    Accepted deliberately: the generated
    object key is unique per upload (``generate_s3direct_file_name``) and the
    object only becomes visible once a branding row references it -- an admin of
    one eligible org obtaining a key another admin could theoretically also
    obtain is not a meaningful information disclosure.

    The GraphQL logo-signing mutation does NOT use this function -- it checks
    ``is_branding_eligible_organization`` directly against the acting
    organization, which it always knows, so it gets the tighter, org-specific
    check instead of this coarser one.

    The role half is now the ``organizations.manage_branding`` permission,
    replacing the dropped ``role`` column; the entitlement half
    (``is_branding_eligible_organization``) is untouched. The permission half is
    asked of the *database*, through
    ``OrganizationMembershipQuerySet.holding_permission`` -- the same lookup
    ``billing_recipients`` uses, and the queryset form of the question
    ``has_organization_permission`` answers for one ``(user, organization)``
    pair, so the two agree by construction (both read the union of a
    membership's own ``permissions`` grant with the permissions its ``groups``
    carry, and neither consults the global half or the superuser
    short-circuit). Asking it per membership instead cost one ``_get_membership``
    plus two permission queries **per organization the caller belongs to**, on a
    path -- s3direct logo signing -- that has no acting organization to narrow
    by. There is no bound organization on that path at all, which is why the
    question has to name organizations explicitly rather than read the ambient
    binding.

    ``user.is_active`` is checked here rather than inherited: the per-membership
    form got it from ``has_organization_permission``, and the queryset filters
    ``membership.is_active`` only.
    """
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    # ``active_for_user`` already ``select_related``s the organization.
    memberships = OrganizationMembership.objects.active_for_user(user).holding_permission(
        MANAGE_BRANDING
    )
    return any(
        is_branding_eligible_organization(membership.organization) for membership in memberships
    )


class OrganizationManagementPermission(BasePermission):
    def has_permission(self, request, view):
        # Anonymous / unauthenticated users have no user attribute at all.
        if not hasattr(request, "user"):
            return True
        user = request.user

        # Any authenticated user may create an additional organisation
        # (they become its admin via a fresh membership). Restricting create to
        # membership-less users would block the "create additional org" use-case.
        # All other actions still require the user to have no active membership
        # (the onboarding check).
        if view.action == "create":
            return bool(user and user.is_authenticated)

        # The package resolver represents missing and inactive membership as
        # ``None``. Only those callers may reach the remaining onboarding
        # endpoints on this viewset.
        membership = request.organization_membership
        return membership is None

    def has_object_permission(self, request, view, obj):
        # Anonymous / unauthenticated users propagate to here only in edge
        # cases; treat them the same as membership-less (allow the framework
        # to deny them via IsAuthenticated first).
        if not hasattr(request, "user"):
            return True

        membership = request.organization_membership
        if membership is None:
            # Membership-less OR inactive members never have object-level
            # access (they can only CREATE an org — handled in has_permission).
            return False

        return view.action != "create" and (
            (isinstance(obj, Organization) and membership.organization_id == obj.id)
            or (
                isinstance(obj, SingleOrganizationModelMixin)
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
        return request.organization_membership is not None

    def has_object_permission(self, request, view, obj):
        # User must have an active organization membership
        membership = request.organization_membership
        if not membership:
            return False

        # User can only manage invitations for their own organization
        if isinstance(obj, OrganizationInvitation):
            return membership.organization_id == obj.organization_id
        return False


class IsOrganizationAdmin(BasePermission):
    """
    Permission for admin-only endpoints within an organization.

    - `has_permission`: requires an authenticated user with an active organization
      membership carrying `organizations.manage_members`. This gate enforces the admin
      capability at the collection level (list, create).
    - `has_object_permission`: additionally enforces that the object's organization matches
      the membership organization and delegates the "is this user an admin of this object's org"
      decision to `User.is_organization_admin(organization_id)` so the rule has a single
      implementation. Handles both Organization instances and organization-scoped
      (`SingleOrganizationModelMixin`) subclasses.

    The `organizations.manage_members` permission replaced the `membership.is_admin`
    property this used to read. The active-membership check in front of it is **not** redundant
    and must stay: it is what supplies the organization the capability is asked
    about. `has_permission` runs before any object is known, so the caller's own
    resolved organization is the only one it can name.

    A global grant -- a Django-admin-assigned `organizations.manage_members`, or
    membership of the seeded global `organization_admin` group -- admits nobody
    here either, and that is enforced one layer down rather than by this class:
    `has_organization_permission` resolves the capability from an active
    membership in the named organization alone. See
    `organizations/authorization.py`.
    """

    permission = MANAGE_MEMBERS

    def has_permission(self, request, view) -> bool:
        user: User = request.user
        if not user or not user.is_authenticated:
            return False
        membership = request.organization_membership
        return membership is not None and has_organization_permission(
            user, self.permission, membership.organization
        )

    def has_object_permission(self, request, view, obj) -> bool:
        membership = request.organization_membership
        if membership is None:
            return False

        # Determine the object's organization_id
        if isinstance(obj, Organization):
            obj_organization_id = obj.id
        elif isinstance(obj, OrganizationMembership):
            obj_organization_id = obj.organization_id
        elif isinstance(obj, SingleOrganizationModelMixin):
            obj_organization_id = obj.organization_id
        else:
            # Handle objects that carry a plain ``organization`` FK
            if hasattr(obj, "organization_id"):
                obj_organization_id = obj.organization_id
            else:
                return False

        # Membership org must match object org; user must hold the admin permission.
        if membership.organization_id != obj_organization_id:
            return False

        return has_organization_permission(request.user, self.permission, membership.organization)


class CanManageOrganization(IsOrganizationAdmin):
    """Require the organization-settings capability for the acting organization."""

    permission = MANAGE_ORGANIZATION


class CanManageBranding(IsOrganizationAdmin):
    """Require the branding capability; entitlement checks remain at the view."""

    permission = MANAGE_BRANDING


class IsBillingOwnerOrAdmin(BasePermission):
    """**Phase 5 of the billing migration ratified keeping this class**, rather
    than deleting it along with its two test modules
    (``payments/tests/test_reseller_root_billing.py`` and the
    ``TestIsBillingOwnerOrAdminParity`` rows of
    ``organizations/tests/test_permissions_parity.py``). Both were reviewed
    against the object-level equivalence and the branch-2 unreachability
    argued below -- no authorization regression either way -- and kept
    because they are honest about what they prove: both test files exercise
    this class directly (``has_permission``/``has_object_permission``), never
    through a live view, and neither claims any endpoint is gated by it. This
    class gates nothing today: it is deliberately retained-but-unwired policy
    for a request shape no current endpoint produces, not dead code left
    behind by accident.

    The host's former policy for the billing-management endpoints: changing a
    plan, purchasing/cancelling an add-on, cancelling the subscription.

    No longer wired into those endpoints directly (Phase 2 of the billing
    migration deleted ``payments/seams/permissions.py``, the shim that swapped
    this class in for ``vinta_billing.permissions.IsBillingManager``): 0.5.0
    fixed that class's own object-level check, and its branch-1 answer --
    a member holding ``vinta_billing.manage_billing`` in the object's own
    organization -- is exactly what this class's branch 1 already computed, so
    the swap lost nothing. Branch 2 (the acting-reseller-root subtree walk
    below) was already unreachable from any endpoint before the deletion --
    see its own docstring -- so it is kept here as host policy for direct
    callers, verified by ``payments/tests/test_reseller_root_billing.py``,
    rather than deleted along with the seam.

    Split across ``has_permission``/``has_object_permission`` rather than doing
    everything in ``has_permission``, because the two answer different
    questions: ``has_permission`` cannot know *which* organization is being
    billed. The billing endpoints act on the **billing root**, which is
    frequently an ancestor of the organization the request resolved
    (``resolve_billing_root``); when this class was still wired to those
    endpoints, the views handed that resolved root to
    ``check_object_permissions`` explicitly (see ``SubscriptionViewSet`` /
    ``AddOnViewSet`` -- the package's own viewsets, which do not use this
    class). So the coarse gate ran first and the real decision ran
    against ``obj``.

    (Historical note: this split was originally *forced* by an ordering defect —
    ``TenantScopedViewMixin`` resolved the active organization after DRF had
    already run ``check_permissions``, so nothing organization-specific was
    reliable in ``has_permission`` at all. That ordering has since been fixed --
    ``TenantScopedViewMixin.perform_authentication`` now resolves and binds
    between authentication and ``check_permissions`` -- and the split stays
    because of the billing-root reason above, which is independent of it.
    ``request.organization`` and ``request.organization_membership`` are now
    both correct inside ``has_permission``.)

    - ``has_permission``: coarse gate -- an active membership carrying
      ``vinta_billing.manage_billing`` **in the organization the request resolved**
      (since the ordering fix above, that is the membership
      ``X-Organization-Id`` selected, not "some organization"). Coarse all the
      same, because the organization
      being *billed* is frequently an ancestor of it; deciding *which*
      organization is ``has_object_permission``'s job.
    - ``has_object_permission``: the gate against ``obj`` (an
      ``Organization`` -- the resolved billing root), when this class was
      still wired to an endpoint. Granted access when either:

      1. The caller's active membership is in ``obj`` itself and carries
         ``vinta_billing.manage_billing``.
      2. An **acting reseller root**: a low-level policy branch for a caller's
         active membership that carries ``vinta_billing.manage_billing``, can
         invite/create organizations (``can_invite_organizations``), and has
         ``obj`` within its subtree. It reuses
         ``public_api.capabilities.is_target_in_subtree`` — the boolean form of
         the reseller bundle's subtree-membership walk — rather than
         re-deriving it or coupling this REST layer to a GraphQL error type.
         The package header resolver currently cannot produce the cross-binding
         request shape that would make this branch decisive, so it preserves the
         policy for direct callers without describing current endpoint behavior.

    ``vinta_billing.manage_billing`` replaced the flat two-column disjunction this
    used to read (``role == ADMIN or is_billing_owner``); the
    ``organization_admin`` and ``organization_billing_owner`` groups both carry
    the permission, which is what makes the two spellings the same set.
    **Branch 2 keeps its bespoke walk** rather than becoming a plain permission
    lookup: the auth backend
    resolves permissions for the *bound* organization only, and the grant this
    branch looks for is held in an ancestor of it. The permission is therefore
    asked with that ancestor named explicitly (see
    ``organizations.authorization.has_organization_permission``); nothing else
    about the branch changed.

    Read-only billing endpoints (usage, plan catalog, subscription detail)
    were, back when this class was still wired to an endpoint, intentionally
    **not** gated by it -- they stayed open to any authenticated member,
    mirroring ``BillingProfileViewSet``'s reads-open, writes-gated split.
    Today neither the reads nor the writes are gated by this class: the
    write endpoints are gated by ``vinta_billing.permissions.IsBillingManager``
    instead.
    """

    def has_permission(self, request, view) -> bool:
        user: User = request.user
        if not user or not user.is_authenticated:
            return False
        membership = request.organization_membership
        return membership is not None and has_organization_permission(
            user, MANAGE_BILLING, membership.organization
        )

    def has_object_permission(self, request, view, obj) -> bool:
        user: User = request.user
        membership = request.organization_membership
        if membership is None:
            return False
        target_organization = self._resolve_target_organization(obj)
        if target_organization is None:
            return False

        if membership.organization_id == target_organization.id and has_organization_permission(
            user, MANAGE_BILLING, target_organization
        ):
            return True

        return self._acting_reseller_root_permits(user, membership, target_organization)

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
        self,
        user: "User",
        membership: OrganizationMembership,
        target_organization: Organization,
    ) -> bool:
        """The hand-written half of this class -- see the class docstring.

        The package header resolver resolves the membership from the selected
        organization, so this branch is currently not decisive for a request
        that crosses bindings. It remains an explicit low-level subtree-policy
        check for direct callers; naming ``membership.organization`` keeps that
        policy independent of the ambient context.
        """
        if not has_organization_permission(user, MANAGE_BILLING, membership.organization):
            return False
        if not membership.organization.can_invite_organizations:
            return False
        return is_target_in_subtree(membership.organization, target_organization)
