"""Drop ``role`` / ``is_billing_owner``, and re-spell the invitation's role as a group.

The last phase of the vinta-django-orgs migration
(``ai-plans/2026-08-12-VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md``,
Phase 6). ``0029`` put every membership in the groups its two flat columns
implied and every write path since has kept them there, so the columns have had
no reader outside this app's own dual-write since Phase 4. This removes them and
leaves one representation of what a membership may do.

Three operations, in dependency order:

1. **``OrganizationInvitation.role`` becomes ``group``.** The invitation records
   what the membership created on acceptance will be, and that is now a seeded
   group name. Renamed rather than dropped-and-added: the two values it holds
   (``member`` / ``admin``) carry real intent -- an admin invitation that
   silently became a member invitation is a capability quietly withdrawn from
   somebody who was promised it -- and a rename plus a two-row ``UPDATE`` is
   cheaper than a data-loss note. ``RenameField`` also keeps the column, so no
   index or constraint is rebuilt.
2. **``OrganizationMembership.role`` and ``is_billing_owner`` are dropped.**
   Neither participates in an index or a constraint (verified against the live
   schema: the only entries on this table are the primary key, the
   ``(organization, id)`` composite, ``user_id``'s FK index, ``is_active``'s
   index, and ``uniq_membership_user_organization``), so the drops take no index
   with them.
3. **``is_active``'s ``help_text`` changes.** State-only in practice --
   ``sqlmigrate`` emits nothing for it -- but the autodetector wants it, and
   leaving it out would make ``makemigrations --check`` dirty.

``uniq_membership_user_organization`` is untouched and must stay that way: the
five raw-SQL composite PROTECT FKs in ``calendar_integration`` (``0026``,
``0032``, ``0036``, ``0038``, ``0040``) bind to it.
``calendar_integration/tests/test_membership_protect_fk.py`` is the gate.

Reverse
-------
Restores both columns with their defaults and maps the invitation's group name
back. It **cannot** restore per-row values for the two membership columns --
there is nothing to restore them from once the columns are gone, and a reverse
that guessed from the groups would be inventing history. Every membership comes
back as ``role='member', is_billing_owner=False``; re-running ``0029`` forward
after a reverse would then assign every membership to ``organization_member``,
which is *wrong* for a database that had admins. Per the plan's **Pre-launch
posture** Guiding Decision a tested reverse path is not required, and this one
is offered only so the migration is steppable, not as a data-safe undo. Read the
note before stepping backwards on anything that holds real memberships.

Lock audit
----------
Two ``DROP COLUMN``s (brief ``ACCESS EXCLUSIVE``, space reclaimed at vacuum),
one ``RENAME COLUMN`` (brief ``ACCESS EXCLUSIVE``, metadata only) and one
``ALTER COLUMN TYPE varchar(20) -> varchar(50)`` on ``organizations_organizationinvitation``.
The last is the only one that is not metadata-only: PostgreSQL rewrites the table
for a ``varchar`` length change unless it can prove the change is a widening of
the same type, which it can here (``varchar(n)`` to ``varchar(m)`` with
``m > n`` is exempt from the rewrite since 9.2), so it takes the lock briefly and
returns. Invitations are a low-volume table regardless.

Rolling-deploy compatibility
----------------------------
Deliberately **not** two-phase, which is the other half of the lock question
``add-migration``'s pitfalls name. Both the column drops and the invitation
rename are breaking for any process still running the old code -- old workers
would ``SELECT role`` against a table that no longer has it -- and the standard
remedy (ship the code that stops reading the columns, deploy, then drop in a
later release) is skipped on purpose, under the plan's **Pre-launch posture**
Guiding Decision: there are no production tenants, so there is no window during
which old and new code both serve traffic. If that assumption ever stops
holding, this migration is the shape to split, not to re-run.
"""

from django.db import migrations, models


#: The two values the ``role`` column held, and the seeded group each becomes.
#: Frozen literals rather than imports from
#: ``organizations.permission_catalog``, per ``AGENTS.md`` on data migrations.
ROLE_TO_GROUP = {
    "member": "organization_member",
    "admin": "organization_admin",
}


def rename_role_values_to_group_names(apps, schema_editor):
    """``member`` -> ``organization_member``, ``admin`` -> ``organization_admin``.

    Runs after the ``RenameField``/``AlterField`` pair, so the column is already
    called ``group`` and already wide enough for the longer values. Idempotent:
    a second run matches nothing, because no group name is also a role name.
    """
    Invitation = apps.get_model("organizations", "OrganizationInvitation")
    db_alias = schema_editor.connection.alias
    for role, group in ROLE_TO_GROUP.items():
        Invitation.objects.using(db_alias).filter(group=role).update(group=group)


def rename_group_names_back_to_role_values(apps, schema_editor):
    """The exact inverse of the forward map. Lossless, unlike the column drops."""
    Invitation = apps.get_model("organizations", "OrganizationInvitation")
    db_alias = schema_editor.connection.alias
    for role, group in ROLE_TO_GROUP.items():
        Invitation.objects.using(db_alias).filter(group=group).update(group=role)


class Migration(migrations.Migration):
    dependencies = [
        ("organizations", "0029_backfill_membership_groups"),
    ]

    operations = [
        migrations.RenameField(
            model_name="organizationinvitation",
            old_name="role",
            new_name="group",
        ),
        migrations.AlterField(
            model_name="organizationinvitation",
            name="group",
            field=models.CharField(
                choices=[
                    ("organization_member", "organization_member"),
                    ("organization_admin", "organization_admin"),
                ],
                default="organization_member",
                help_text=(
                    "Seeded group the membership created on acceptance joins. "
                    "Defaults to 'organization_member', which confers no "
                    "capability; admin invitations must name "
                    "'organization_admin' explicitly."
                ),
                max_length=50,
            ),
        ),
        migrations.RunPython(
            rename_role_values_to_group_names,
            rename_group_names_back_to_role_values,
        ),
        migrations.RemoveField(
            model_name="organizationmembership",
            name="is_billing_owner",
        ),
        migrations.RemoveField(
            model_name="organizationmembership",
            name="role",
        ),
        migrations.AlterField(
            model_name="organizationmembership",
            name="is_active",
            field=models.BooleanField(
                db_default=True,
                db_index=True,
                default=True,
                help_text=(
                    "Whether this membership is active. Inactive memberships are "
                    "treated as gated: the user still has a row but loses all "
                    "tenant-scoped access until reactivated. Use this to disable "
                    "a user without deleting their membership record (which "
                    "would lose their groups and history). Default True keeps "
                    "every existing read unchanged."
                ),
            ),
        ),
    ]
