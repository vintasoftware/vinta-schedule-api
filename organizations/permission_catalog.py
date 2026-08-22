"""The capability permissions and the three global groups that carry them.

One place to name the four custom permissions declared as ``Meta.permissions``
on ``Organization`` / ``OrganizationMembership`` / ``Subscription``, and the
group-to-permission mapping the seed data migration writes.

Two deliberate shapes:

* **Named for capabilities, not for CRUD.** ``manage_billing`` rather than
  ``change_subscription``: the authorization questions this codebase asks are
  behavioural ("may this member change the plan"), and mapping them onto the
  model-CRUD triples ``auth.Permission`` defaults to would misrepresent them.
* **The groups are global rows, shared by every organization.** Per-organization
  scoping comes from the *membership* the group hangs off
  (``OrganizationMembership.groups``), not from the group. Every authorization
  check resolves a capability from the active membership, never a group name, so a
  per-organization group layer can be added later without touching a call site.

Permission classes and published membership serializers consume these constants,
so the migration, the services, the runtime checks, and the tests all spell the
same strings.

The migration that seeds these does **not** import this module: a data
migration must keep working when the live code moves on, so it carries its own
frozen copy of these literals (see ``AGENTS.md`` on data migrations re-deriving
their logic). ``organizations/tests/test_group_backfill_migration.py`` pins the
two against the same literals, so a drift is a test failure rather than a
silent divergence.

The seeders at the bottom of this module are therefore the *runtime* half only:
``ORGANIZATION_GROUP_SEEDERS`` points at them so a transactional test's flush can
be repaired from head state, and nothing in ``organizations/migrations/`` calls
them.
"""

from __future__ import annotations

from functools import reduce
from typing import TYPE_CHECKING

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import F, Q

from organizations.exceptions import OrganizationGroupNotAssignableError


if TYPE_CHECKING:
    from collections.abc import Iterable


def _label(app_label: str, _model: str, codename: str, _name: str) -> str:
    """The ``"app_label.codename"`` half of a catalog row.

    Derived rather than written twice: the four constants below are the strings
    every permission check reads, and the four tuples they come from are what the
    seeder needs to *create* the row. Spelling both by hand is how they drift.
    """
    return f"{app_label}.{codename}"


#: ``OrganizationMembership`` -- may add, remove, deactivate and re-group members.
MANAGE_MEMBERS_TUPLE = (
    "organizations",
    "organizationmembership",
    "manage_members",
    "Can manage the organization's members",
)

#: ``Organization`` -- may change the organization's own settings.
MANAGE_ORGANIZATION_TUPLE = (
    "organizations",
    "organization",
    "manage_organization",
    "Can manage the organization's settings",
)
#: ``Organization`` -- the *role* half of the branding write gate. The
#: entitlement half (``white_label_branding``) is a separate, unrelated check;
#: holding this permission is not on its own enough to write branding.
MANAGE_BRANDING_TUPLE = (
    "organizations",
    "organization",
    "manage_branding",
    "Can manage the organization's branding",
)

#: ``vinta_billing.Subscription`` -- may change the plan, buy add-ons, manage the
#: payment method. Also what ``billing_recipients`` reads to decide who receives
#: the dunning ladder, so "who may write billing" and "who is told about it"
#: derive from one source.
#:
#: The app label is ``vinta_billing``, not ``payments``: the billing models moved
#: to the package in
#: ``payments/migrations/0024_move_billing_to_vinta_billing.py``, which also
#: re-grants this permission on the new content type and deletes the old one.
#: ``organizations/migrations/0028_seed_permission_groups.py`` still says
#: ``payments`` and always will -- it describes the world as it was, and 0023
#: depends on it.
MANAGE_BILLING_TUPLE = (
    "vinta_billing",
    "subscription",
    "manage_billing",
    "Can manage the organization's billing",
)

MANAGE_MEMBERS = _label(*MANAGE_MEMBERS_TUPLE)
MANAGE_ORGANIZATION = _label(*MANAGE_ORGANIZATION_TUPLE)
MANAGE_BRANDING = _label(*MANAGE_BRANDING_TUPLE)
MANAGE_BILLING = _label(*MANAGE_BILLING_TUPLE)


GROUP_ORGANIZATION_ADMIN = "organization_admin"
GROUP_ORGANIZATION_BILLING_OWNER = "organization_billing_owner"
GROUP_ORGANIZATION_MEMBER = "organization_member"

#: ``(app_label, model, codename, name)`` for each of the four, in the shape
#: ``auth.Permission`` rows are created from. The four ``*_TUPLE`` constants above
#: exist for this list: the seeder has to be able to *create* a missing permission
#: row, and the ``"app_label.codename"`` strings every call site reads carry neither
#: the model that owns it nor its display name.
#:
#: ``organizations/migrations/0028_seed_permission_groups.py`` keeps its own frozen
#: copy of the same four rows; ``organizations/tests/test_group_backfill_migration.py``
#: pins both against a third set of literals.
PERMISSIONS = [
    MANAGE_ORGANIZATION_TUPLE,
    MANAGE_BRANDING_TUPLE,
    MANAGE_MEMBERS_TUPLE,
    MANAGE_BILLING_TUPLE,
]


