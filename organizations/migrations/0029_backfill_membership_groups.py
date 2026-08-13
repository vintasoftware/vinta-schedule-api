"""Give every existing membership the groups its ``role`` / ``is_billing_owner`` imply.

The mapping, from the plan's Phase 3:

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
dropping it here would lose information that Phase 6, not this migration, is
the one entitled to discard.

``role`` and ``is_billing_owner`` are **read, not written**. Both
representations are live and must agree until Phase 6 retires one; the
temporary dual-write in ``organizations.services`` is what keeps rows written
after this migration in step.

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


def _group_ids(apps, db_alias):
    Group = apps.get_model("auth", "Group")
    group_ids = dict(
        Group.objects.using(db_alias).filter(name__in=SEEDED_GROUPS).values_list("name", "id")
    )
    missing = [name for name in SEEDED_GROUPS if name not in group_ids]
    if missing:
        raise SeededGroupsMissingError(
            f"0028_seed_permission_groups did not leave these groups behind: {missing}"
        )
    return group_ids


def backfill_membership_groups(apps, schema_editor):
    Membership = apps.get_model("organizations", "OrganizationMembership")
    db_alias = schema_editor.connection.alias
    group_ids = _group_ids(apps, db_alias)
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
    """
    Membership = apps.get_model("organizations", "OrganizationMembership")
    db_alias = schema_editor.connection.alias
    group_ids = _group_ids(apps, db_alias)
    through = Membership.groups.through

    through.objects.using(db_alias).filter(group_id__in=list(group_ids.values())).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("organizations", "0028_seed_permission_groups"),
    ]

    operations = [
        migrations.RunPython(backfill_membership_groups, unassign_membership_groups),
    ]
