"""Give every existing membership the groups its ``role`` / ``is_billing_owner`` imply.

The mapping:

======================  ====================  ==========================================
``role``                ``is_billing_owner``  groups
======================  ====================  ==========================================
``admin``               ``False``             ``organization_admin``
``admin``               ``True``              ``organization_admin`` +
                                              ``organization_billing_owner``
``member``              ``True``              ``organization_billing_owner``
``member``              ``False``             ``organization_member``
======================  ====================  ==========================================

An admin already holds ``payments.manage_billing`` through
``organization_admin``, so the second row's extra group grants nothing new --
it is written anyway because ``is_billing_owner`` is an independent column and
dropping it here would lose information that only
``0030_drop_role_and_is_billing_owner``, not this migration, is entitled to
discard.

``role`` and ``is_billing_owner`` are **read, not written**. Both
representations are live and must agree until ``0030`` drops the two columns
and retires one of them; the temporary dual-write in ``organizations.services``
is what keeps rows written after this migration in step.

Idempotent, batched and resumable: rows are inserted straight into the M2M
through table in fixed-size batches with ``ignore_conflicts=True``, so a
re-run, a resumed run, or a run against a partially-assigned table all converge
on the same state without a per-row read. Additive only -- it never removes a
group a membership already holds.

The strings are frozen literals rather than imports from
``organizations.permission_catalog`` for the reason given in ``0028``'s header.
"""

from django.db import migrations


ADMIN_ROLE = "admin"

GROUP_ORGANIZATION_ADMIN = "organization_admin"
GROUP_ORGANIZATION_BILLING_OWNER = "organization_billing_owner"
GROUP_ORGANIZATION_MEMBER = "organization_member"

SEEDED_GROUPS = [
    GROUP_ORGANIZATION_ADMIN,
    GROUP_ORGANIZATION_BILLING_OWNER,
    GROUP_ORGANIZATION_MEMBER,
]

BATCH_SIZE = 1000


class SeededGroupsMissingError(RuntimeError):
    """Raised when ``0028``'s groups are not in the database this is running against."""


def target_group_names(role, is_billing_owner):
    """The group names a membership in this state must hold. See the table above."""
    names = []
    if role == ADMIN_ROLE:
        names.append(GROUP_ORGANIZATION_ADMIN)
    if is_billing_owner:
        names.append(GROUP_ORGANIZATION_BILLING_OWNER)
    if not names:
        names.append(GROUP_ORGANIZATION_MEMBER)
    return names


def _group_ids(apps, db_alias, *, required: bool):
    """Resolve the three seeded groups to their ids.

    ``required`` is the whole point of the parameter, and the two callers want
    opposite things:

    *Forward* cannot proceed without them -- it is about to assign memberships
    to groups, and a missing group means ``0028`` did not do its job. Raising is
    the only honest answer; silently assigning nothing would leave every
    membership ungrouped and ``billing_recipients`` returning no one.

    *Reverse* must tolerate their absence. It detaches groups from memberships,
    so "the groups are not there" means the work it exists to undo is already
    undone -- a completed no-op, not a failure. Raising there turns a benign
    state into a hard error, and it does so on a path Django runs whenever
    *anything* downstream is unapplied: a test that steps another app's
    migrations backwards drags this reverse along with it (``payments.0022`` is
    a real dependency of ``0028``), and it should not have to care whether the
    groups happen to exist at that moment.
    """
    Group = apps.get_model("auth", "Group")
    group_ids = dict(
        Group.objects.using(db_alias).filter(name__in=SEEDED_GROUPS).values_list("name", "id")
    )
    missing = [name for name in SEEDED_GROUPS if name not in group_ids]
    if missing and required:
        raise SeededGroupsMissingError(
            f"0028_seed_permission_groups did not leave these groups behind: {missing}"
        )
    return group_ids


def backfill_membership_groups(apps, schema_editor):
    Membership = apps.get_model("organizations", "OrganizationMembership")
    db_alias = schema_editor.connection.alias
    group_ids = _group_ids(apps, db_alias, required=True)
    through = Membership.groups.through

    pending = []
    memberships = (
        Membership.objects.using(db_alias)
        .order_by("pk")
        .values_list("pk", "role", "is_billing_owner")
    )
    for membership_id, role, is_billing_owner in memberships.iterator(chunk_size=BATCH_SIZE):
        pending.extend(
            through(organizationmembership_id=membership_id, group_id=group_ids[name])
            for name in target_group_names(role, is_billing_owner)
        )
        if len(pending) >= BATCH_SIZE:
            through.objects.using(db_alias).bulk_create(pending, ignore_conflicts=True)
            pending = []

    if pending:
        through.objects.using(db_alias).bulk_create(pending, ignore_conflicts=True)


def unassign_membership_groups(apps, schema_editor):
    """Detach the three seeded groups from every membership.

    A plain mirror of the forward operation against current state, the way a
    Django migration reverse normally works -- not "only what the forward
    touched", which nothing records.

    Tolerates the groups already being gone. ``0028``'s own reverse deletes
    them, so any backwards plan that reaches past this migration will have
    removed them by the time a *later* backwards plan runs -- and Django runs
    this reverse whenever anything downstream of ``0028`` is unapplied, which
    includes stepping ``payments`` backwards, since ``payments.0022`` is one of
    ``0028``'s dependencies. With nothing to detach there is nothing to do.
    """
    Membership = apps.get_model("organizations", "OrganizationMembership")
    db_alias = schema_editor.connection.alias
    group_ids = _group_ids(apps, db_alias, required=False)
    if not group_ids:
        return
    through = Membership.groups.through

    through.objects.using(db_alias).filter(group_id__in=list(group_ids.values())).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("organizations", "0028_seed_permission_groups"),
    ]

    operations = [
        migrations.RunPython(backfill_membership_groups, unassign_membership_groups),
    ]