#: The seeded mapping. ``organization_member`` is deliberately empty: it exists
#: so that "has a membership and no capabilities" is distinguishable from "has
#: no membership at all", and so a future per-organization layer has a base to
#: extend. A permission granted to every member would be indistinguishable from
#: no check at all.
GROUP_PERMISSIONS: dict[str, tuple[str, ...]] = {
    GROUP_ORGANIZATION_ADMIN: (
        MANAGE_MEMBERS,
        MANAGE_ORGANIZATION,
        MANAGE_BRANDING,
        MANAGE_BILLING,
    ),
    GROUP_ORGANIZATION_BILLING_OWNER: (MANAGE_BILLING,),
    GROUP_ORGANIZATION_MEMBER: (),
}


def _permissions_by_label() -> dict[str, Permission]:
    """The catalog's four ``auth.Permission`` rows, keyed by ``"app_label.codename"``.

    One query rather than four, and each ``Q`` keeps its codename and its app label
    inside the same clause so both bind to the same row -- the reason
    ``vinta_orgs.querysets.filter_memberships_holding_permission`` spells it that way
    too. A codename declared in another app must not satisfy half of the condition.
    """
    permissions_query = reduce(
        lambda left, right: left | right,
        [
            Q(content_type__app_label=app_label, codename=codename)
            for app_label, _model, codename, _name in PERMISSIONS
        ],
        Q(),
    )
    permissions = Permission.objects.filter(permissions_query).annotate(
        app_label=F("content_type__app_label")
    )
    return {
        f"{permission.app_label}.{permission.codename}": permission for permission in permissions
    }


def canonical_groups(group_names: Iterable[str]) -> tuple[str, ...]:
    """The seeded groups a membership asked for ``group_names`` actually holds.

    The single definition of "which of the three seeded groups does this
    membership belong in", shared by every write path that puts a membership in
    groups (``organizations.services.assign_membership_groups``) and by the intent
    of the ``organizations/migrations/0029_backfill_membership_groups.py``
    backfill.

    Canonicalising rather than storing the request verbatim is what keeps the
    stored set a *function* of the capabilities asked for, so two requests that
    mean the same thing store the same thing:

    * ``organization_member`` carries no permission, so it is dropped whenever a
      capability group is present -- ``["organization_admin",
      "organization_member"]`` stores the admin group alone.
    * A membership asked for no capability group at all is put in
      ``organization_member``, so "a member with no capabilities" stays
      distinguishable from "no membership".
    * ``organization_admin`` and ``organization_billing_owner`` are independent
      and both are kept when both are asked for, even though the admin group
      already carries ``manage_billing`` -- dropping the narrower one would make
      a later demotion out of ``organization_admin`` silently remove billing
      access the caller asked for separately.

    Any name that is not one of the three seeded groups contributes nothing;
    validating that the caller named a *known* group is the request
    serializer's job, not this function's.
    """
    names = set(group_names)
    kept: list[str] = []
    if GROUP_ORGANIZATION_ADMIN in names:
        kept.append(GROUP_ORGANIZATION_ADMIN)
    if GROUP_ORGANIZATION_BILLING_OWNER in names:
        kept.append(GROUP_ORGANIZATION_BILLING_OWNER)
    if not kept:
        kept.append(GROUP_ORGANIZATION_MEMBER)
    return tuple(kept)


def seed_organization_groups() -> list[Group]:
    """Create (or repair) the three seeded groups from **this module's live catalog**.

    The callable ``SHARED_SCHEMA_ORGANIZATIONS["ORGANIZATION_GROUP_SEEDERS"]``
    names, so ``vinta_orgs.testing.reseed_organization_groups()`` can put the
    catalog back after a transactional test flushes ``auth_group`` /
    ``auth_group_permissions`` -- see that module's docstring for why the
    repair has to happen at test *setup* rather than teardown.

    Reads ``GROUP_PERMISSIONS`` above -- head state, not
    ``organizations/migrations/0028_seed_permission_groups.py``'s frozen
    literals. That migration stays the production path and deliberately keeps
    its own copy (a data migration must keep meaning what it meant when it was
    written); this is the same catalog, reachable as a callable, for tests.

    Additive and idempotent, the contract
    ``vinta_orgs.testing.reseed_organization_groups`` requires of every
    seeder: ``get_or_create`` for the group, ``add`` (not ``set``) for its
    permissions, so a second call -- or a call against an already-seeded
    database -- changes nothing and never revokes a permission a caller added
    on purpose.

    **The permission rows are created here too**, by ``seed_capability_permissions``
    below, rather than looked up and skipped when absent. The flush that destroys the
    groups also destroys ``auth_permission``; ``post_migrate`` rebuilds those, but a
    seeder that merely *hoped* it had already run reported success while leaving
    ``organization_admin`` empty -- a group with no permissions denies everything, and
    that denial reads as an authorization bug in whichever unrelated module asserts
    next. Creating the row on the same key ``create_permissions`` de-duplicates on
    makes the two orders equivalent.
    """
    groups: list[Group] = []

    with transaction.atomic():
        seed_capability_permissions()
        groups = seed_capability_groups()

    return groups


