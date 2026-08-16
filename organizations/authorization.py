"""The one way this repository asks "may this user do X in organization Y".

Two spellings, for two shapes of the same question:
:func:`has_organization_permission` takes a ``(user, organization)`` pair and is
what every permission class asks; :func:`membership_holds_permission` takes a
membership row the caller already holds, and is what the handful of call sites
outside the permission classes ask.

Every authorization decision reads a *permission*, never a role column and never
a group name. The three seeded groups are global ``auth.Group`` rows shared by
every organization -- per-organization scoping comes from the *membership* the
group hangs off, not from the group -- so a group name says nothing on its own,
and the permissions themselves are named for capabilities ("may this member
change the plan") rather than for model CRUD. The codenames live in
``organizations.permission_catalog``.

**The rule itself is the package's.** ``vinta_orgs.authorization
.has_organization_permission`` resolves a permission from an *active* membership
in the organization *named* -- it binds that organization for the duration of the
lookup, restores the previous binding afterwards, accepts an ``Organization`` or a
bare pk, and answers ``False`` for an anonymous caller, an inactive user, an
organization that does not exist and a caller with no active membership in it.
This repository consumed that rather than keeping the equivalent hand-written
helper it had: the authorization substrate is the ``vinta_orgs`` package's to
own, and a local copy of it would be one more thing to keep in step.

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
about the organization named, and neither could grant anything under the two
flat columns these permission checks replaced
(``OrganizationMembership.role`` and ``OrganizationMembership.is_billing_owner``):
a global ``organizations.manage_members`` was inert, membership of the seeded
``organization_admin`` group (a plain global ``auth.Group``, listed by the user
form's group picker in ``users/admin.py``) was inert, and a superuser without an
admin membership did **not** satisfy ``role == ADMIN``. Producing outcomes
identical to those columns is this module's contract, and admitting any of those
three would break it -- which
is why ``INCLUDE_GLOBAL_PERMISSIONS`` and ``ALLOW_SUPERUSER`` below are ``False``
and are *passed*, not assumed.

``has_perm`` itself is untouched: the Django admin and every other
``ModelBackend`` consumer keep stock semantics. The narrowing is visible only to
callers of this function.

**One deliberate difference from the role columns this replaces**, named here
because "identical outcomes to those columns" is this module's contract:

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

from organizations.permission_catalog import MANAGE_MEMBERS as _MANAGE_MEMBERS


if TYPE_CHECKING:
    from organizations.models import Organization, OrganizationMembership
    from users.models import User


#: Whether a grant made outside any organization counts here. **No.**
#:
#: ``user.user_permissions`` and the user's own global ``auth.Group`` rows are
#: not scoped to an organization, so one row added once in the Django user admin
#: -- or one click in that form's ``groups`` picker, which lists the seeded
#: ``organization_admin`` group -- would become all four capabilities in *every*
#: organization in the database. Both were inert under the ``role`` column these
#: checks replaced, so admitting either is a widening rather than a migration.
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
    they exclude is exactly what ``organizations/tests/test_permissions_parity.py``
    pins, and a policy that important should not be inherited silently from a
    dependency's defaults.
    """
    return vinta_orgs_authorization.has_organization_permission(
        user,
        permission,
        organization,
        include_global=INCLUDE_GLOBAL_PERMISSIONS,
        allow_superuser=ALLOW_SUPERUSER,
    )


