"""Let ``CalendarEvent.external_id`` hold NULL for provider-less events.

``external_id`` is ``unique``. An event created without a calendar provider has
no id to store, and every such row used to be saved with the empty string --
which the unique index treats as an ordinary value, so the second one raised an
``IntegrityError``. Postgres exempts NULL from a unique index, so switching the
column to nullable lets any number of provider-less events coexist.

The backfill touches at most one row: the unique index means only a single row
could ever have held the empty string.

Reverse restores the NOT NULL column, so it has to give every NULL row a
distinct value first -- ``internal-<uuid4>`` -- since the unique index would
reject a shared placeholder.
"""

import uuid

from django.db import migrations, models


def blank_external_id_to_null(apps, schema_editor):
    CalendarEvent = apps.get_model("calendar_integration", "CalendarEvent")
    CalendarEvent.objects.filter(external_id="").update(external_id=None)


def null_external_id_to_unique_placeholder(apps, schema_editor):
    CalendarEvent = apps.get_model("calendar_integration", "CalendarEvent")
    for pk in CalendarEvent.objects.filter(external_id__isnull=True).values_list("pk", flat=True):
        CalendarEvent.objects.filter(pk=pk).update(external_id=f"internal-{uuid.uuid4()}")


class Migration(migrations.Migration):
    dependencies = [
        ("calendar_integration", "0063_appointment_type_quota_period_counting_functions"),
    ]

    operations = [
        migrations.AlterField(
            model_name="calendarevent",
            name="external_id",
            field=models.CharField(blank=True, max_length=255, null=True, unique=True),
        ),
        migrations.RunPython(
            blank_external_id_to_null,
            null_external_id_to_unique_placeholder,
        ),
    ]
