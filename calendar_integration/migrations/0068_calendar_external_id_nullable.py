"""Let ``Calendar.external_id`` hold NULL for internal calendars.

``Calendar`` is unique on ``(external_id, provider, organization_id)``. An internal
calendar (virtual, resource, bundle) has no external id, and every such row used to be
saved with the empty string -- which the unique index treats as an ordinary value, so
the second internal calendar in an organization raised an ``IntegrityError``. Postgres
exempts NULL from a unique index, so switching the column to nullable lets an
organization hold any number of internal calendars.

The backfill touches at most one row per organization: the unique index means only one
internal calendar per organization could ever have held the empty string.

Reverse turns NULL back into the empty string and restores the NOT NULL column. That
only fits while each organization holds at most one internal calendar, which is all 0067
allowed; with more, the unique index rejects the rollback.
"""

from django.db import migrations, models


def blank_external_id_to_null(apps, schema_editor):
    Calendar = apps.get_model("calendar_integration", "Calendar")
    Calendar.objects.filter(external_id="").update(external_id=None)


def null_external_id_to_blank(apps, schema_editor):
    Calendar = apps.get_model("calendar_integration", "Calendar")
    Calendar.objects.filter(external_id__isnull=True).update(external_id="")


class Migration(migrations.Migration):
    dependencies = [
        ("calendar_integration", "0067_webhook_subscription_opening_account"),
    ]

    operations = [
        migrations.AlterField(
            model_name="calendar",
            name="external_id",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.RunPython(
            blank_external_id_to_null,
            null_external_id_to_blank,
        ),
    ]