def membership_holds_permission(membership: OrganizationMembership, permission: str) -> bool:
    """Whether **this membership row** carries ``permission``.

    The membership-shaped sibling of :func:`has_organization_permission`, and
    the replacement for the ``membership.is_admin`` property that was deleted
    along with the ``role`` column. Use it where
    the question is about a membership the caller already holds, and *not* about
    a ``(user, organization)`` pair -- three call sites outside the permission
    classes ask exactly that:

    * ``public_api.scoping`` -- what standing does the membership a token is
      scoped to have;
    * ``calendar_integration.querysets.ExternalEventChangeRequestQuerySet
      .resolvable_by`` and ``ExternalEventChangeRequestService.can_resolve`` --
      the same question, asked of a membership resolved by the caller.

    Deliberately **not** implemented in terms of ``has_organization_permission``,
    even though the two agree everywhere both are defined. That one takes a
    ``user``, and so applies the auth backend's ``user.is_active`` gate and
    needs ``membership.user`` loaded; ``membership.is_admin`` applied neither,
    and each of the three call sites above already owns whichever ``is_active``
    gate it wants. Routing through the user would therefore have been a silent
    behaviour change dressed as a refactor.

    One query, and no new predicate: it is
    ``OrganizationMembershipQuerySet.holding_permission`` -- the same union of
    the membership's direct ``permissions`` grant with the permissions its
    ``groups`` carry that the last-admin guard counts by -- narrowed to a single
    row. Under the group catalog this replaces, ``role == ADMIN`` and
    "holds ``organizations.manage_members``" are the same set, because
    ``organization_admin`` is the only seeded group carrying it.

    **Precondition: ``membership`` is saved.** The lookup is by ``pk``, so an
    unsaved row filters on ``pk=None``, matches nothing, and returns ``False`` --
    "holds no permission", indistinguishable from a real denial. The
    ``membership.is_admin`` property this replaced answered from memory and so
    had no such precondition. No caller passes an unsaved membership today (all
    three resolve theirs from the database), which is why this is documented
    rather than enforced; a caller that starts building memberships in memory
    must assign groups and save before asking.
    """
    # Late, for symmetry with this module's other model imports rather than out of
    # necessity: ``organizations.models`` does not reach back here today, but it is
    # imported by ``users.models``, which this module is imported *from*, so a
    # module-scope import here is one refactor away from a cycle.
    from organizations.models import OrganizationMembership as MembershipModel

    return MembershipModel.objects.filter(pk=membership.pk).holding_permission(permission).exists()


#: The two values published as a *description* of a membership's standing: the
#: ``membership_role`` key in the ``organization_member_created`` webhook
#: payload, and the ``audit.Audit.actor_role`` snapshot column. Neither
#: authorizes anything. They were kept verbatim when the ``role`` column was
#: dropped, because changing either would be a partner-visible payload change nobody was
#: told about, and because every audit row already on disk holds one of them --
#: writing something else would split the audit history in two silently, which
#: is exactly the trap the withdrawn app rename was withdrawn to avoid.
MEMBERSHIP_ROLE_LABEL_ADMIN = "admin"
MEMBERSHIP_ROLE_LABEL_MEMBER = "member"


def membership_role_label(membership: OrganizationMembership) -> str:
    """``"admin"`` or ``"member"`` -- the published description of a membership.

    Derived from ``organizations.manage_members`` rather than read from the
    dropped ``role`` column. The two agree: ``role == ADMIN`` memberships
    were backfilled into ``organization_admin``, the only seeded group carrying
    that permission, and every write path since keeps the two in step.

    **Not an authorization input.** Nothing may branch on the return value to
    decide whether an action is allowed -- ask
    :func:`membership_holds_permission` or :func:`has_organization_permission`
    for that. This exists only so the two surfaces that publish a role *name*
    (see :data:`MEMBERSHIP_ROLE_LABEL_ADMIN`) keep publishing the same names.

    Costs one query per call, where the column cost none. Three callers, not
    two: the webhook payload builder, the ``audit.Audit`` row writer
    (``AuditService.actor_from_membership``), and -- indirectly --
    ``AuditService.actor_from_user``, which delegates to
    ``actor_from_membership`` and is itself reached from
    ``actor_from_user_or_token``. Each writes a row per call, so the extra query
    is proportionate *per call*; what it is not proportionate to is a caller
    that builds one actor snapshot per row in a loop. Hoist the snapshot out of
    the loop instead of reaching past this function --
    ``CalendarGroupService._delete_group_scoped_rows_for_removed_calendars``
    is the worked example.
    """
    if membership_holds_permission(membership, _MANAGE_MEMBERS):
        return MEMBERSHIP_ROLE_LABEL_ADMIN
    return MEMBERSHIP_ROLE_LABEL_MEMBER
