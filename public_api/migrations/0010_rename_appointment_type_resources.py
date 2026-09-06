"""Repoint stored ``ResourceAccess.resource_name`` grants at the renamed resources.

``PublicAPIResources`` is the source of these values and it moved with the
``CalendarGroup`` -> ``AppointmentType`` rename. The column stores the *value*,
not a reference, so every row a partner token already holds would otherwise go
on naming a resource that no longer exists -- and
``OrganizationResourceAccess`` would quietly refuse every request for it. A
silent loss of access, not an error anyone would see in a deploy log.

Only the seven values the rename touched are rewritten, and the reverse maps
them straight back, so the migration is safe to run in either direction and
idempotent on re-run (a second pass matches nothing).

The field itself needs no ``AlterField``: it is declared ``choices=PublicAPIResources``,
which Django records by reference rather than by expanding the members, so the
autodetector sees no schema change here.
"""

from django.db import migrations


#: old value -> new value. Anything not listed is untouched.
RENAMED_RESOURCES = {
    "calendar_group": "appointment_type",
    "group_scoped_availability_windows": "appointment_type_scoped_availability_windows",
    "batch_upsert_group_scoped_availability_windows": (
        "batch_upsert_appointment_type_scoped_availability_windows"
    ),
    "group_scoped_blocked_times": "appointment_type_scoped_blocked_times",
    "batch_upsert_group_scoped_blocked_times": "batch_upsert_appointment_type_scoped_blocked_times",
    "group_scoped_quota_rules": "appointment_type_scoped_quota_rules",
    "batch_upsert_group_scoped_quota_rules": "batch_upsert_appointment_type_scoped_quota_rules",
}


def _remap(apps, mapping: dict[str, str]) -> None:
    resource_access = apps.get_model("public_api", "ResourceAccess")
    for old, new in mapping.items():
        # ``ResourceAccess`` is not organization-scoped -- a grant belongs to a
        # system user, and this rewrite has to reach every row in the table
        # regardless of tenant, so the historical model's plain manager is the
        # right one here.
        resource_access.objects.filter(resource_name=old).update(resource_name=new)


def forwards(apps, schema_editor):
    _remap(apps, RENAMED_RESOURCES)


def backwards(apps, schema_editor):
    _remap(apps, {new: old for old, new in RENAMED_RESOURCES.items()})


class Migration(migrations.Migration):
    dependencies = [
        ("public_api", "0009_alter_systemuser_options_alter_systemuser_managers_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
