"""The one way this repository asks "may this user do X in organization Y".

Every authorization decision reads a *permission*, never a role column and never
a group name -- see the plan's **Group scope** and **Permission catalog shape**
Guiding Decisions
(``ai-plans/2026-08-12-VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md``).
The codenames themselves live in ``organizations.permission_catalog``.

**Why a helper rather than a bare ``user.has_perm(...)`` at each call site.**
Two independent reasons, and each on its own is sufficient.

*The organization.* ``vinta_orgs.auth_backends.OrganizationModelBackend``
resolves a membership's permissions for *the organization bound to the current
context* and for no other. Every call site this replaces named its organization
explicitly -- ``membership.is_admin`` is a statement about
``membership.organization``, and ``User.is_organization_admin(organization)``
takes the organization as an argument. Two of those call sites ask about an
organization that is **not** the bound one, and a bare ``has_perm`` would answer
the wrong question in both:

* ``IsBillingOwnerOrAdmin``'s acting-reseller-root branch asks about an
  *ancestor* of the bound organization (the whole point of the branch).
* Roughly a dozen DRF views are not built on ``TenantScopedViewMixin`` and so
  bind nothing at all (``AGENTS.md``, Multi-Tenancy). ``ServiceAccountViewSet``
  is one of them and carries ``IsOrganizationAdmin``; under a bare ``has_perm``
  it would resolve no organization permissions and refuse *every* caller.

*The source of the grant.* ``has_perm`` answers from the **union** of the
organization half with a global half (``user.user_permissions`` plus the user's
own ``auth.Group`` rows) and, before any backend runs at all, from
``PermissionsMixin``'s superuser short-circuit. Neither of those is a statement
about the organization named, and neither could grant anything under the
``role`` / ``is_billing_owner`` columns this phase replaces: a global
``organizations.manage_members`` was inert, membership of the seeded
``organization_admin`` group (a plain global ``auth.Group``, listed by the user
form's group picker in ``users/admin.py``) was inert, and a superuser without an
admin membership did **not** satisfy ``role == ADMIN``. So this helper asks
``organizations.auth_backends.OrganizationModelBackend.get_membership_permissions``
-- the organization half alone, resolved from an active membership in the named
organization -- rather than ``user.has_perm``. Identical outcomes is this
phase's contract, and admitting any of those three would break it.

The helper binds the named organization for the duration of the check and
restores the previous binding afterwards. The resolution itself takes the
organization as an argument and so does not depend on the binding; the binding
is kept because everything *underneath* it (the membership manager, and any
future package change to how a membership is looked up) is written against the
ambient organization. The backend caches per organization pk, so a check against
a second organization neither reads nor poisons the first one's cached set.

**One deliberate difference from the role columns this replaces**, named here
because "identical outcomes" is this migration phase's contract:

* **An inactive *user* passes nothing.** The backend gates on
  ``user.is_active``; the role checks gated only on ``membership.is_active``.
  This narrows rather than widens, and is unreachable from the request path
  (authentication refuses an inactive user first).

A second one was claimed here and is **withdrawn**: "a superuser passes every
check", justified as granting nothing new because a superuser already reads
every tenant through the Django admin. It was never true of the code being
replaced -- ``membership.role == ADMIN`` refused a superuser who held no admin
membership -- and the "already reaches everything" argument does not survive the
billing endpoints, where passing ``IsBillingOwnerOrAdmin`` changes a tenant's
plan, buys an add-on, or cancels a subscription **at Stripe / MercadoPago**. The
Django admin exposes no such button. A superuser is now answered from their
memberships like anybody else; pinned by
``organizations/tests/test_permissions_parity.py``.

The *membership*'s ``is_active`` gate is not lost either: the backend resolves
nothing for a deactivated membership (Phase 3.5). Do not re-add it here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from common.organization_context import get_current_organization, organization_context


if TYPE_CHECKING:
    from organizations.models import Organization
    from users.models import User


def _organization_by_pk(organization_pk: int | str) -> Organization | None:
    """Load the organization to bind when the caller only had its pk.

    Imported lazily: ``users.models`` imports this module, and
    ``organizations.models`` imports ``users`` indirectly through the swappable
    membership model.

    ``Organization`` is the tenant root, not tenant-scoped data, so its manager
    is a plain one and this needs no binding of its own.
    """
    from organizations.models import Organization

    return Organization.objects.filter(pk=organization_pk).first()


def _holds_through_a_membership(user: User, permission: str, organization: Organization) -> bool:
    """``permission``, from an active membership in ``organization`` and nowhere else.

    See ``organizations.auth_backends.OrganizationModelBackend
    .get_membership_permissions`` for what is deliberately *not* consulted (the
    global permission half, and the superuser short-circuit) and why.

    A fresh backend instance per call is free: the class holds no state, and
    every cache it fills lives on the ``user`` object, so a second call shares
    the first one's cached sets. Instantiated directly rather than picked out of
    ``get_backends()`` because this asks *our* backend a question the
    ``AUTHENTICATION_BACKENDS`` protocol does not define -- there is no other
    entry that could answer it.

    Imported lazily for the same reason ``_organization_by_pk`` is: this module
    is imported from ``users.models``.
    """
    from organizations.auth_backends import OrganizationModelBackend

    return permission in OrganizationModelBackend().get_membership_permissions(user, organization)


def has_organization_permission(
    user: User | None,
    permission: str,
    organization: Organization | int | str | None,
) -> bool:
    """Whether ``user`` holds ``permission`` **through an active membership in
    ``organization``**.

    ``permission`` is an ``"app_label.codename"`` string from
    ``organizations.permission_catalog``. ``organization`` may be an
    ``Organization`` or its pk -- the pk form costs one extra query, and only
    when the organization asked about is not the one already bound.

    Returns ``False`` for an anonymous caller, for an organization that does not
    exist, and for a caller with no active membership in it -- so a caller can
    pass a resolved-or-``None`` value straight in.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if organization is None:
        return False

    # ``hasattr(..., "pk")`` rather than an ``isinstance`` on ``int``: callers
    # pass an ``Organization``, a ``LazyObject`` standing in for one (which
    # proxies ``pk``), or a bare pk -- and a pk is not always an ``int``
    # (``User.is_organization_admin``'s published signature is "instance or id").
    target: Organization | None
    if hasattr(organization, "pk"):
        target = cast("Organization", organization)
        organization_pk: int | str | None = target.pk
    else:
        target = None
        organization_pk = cast("int | str", organization)
    if organization_pk is None:
        return False

    current = get_current_organization()
    # ``getattr`` rather than ``current.pk``: the bound value may be a
    # ``SimpleLazyObject`` that resolves to ``None`` (the slug-form binding),
    # which has no ``pk``. Unreachable from here today; cheaper than relying on
    # that staying true.
    if current is not None and str(getattr(current, "pk", None)) == str(organization_pk):
        # The overwhelmingly common case on the request path:
        # ``TenantScopedViewMixin`` has already bound the resolved organization
        # and the permission class is asking about that same one. No rebinding,
        # no query.
        return _holds_through_a_membership(user, permission, current)

    if target is None:
        target = _organization_by_pk(organization_pk)
    if target is None:
        return False

    with organization_context(target):
        return _holds_through_a_membership(user, permission, target)