#: The groups an *invitation* may carry. Narrower than the full set on purpose:
#: an invitation records its future membership's standing in a single
#: ``group`` column, so it can name one group and only one. Refused explicitly
#: rather than silently dropped -- a caller who asked for
#: ``organization_billing_owner`` would otherwise believe they had granted it.
INVITABLE_GROUPS: tuple[str, ...] = (GROUP_ORGANIZATION_MEMBER, GROUP_ORGANIZATION_ADMIN)


def group_for_invitation_groups(group_names: Iterable[str]) -> str:
    """The single group name an invitation naming ``group_names`` stores.

    The public GraphQL invitation input accepts a *list*, because that is the
    shape ``POST /organization-members/{user_id}/groups/`` accepts once the
    invitation has been accepted; ``OrganizationInvitation.group`` holds one.
    This is the one narrowing between the two, kept here beside
    ``INVITABLE_GROUPS`` rather than inline in the mutation.

    :raises OrganizationGroupNotAssignableError: for a name that is not a
        seeded group, and for ``organization_billing_owner`` (see
        ``INVITABLE_GROUPS``). Both are refusals, not silent no-ops.
    """
    names = list(group_names)
    unusable = [name for name in names if name not in INVITABLE_GROUPS]
    if unusable:
        raise OrganizationGroupNotAssignableError(
            f"Cannot assign {', '.join(sorted(unusable))} to an invitation. "
            f"Allowed groups: {', '.join(INVITABLE_GROUPS)}."
        )
    if GROUP_ORGANIZATION_ADMIN in names:
        return GROUP_ORGANIZATION_ADMIN
    return GROUP_ORGANIZATION_MEMBER


def permissions_for_groups(group_names: Iterable[str]) -> frozenset[str]:
    """Every ``"app_label.codename"`` the named seeded groups carry between them.

    Answers "would a membership in these groups hold this capability" without a
    query -- which is what the last-admin guard needs *before* it writes, to
    decide whether the assignment about to happen removes
    ``organizations.manage_members`` from its target. Unknown names contribute
    nothing.

    **Groups only.** Callers combine this prospective group result with the
    target's retained direct permissions when they need the post-write answer.
    """
    return frozenset(
        permission for name in group_names for permission in GROUP_PERMISSIONS.get(name, ())
    )


def seed_capability_permissions() -> list[Permission]:
    """Create the catalog's four ``auth.Permission`` rows if they are absent.

    ``get_or_create`` on ``(content_type, codename)`` -- the same key
    ``django.contrib.auth.management.create_permissions`` de-duplicates on -- so this
    and ``post_migrate`` can run in either order and produce one row each.

    Runtime only. ``organizations/migrations/0028_seed_permission_groups.py`` does the
    equivalent thing against historical models with its own frozen literals, and does
    not call this.
    """
    permissions: list[Permission] = []
    for app_label, model, codename, name in PERMISSIONS:
        content_type, _created = ContentType.objects.get_or_create(app_label=app_label, model=model)
        permission, _created = Permission.objects.get_or_create(
            content_type=content_type,
            codename=codename,
            defaults={"name": name},
        )
        permissions.append(permission)

    return permissions


def seed_capability_groups() -> list[Group]:
    """Create the three seeded groups and attach the permissions they carry.

    Call ``seed_capability_permissions`` first (``seed_organization_groups`` does): a
    label in ``GROUP_PERMISSIONS`` with no matching row **raises**, it is not skipped.
    Once the rows are created rather than merely hoped for, the only way to reach that
    branch is a label naming a permission the catalog does not declare -- a bug in this
    module, not a transient state -- and a seeder that logged and continued would hand
    back an ``organization_admin`` carrying nothing while reporting success. An empty
    admin group denies everything, and the denial surfaces as an authorization failure
    somewhere else entirely.
    """
    permissions_by_label = _permissions_by_label()

    groups: list[Group] = []
    for group_name, labels in GROUP_PERMISSIONS.items():
        group, _created = Group.objects.get_or_create(name=group_name)
        # ``add`` rather than ``set``: additive and idempotent, and it does not
        # revoke a grant an operator added on purpose.
        for label in labels:
            try:
                permission = permissions_by_label[label]
            except KeyError:
                raise LookupError(
                    f"Cannot seed {group_name!r}: no auth.Permission exists for {label!r}. "
                    "Every label in GROUP_PERMISSIONS must be declared in PERMISSIONS, "
                    "which seed_capability_permissions() creates the rows from."
                ) from None
            group.permissions.add(permission)
        groups.append(group)

    return groups
