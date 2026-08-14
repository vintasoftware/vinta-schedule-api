import enum
from collections.abc import Sequence
from typing import TYPE_CHECKING, Annotated

from dependency_injector.wiring import Provide, inject
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission
from vinta_orgs.mixins import SingleOrganizationModelMixin

from organizations.exceptions import (
    BrandingEntitlementRequiredError,
    OrganizationHasParentBrandingError,
    OrganizationSlugRequiredForBrandingError,
)
from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
)
from payments.billing_constants import Entitlement
from payments.entitlement_cache import has_entitlement_cached
from payments.services.entitlement_service import EntitlementService
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
    return has_entitlement_cached(
        entitlement_service, organization, Entitlement.WHITE_LABEL_BRANDING
    )


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
        organizations, Entitlement.WHITE_LABEL_BRANDING
    )


def is_branding_eligible_organization(organization: Organization | None) -> bool:
    """Shared branding-eligibility gate: ``organization`` has no parent AND holds
    the ``white_label_branding`` entitlement.

    Two conditions -- this is the ``branding_logos`` S3Direct destination's
    ``auth`` callable (via ``user_administers_branding_eligible_organization``
    below) and the GraphQL logo-signing mutation's gate (Organization
    Auth-Area Branding plan, Phase 2b).

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
    - ``NO_SLUG`` -- **retired, and unreachable.** It used to mean "otherwise
      eligible but has not picked a public slug yet". ``Organization.slug`` is
      now NOT NULL, refused when blank by the ``organization_slug_not_blank``
      check constraint, and filled in by ``Organization.save()`` when a caller
      leaves it out -- so no organization that exists can be missing one, and
      the rule it expressed is gone as a product rule, not merely as code. Kept
      dead-with-reason rather than deleted here because it is still part of this
      enum's published contract (``BRANDING_GATE_EXCEPTIONS`` still maps it, and
      ``check_branding_read_eligibility`` still admits it); Phase 4 of the
      vinta-django-orgs migration plan deletes all three together.
    """

    OK = "ok"
    HAS_PARENT = "has_parent"
    NOT_ENTITLED = "not_entitled"
    NO_SLUG = "no_slug"


def evaluate_branding_write_gate(organization: Organization | None) -> BrandingWriteGateReason:
    """Two-condition branding **write** gate (Organization Auth-Area Branding
    plan, Phase 3): the acting organization must be parentless and hold the
    ``white_label_branding`` entitlement. Replaces ``is_reseller()`` on every
    write surface.

    Composes on top of, rather than duplicating, the two-condition
    ``is_branding_eligible_organization`` above by sharing
    ``_organization_holds_white_label_branding`` -- see that function's
    docstring for why the logo-signing surface stays on the two-condition
    helper instead of this one.

    Checked in this order -- parent, then entitlement -- so the permanent case
    is never masked by a fixable one.

    **The third condition, "and has picked a public slug", is retired** (Phase 1
    of the vinta-django-orgs migration; see that plan's "Slug precondition for
    branding writes is retired" Guiding Decision). ``Organization.slug`` became
    NOT NULL with a ``save()``-time fallback and an
    ``organization_slug_not_blank`` check constraint, so no persisted
    organization can fail it and no write surface -- supported or not, including
    a raw ``queryset.update(slug="")`` -- can manufacture one that does. The
    branch below is kept, unreachable and stated as such, only because
    ``BrandingWriteGateReason.NO_SLUG`` is still part of this module's published
    contract; Phase 4 removes the branch, the enum member, its
    ``BRANDING_GATE_EXCEPTIONS`` entry and
    ``OrganizationSlugRequiredForBrandingError`` together.
    """
    if organization is None or organization.parent_id is not None:
        return BrandingWriteGateReason.HAS_PARENT
    if not _organization_holds_white_label_branding(organization):
        return BrandingWriteGateReason.NOT_ENTITLED
    if not organization.slug:
        # Unreachable for any persisted organization -- see the docstring. An
        # unsaved, in-memory ``Organization()`` is the only value that still
        # reaches it, which is what the remaining unit coverage exercises.
        return BrandingWriteGateReason.NO_SLUG
    return BrandingWriteGateReason.OK


