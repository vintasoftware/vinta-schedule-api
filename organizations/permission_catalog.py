"""The capability permissions and the three global groups that carry them.

One place to name the four custom permissions declared as ``Meta.permissions``
on ``Organization`` / ``OrganizationMembership`` / ``Subscription``, and the
group-to-permission mapping the seed data migration writes.

Two deliberate shapes, both from the plan's Guiding Decisions
(``ai-plans/2026-08-12-VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md``):

* **Named for capabilities, not for CRUD.** ``manage_billing`` rather than
  ``change_subscription``: the authorization questions this codebase asks are
  behavioural ("may this member change the plan"), and mapping them onto the
  model-CRUD triples ``auth.Permission`` defaults to would misrepresent them.
* **The groups are global rows, shared by every organization.** Per-organization
  scoping comes from the *membership* the group hangs off
  (``OrganizationMembership.groups``), not from the group. Every authorization
  check reads ``user.has_perm("app.codename")``, never a group name, so a
  per-organization group layer can be added later without touching a call site.

Nothing in this module is consulted by a permission class yet -- Phase 4 does
that. The constants exist now so the migration, the services' dual-write and
the tests all spell the same strings.

The migration that seeds these does **not** import this module: a data
migration must keep working when the live code moves on, so it carries its own
frozen copy of these literals (see ``AGENTS.md`` on data migrations re-deriving
their logic). ``organizations/tests/test_group_backfill_migration.py`` pins the
two against the same literals, so a drift is a test failure rather than a
silent divergence.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.auth.models import Group, Permission
from django.db import transaction


logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from collections.abc import Iterable


#: ``OrganizationMembership`` -- may add, remove, deactivate and re-group members.
MANAGE_MEMBERS = "organizations.manage_members"

#: ``Organization`` -- may change the organization's own settings.
MANAGE_ORGANIZATION = "organizations.manage_organization"

#: ``Organization`` -- the *role* half of the branding write gate. The
#: entitlement half (``white_label_branding``) is a separate, unrelated check;
#: holding this permission is not on its own enough to write branding.
MANAGE_BRANDING = "organizations.manage_branding"

#: ``payments.Subscription`` -- may change the plan, buy add-ons, manage the
#: payment method. Also what ``billing_recipients`` reads to decide who receives
#: the dunning ladder, so "who may write billing" and "who is told about it"
#: derive from one source.
MANAGE_BILLING = "payments.manage_billing"


GROUP_ORGANIZATION_ADMIN = "organization_admin"
GROUP_ORGANIZATION_BILLING_OWNER = "organization_billing_owner"
GROUP_ORGANIZATION_MEMBER = "organization_member"


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


def groups_for_membership_state(*, is_admin: bool, is_billing_owner: bool) -> tuple[str, ...]:
    """The group names a membership in this ``role``/``is_billing_owner`` state holds.

    The single definition of the mapping, shared by the Phase 3 backfill's
    intent and by the temporary dual-write in ``organizations.services``. An
    admin is also a billing owner by permission (``organization_admin`` carries
    ``manage_billing``), so the admin group alone is enough where
    ``is_billing_owner`` is not separately set -- but a membership that is both
    gets both groups, because ``is_billing_owner`` is an independent column and
    dropping it on the floor would lose information Phase 6 has not yet
    retired.
    """
    names: list[str] = []
    if is_admin:
        names.append(GROUP_ORGANIZATION_ADMIN)
    if is_billing_owner:
        names.append(GROUP_ORGANIZATION_BILLING_OWNER)
    if not names:
        names.append(GROUP_ORGANIZATION_MEMBER)
    return tuple(names)


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

    **A missing permission is skipped with a warning rather than raised.** This
    runs from an *autouse* fixture before every test that has a database, so a
    renamed or not-yet-created permission raised from here would turn one
    targeted failure into ``Permission.DoesNotExist`` on the entire suite --
    one root cause wearing several hundred symptoms, which is precisely the
    shape this migration already lost four phases to. The tests that exist to
    notice a missing permission
    (``organizations/tests/test_group_backfill_migration.py``,
    ``organizations/tests/test_permission_backend.py``) drive
    ``0028_seed_permission_groups`` directly rather than this seeder, so they
    stay red when it matters. Creating the row instead is deliberately *not*
    what happens here: ``auth_permission`` rows belong to the migration and to
    ``post_migrate``, and this catalog carries no content type to create one
    against.
    """
    groups: list[Group] = []

    with transaction.atomic():
        for group_name, permission_labels in GROUP_PERMISSIONS.items():
            group, _ = Group.objects.get_or_create(name=group_name)
            for label in permission_labels:
                app_label, codename = label.split(".", 1)
                try:
                    permission = Permission.objects.get(
                        content_type__app_label=app_label, codename=codename
                    )
                except Permission.DoesNotExist:
                    logger.warning(
                        "Skipping %r while seeding %r: no auth.Permission with "
                        "content_type__app_label=%r and codename=%r exists.",
                        label,
                        group_name,
                        app_label,
                        codename,
                    )
                    continue
                group.permissions.add(permission)
            groups.append(group)

    return groups


