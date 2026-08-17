"""Route ``CalendarEvent.external_attendees`` through the organization-scoped table.

The field used to have an **auto-created** through table,
``calendar_integration_calendarevent_external_attendees``, holding
``calendarevent_id`` and ``externalattendee_id`` and no ``organization`` column.
It was the last many-to-many on a scoped model whose join carried no
organization -- and the related manager a many-to-many builds is not
organization-scoped either, so nothing put it back. The field is exposed
publicly (``calendar_integration/graphql.py``).

**Dropping the table loses nothing.** Nothing has ever written it: every write
path goes through ``EventExternalAttendance``, the scoped through model, which
is also what ``CalendarEvent.external_attendances`` reads. Verified empirically
before writing this migration (a persisted ``EventExternalAttendance`` leaves the
auto-created table at zero rows), and the consequence is that the public
``externalAttendees`` field answered ``[]`` for every event regardless of its
attendees. Pointing it at ``EventExternalAttendance`` gives both hops of the join
an organization-matched ``ON`` clause -- ``event`` and ``external_attendee`` are
both ``OrganizationSafeForeignKey`` -- and, as a side effect, makes the field
return the attendees it always claimed to.

**Hand-written as ``RemoveField`` + ``AddField``**, not the ``AlterField`` the
autodetector proposes. Django refuses to apply that one outright
(``Cannot alter field ... you cannot alter to or from M2M fields, or add or
remove through= on M2M fields``). The pair leaves the identical end state, so
``makemigrations --check`` stays quiet.

**Lock audit.** ``DROP TABLE`` on an empty, unreferenced table; the ``AddField``
emits nothing at all, since ``EventExternalAttendance``'s table already exists.
The reverse re-creates the auto-created table empty, which is exactly the state
it is in today.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("calendar_integration", "0046_alter_calendar_memberships"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="calendarevent",
            name="external_attendees",
        ),
        migrations.AddField(
            model_name="calendarevent",
            name="external_attendees",
            field=models.ManyToManyField(
                blank=True,
                related_name="calendar_events",
                through="calendar_integration.EventExternalAttendance",
                through_fields=("event", "external_attendee"),
                to="calendar_integration.externalattendee",
            ),
        ),
    ]
