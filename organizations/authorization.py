"""The one way this repository asks "may this user do X in organization Y".

Every authorization decision reads a *permission*, never a role column and never
a group name -- see the plan's **Group scope** and **Permission catalog shape**
Guiding Decisions
(``ai-plans/2026-08-12-VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md``).
The codenames themselves live in ``organizations.permission_catalog``.

**The rule itself is the package's.** ``vinta_orgs.authorization
.has_organization_permission`` resolves a permission from an *active* membership
in the organization *named* -- it binds that organization for the duration of the
lookup, restores the previous binding afterwards, accepts an ``Organization`` or a
bare pk, and answers ``False`` for an anonymous caller, an inactive user, an
organization that does not exist and a caller with no active membership in it.
This repository consumed that rather than keeping the equivalent hand-written
helper it had, per the plan's **Package owns the authorization substrate**
Guiding Decision.

**What this module is for**, then, is the two keyword arguments below. They are
this project's policy, not the package's, and they are spelled out at the one
call the whole repository goes through so that the policy is a *declaration in
our code* rather than an inherited default.

*Why not a bare ``user.has_perm(...)`` at each call site.* Two independent
reasons, and each on its own is sufficient.

*The organization.* ``vinta_orgs.auth_backends.OrganizationModelBackend``
resolves a membership's permissions for *the organization bound to the current
context* and for no other, and that is what ``has_perm`` reaches. Every call site
this replaces named its organization explicitly -- ``membership.is_admin`` is a
statement about ``membership.organization``, and
``User.is_organization_admin(organization)`` takes the organization as an
argument. The reseller-root branch retains an explicit organization check for
its low-level subtree policy:

* ``IsBillingOwnerOrAdmin``'s acting-reseller-root branch asks about an
  *ancestor* of the bound organization (the whole point of the branch).
  The package header resolver cannot currently produce a request whose resolved
  membership belongs to a different organization from the binding, so this
  branch is not currently decisive on an endpoint. Keeping the named check
  preserves the policy without claiming that request shape exists.

*The source of the grant.* ``has_perm`` answers from the **union** of the
organization half with a global half (``user.user_permissions`` plus the user's
own ``auth.Group`` rows) and, before any backend runs at all, from
``PermissionsMixin``'s superuser short-circuit. Neither of those is a statement
about the organization named, and neither could grant anything under the ``role``
/ ``is_billing_owner`` columns this phase replaces: a global
``organizations.manage_members`` was inert, membership of the seeded
``organization_admin`` group (a plain global ``auth.Group``, listed by the user
form's group picker in ``users/admin.py``) was inert, and a superuser without an
admin membership did **not** satisfy ``role == ADMIN``. Identical outcomes is
this phase's contract, and admitting any of those three would break it -- which
is why ``INCLUDE_GLOBAL_PERMISSIONS`` and ``ALLOW_SUPERUSER`` below are ``False``
and are *passed*, not assumed.

``has_perm`` itself is untouched: the Django admin and every other
``ModelBackend`` consumer keep stock semantics. The narrowing is visible only to
callers of this function.

**One deliberate difference from the role columns this replaces**, named here
because "identical outcomes" is this migration phase's contract:

* **An inactive *user* passes nothing.** The package gates on ``user.is_active``;
  the role checks gated only on ``membership.is_active``. This narrows rather
  than widens, and is unreachable from the request path (authentication refuses
  an inactive user first).

A second one was claimed here and is **withdrawn**: "a superuser passes every
check", justified as granting nothing new because a superuser already reads every
tenant through the Django admin. It was never true of the code being replaced --
``membership.role == ADMIN`` refused a superuser who held no admin membership --
and the "already reaches everything" argument does not survive the billing
endpoints, where passing ``IsBillingOwnerOrAdmin`` changes a tenant's plan, buys
an add-on, or cancels a subscription **at Stripe / MercadoPago**. The Django
admin exposes no such button. A superuser is now answered from their memberships
like anybody else; pinned by ``organizations/tests/test_permissions_parity.py``.

The *membership*'s ``is_active`` gate is not lost either: ``0.3.0`` filters it
inside ``OrganizationModelBackend._get_membership``, so the per-organization
cache never holds a row nothing is allowed to use. Do not re-add it here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vinta_orgs import authorization as vinta_orgs_authorization


if TYPE_CHECKING:
    from organizations.models import Organization
    from users.models import User


#: Whether a grant made outside any organization counts here. **No.**
#:
#: ``user.user_permissions`` and the user's own global ``auth.Group`` rows are
#: not scoped to an organization, so one row added once in the Django user admin
#: -- or one click in that form's ``groups`` picker, which lists the seeded
#: ``organization_admin`` group -- would become all four capabilities in *every*
#: organization in the database. Both were inert under the ``role`` column this
#: phase replaces, so admitting either is a widening rather than a migration.
#:
#: **It also admits every superuser, regardless of ``ALLOW_SUPERUSER`` below.**
#: The global half is fetched through ``vinta_orgs.auth_backends
#: .OrganizationModelBackend.get_all_global_permissions``, which applies its own
#: ``if user_obj.is_superuser: perms = Permission.objects.all()`` short-circuit
#: (``_get_global_permissions``) -- ``ALLOW_SUPERUSER`` guards only the
#: *organization* half's short-circuit, and never runs when this one has already
#: answered. So the two flags are not independent in the widening direction:
#: turning this one on is strictly the larger change of the two. Mutating each in
#: turn and running ``organizations/ calendar_integration/ public_api/ users/
#: payments/ common/`` shows it as a **strict superset**, not merely a bigger
#: number -- ``ALLOW_SUPERUSER = True`` alone turns 5 rows red,
#: ``INCLUDE_GLOBAL_PERMISSIONS = True`` alone turns 10, and the 10 contain all
#: 5. Every superuser escalation ``ALLOW_SUPERUSER`` guards is reachable through
#: this flag as well.
INCLUDE_GLOBAL_PERMISSIONS = False

#: Whether ``is_superuser`` short-circuits the membership lookup. **No.**
#:
#: ``role == ADMIN`` refused a superuser who held no admin membership, so this is
#: parity, not policy. "They already reach everything through the Django admin"
#: is not an argument on ``IsBillingOwnerOrAdmin``: passing it changes a plan,
#: buys an add-on or cancels a subscription at Stripe / MercadoPago, and the
#: admin exposes no such button.
#:
#: This flag is **not** on its own what keeps superusers out: it guards the
#: organization half's short-circuit only, and the global half carries its own.
#: See ``INCLUDE_GLOBAL_PERMISSIONS`` above -- both must stay ``False``.
ALLOW_SUPERUSER = False


def has_organization_permission(
    user: User | None,
    permission: str,
    organization: Organization | int | None,
) -> bool:
    """Whether ``user`` holds ``permission`` **through an active membership in
    ``organization``**.

    ``permission`` is an ``"app_label.codename"`` string from
    ``organizations.permission_catalog``. ``organization`` may be an
    ``Organization``, a ``LazyObject`` standing in for one, or a bare pk -- the pk
    form costs one extra query, and only when the organization asked about is not
    the one already bound.

    Returns ``False`` for an anonymous caller, for an organization that does not
    exist, and for a caller with no active membership in it -- so a caller can
    pass a resolved-or-``None`` value straight in.

    A pk must be an ``int``, not an arbitrary ``str``. "Does not exist" is
    answered by ``Organization.objects.filter(pk=...).first()``, and a
    non-numeric string reaches that as a ``ValueError`` rather than as ``False``
    -- a 500 out of a permission class. The annotation is narrowed rather than
    the value coerced: no caller in this repository passes a string, and
    inventing a coercion here would make the permission layer responsible for
    validating identifiers the URL/serializer layer already validates.

    Restated here in terms of *our* ``Organization`` and ``User`` (the package
    annotates against its own swappable models and a structural user protocol),
    the same way ``common.organization_context`` restates ``vinta_orgs.state``.
    The two module-level constants are passed rather than left to default: what
    they exclude is the subject of this phase's parity matrix, and a policy that
    important should not be inherited silently from a dependency's defaults.
    """
    return vinta_orgs_authorization.has_organization_permission(
        user,
        permission,
        organization,
        include_global=INCLUDE_GLOBAL_PERMISSIONS,
        allow_superuser=ALLOW_SUPERUSER,
    )