BRANDING_GATE_EXCEPTIONS: dict[BrandingWriteGateReason, type[PermissionDenied]] = {
    BrandingWriteGateReason.HAS_PARENT: OrganizationHasParentBrandingError,
    BrandingWriteGateReason.NOT_ENTITLED: BrandingEntitlementRequiredError,
    BrandingWriteGateReason.NO_SLUG: OrganizationSlugRequiredForBrandingError,
}


def check_branding_read_eligibility(organization: Organization | None) -> None:
    """Two-condition branding **eligibility** gate (parentless, entitled),
    shared by every branding read-adjacent surface: ``OrganizationBrandingView.get``
    and ``OrganizationBrandingLogoUploadParamsView.post`` (``organizations/views.py``).

    Derives its reason from ``evaluate_branding_write_gate`` and additionally
    admits ``NO_SLUG``. That allowance is now **vacuous**: the write gate can no
    longer return ``NO_SLUG`` for any persisted organization (see its
    docstring), so the two gates admit exactly the same set. It is kept until
    Phase 4 retires the enum member, at which point this reduces to "raise on
    anything but ``OK``".
    """
    reason = evaluate_branding_write_gate(organization)
    if reason in (BrandingWriteGateReason.OK, BrandingWriteGateReason.NO_SLUG):
        return
    raise BRANDING_GATE_EXCEPTIONS[reason]()


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

    - `has_permission`: requires an authenticated user with an active ADMIN organization
      membership. This gate enforces the admin role at the collection level (list, create).
    - `has_object_permission`: additionally enforces that the object's organization matches
      the membership organization and delegates the "is this user an admin of this object's org"
      decision to `User.is_organization_admin(organization_id)` so the rule has a single
      implementation. Handles both Organization instances and organization-scoped
      (`SingleOrganizationModelMixin`) subclasses.
    """

    def has_permission(self, request, view) -> bool:
        user: User = request.user
        if not user or not user.is_authenticated:
            return False
        membership = request.organization_membership
        return membership is not None and membership.is_admin

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

        # Membership org must match object org; user must be an admin
        if membership.organization_id != obj_organization_id:
            return False

        return request.user.is_organization_admin(membership.organization_id)


class IsBillingOwnerOrAdmin(BasePermission):
    """Permission for the billing-management endpoints (``payments/billing_views.py``):
    change plan, purchase/cancel an add-on, cancel the subscription.

    Split across ``has_permission``/``has_object_permission`` rather than doing
    everything in ``has_permission``, because the two answer different
    questions: ``has_permission`` cannot know *which* organization is being
    billed. The billing endpoints act on the **billing root**, which is
    frequently an ancestor of the organization the request resolved
    (``resolve_billing_root``), and the views hand that resolved root to
    ``check_object_permissions`` explicitly (see ``SubscriptionViewSet`` /
    ``AddOnViewSet``). So the coarse gate runs first and the real decision runs
    against ``obj``.

    (Historical note: this split was originally *forced* by an ordering defect —
    ``TenantScopedViewMixin`` resolved the active organization after DRF had
    already run ``check_permissions``, so nothing organization-specific was
    reliable in ``has_permission`` at all. Phase 3.5 of the vinta-django-orgs
    migration fixed the ordering; the split stays because of the billing-root
    reason above, which is independent of it. ``request.organization`` and
    ``request.organization_membership`` are now both correct
    inside ``has_permission``.)

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
        membership = request.organization_membership
        return membership is not None and (membership.is_admin or membership.is_billing_owner)

    def has_object_permission(self, request, view, obj) -> bool:
        membership = request.organization_membership
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
