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
