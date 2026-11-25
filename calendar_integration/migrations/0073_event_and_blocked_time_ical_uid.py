"""Store the provider's iCalendar UID on events and blocked times.

``db_default`` keeps the column's default in the database, so containers still running
the previous release can insert rows (they leave the column out) between this migration
and the service rollout.

The backfill copies the UID out of the provider payload a row already stores in
``meta["latest_original_payload"]``: Google calls it ``iCalUID``, Outlook ``iCalUId``.
Every row the calendar sync wrote has that payload. An event created at a provider has
it only when the calendar service that created it was authenticated with the provider;
an event booked through the public API or a booking code before this migration has no
payload and keeps an empty UID. Rescheduling such an event on a crew whose calendars
share its provider refuses a move that overlaps its old slot, since the other calendars'
synced copies cannot be told apart from other bookings.

The backfill is idempotent (it only fills empty UIDs). Its reverse does nothing: the
reverse of the ``AddField`` steps drops the column with everything in it.
"""

from django.db import migrations, models


_BACKFILL_SQL = """
UPDATE {table}
SET ical_uid = COALESCE(
    meta -> 'latest_original_payload' ->> 'iCalUID',
    meta -> 'latest_original_payload' ->> 'iCalUId',
    ''
)
WHERE ical_uid = ''
  AND (
    meta -> 'latest_original_payload' ? 'iCalUID'
    OR meta -> 'latest_original_payload' ? 'iCalUId'
  );
"""


class Migration(migrations.Migration):
    dependencies = [
        ("calendar_integration", "0072_managed_calendar_is_free_in_range_ignored_rows"),
    ]

    operations = [
        migrations.AddField(
            model_name="blockedtime",
            name="ical_uid",
            field=models.CharField(blank=True, db_default="", default="", max_length=1024),
        ),
        migrations.AddField(
            model_name="calendarevent",
            name="ical_uid",
            field=models.CharField(blank=True, db_default="", default="", max_length=1024),
        ),
        migrations.RunSQL(
            sql=_BACKFILL_SQL.format(table="calendar_integration_blockedtime"),
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql=_BACKFILL_SQL.format(table="calendar_integration_calendarevent"),
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