def membership_state_for_groups(group_names: Iterable[str]) -> tuple[bool, bool]:
    """The ``(is_admin, is_billing_owner)`` a membership in ``group_names`` has.

    The inverse of :func:`groups_for_membership_state`, and the write half of
    ``POST /organization-members/{user_id}/groups/``: the API accepts group
    names, the two columns are still live until Phase 6 drops them, and they
    must not disagree. Round-tripping a result back through
    ``groups_for_membership_state`` is what canonicalises the stored group set
    -- so ``["organization_admin", "organization_member"]`` stores the admin
    group alone, exactly as a promotion through the old ``role`` column did.

    Any name that is not one of the two capability groups (including
    ``organization_member``, which carries no permission) contributes nothing;
    validating that the caller named a *known* group is the request
    serializer's job, not this function's.
    """
    names = set(group_names)
    return (GROUP_ORGANIZATION_ADMIN in names, GROUP_ORGANIZATION_BILLING_OWNER in names)


#: The groups an *invitation* may carry. Narrower than the full set on purpose:
#: an invitation persists its future membership's state in a single ``role``
#: column with no billing-owner half, so ``organization_billing_owner`` has
#: nowhere to live until the invitation is accepted. Refused explicitly rather
#: than silently dropped -- a caller who asked for it would otherwise believe
#: they had granted it.
INVITABLE_GROUPS: tuple[str, ...] = (GROUP_ORGANIZATION_MEMBER, GROUP_ORGANIZATION_ADMIN)


def role_for_invitation_groups(group_names: Iterable[str]) -> str:
    """The ``OrganizationRole`` value an invitation naming ``group_names`` stores.

    The public GraphQL invitation input accepts group names; the invitation row
    still records a ``role`` until Phase 6 of the vinta-django-orgs migration
    drops the column, and the membership created on acceptance is put in the
    matching groups by ``organizations.services``. This is the one translation
    between the two, kept here beside the mapping it inverts rather than inline
    in the mutation.

    :raises OrganizationGroupNotAssignableError: for a name that is not a
        seeded group, and for ``organization_billing_owner`` (see
        ``INVITABLE_GROUPS``). Both are refusals, not silent no-ops.
    """
    # Imported here rather than at module scope: ``organizations.exceptions``
    # imports from ``rest_framework``, and this module is imported by data
    # migrations' test helpers and by ``public_api`` types that have no reason
    # to pull DRF in.
    from organizations.exceptions import OrganizationGroupNotAssignableError
    from organizations.models import OrganizationRole

    names = list(group_names)
    unusable = [name for name in names if name not in INVITABLE_GROUPS]
    if unusable:
        raise OrganizationGroupNotAssignableError(
            f"Cannot assign {', '.join(sorted(unusable))} to an invitation. "
            f"Allowed groups: {', '.join(INVITABLE_GROUPS)}."
        )
    if GROUP_ORGANIZATION_ADMIN in names:
        return OrganizationRole.ADMIN
    return OrganizationRole.MEMBER


def permissions_for_groups(group_names: Iterable[str]) -> frozenset[str]:
    """Every ``"app_label.codename"`` the named seeded groups carry between them.

    Answers "would a membership in these groups hold this capability" without a
    query -- which is what the last-admin guard needs *before* it writes, to
    decide whether the assignment about to happen removes
    ``organizations.manage_members`` from its target. Unknown names contribute
    nothing.
    """
    return frozenset(
        permission for name in group_names for permission in GROUP_PERMISSIONS.get(name, ())
    )
