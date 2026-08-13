"""The one way this repository asks "may this user do X in organization Y".

Every authorization decision reads a *permission* (``user.has_perm``), never a
role column and never a group name -- see the plan's **Group scope** and
**Permission catalog shape** Guiding Decisions
(``ai-plans/2026-08-12-VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md``).
The codenames themselves live in ``organizations.permission_catalog``.

**Why a helper rather than a bare ``user.has_perm(...)`` at each call site.**
``vinta_orgs.auth_backends.OrganizationModelBackend`` resolves a membership's
permissions for *the organization bound to the current context* and for no
other. Every call site this replaces named its organization explicitly --
``membership.is_admin`` is a statement about ``membership.organization``, and
``User.is_organization_admin(organization)`` takes the organization as an
argument. Two of those call sites ask about an organization that is **not** the
bound one, and a bare ``has_perm`` would answer the wrong question in both:

* ``IsBillingOwnerOrAdmin``'s acting-reseller-root branch asks about an
  *ancestor* of the bound organization (the whole point of the branch).
* Roughly a dozen DRF views are not built on ``TenantScopedViewMixin`` and so
  bind nothing at all (``AGENTS.md``, Multi-Tenancy). ``ServiceAccountViewSet``
  is one of them and carries ``IsOrganizationAdmin``; under a bare ``has_perm``
  it would resolve no organization permissions and refuse *every* caller.

So the helper binds the named organization for the duration of the check and
restores the previous binding afterwards. The backend caches per organization
pk, so a check against a second organization neither reads nor poisons the
first one's cached set.

**Two deliberate differences from the role columns it replaces**, both named
here because "identical outcomes" is this migration phase's contract:

* **A superuser passes every check.** ``PermissionsMixin.has_perm``
  short-circuits ``is_superuser`` before any backend runs, where
  ``membership.role == ADMIN`` did not. This grants nothing new in practice: a
  superuser already reaches every tenant's data through the Django admin, which
  is a wider surface than any of these endpoints. Pinned by
  ``organizations/tests/test_permissions_parity.py`` so it stays a decision
  rather than an accident.
* **An inactive *user* passes nothing.** ``ModelBackend`` gates on
  ``user.is_active``; the role checks gated only on ``membership.is_active``.
  This narrows rather than widens, and is unreachable from the request path
  (authentication refuses an inactive user first).

The *membership*'s ``is_active`` gate is not lost: ``organizations.auth_backends
.OrganizationModelBackend`` resolves nothing for a deactivated membership
(Phase 3.5). Do not re-add it here.
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


def has_organization_permission(
    user: User | None,
    permission: str,
    organization: Organization | int | str | None,
) -> bool:
    """Whether ``user`` holds ``permission`` **in ``organization``**.

    ``permission`` is an ``"app_label.codename"`` string from
    ``organizations.permission_catalog``. ``organization`` may be an
    ``Organization`` or its pk -- the pk form costs one extra query, and only
    when the organization asked about is not the one already bound.

    Returns ``False`` for an anonymous caller and for an organization that does
    not exist, so a caller can pass a resolved-or-``None`` value straight in.
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
    if current is not None and str(current.pk) == str(organization_pk):
        # The overwhelmingly common case on the request path:
        # ``TenantScopedViewMixin`` has already bound the resolved organization
        # and the permission class is asking about that same one. No rebinding,
        # no query.
        return user.has_perm(permission)

    if target is None:
        target = _organization_by_pk(organization_pk)
    if target is None:
        return False

    with organization_context(target):
        return user.has_perm(permission)
