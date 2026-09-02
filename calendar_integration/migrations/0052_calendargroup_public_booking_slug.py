"""Add ``CalendarGroup.public_booking_slug`` -- nullable, no default (step 1/3).

Phase 3b of the REST_CODE_GATED_SCHEDULING plan. Phase 3 shipped the codeless
public-group-booking route keyed by ``CalendarGroup``'s integer primary key,
which -- combined with the plan's decision to keep ``organization_id`` out of
every path on that route, and with throttling declined -- let an anonymous
caller walk ``group_id`` 1..N and learn, from the 404/403/201 split, which
groups exist in ANY organization and which accept public scheduling. This
migration is the first of a three-migration chain (0052 -> 0053 -> 0054) that
adds an opaque, unguessable, globally-unique slug replacing the integer id as
that route's identifier. Split into three separate migrations (rather than
three operations in one file) specifically so each phase applies and reverses
independently -- the reverse path of 0054 alone, and the idempotency of
0053's backfill in isolation, both need to be exercisable without unwinding
the other two.

This migration: ``AddField`` -- ``public_booking_slug`` added **nullable,
with no default declared on the field at all** (not even the Python
``default=generate_public_booking_slug`` the final model carries -- that
appears only in 0054's state, see that migration's docstring). A nullable
column with no default of any kind is a metadata-only ``ALTER TABLE`` in
Postgres: no table rewrite, no scan, negligible lock duration regardless of
table size, and -- the reason the default is withheld here specifically --
no ``DEFAULT`` clause on the ``ADD COLUMN`` statement. Postgres's
``ADD COLUMN ... DEFAULT <value>`` sets that **single, identical** value for
every pre-existing row (a fast, metadata-only operation since Postgres 11,
but still logically one shared value), which is exactly what Django emits
for *any* field with ``has_default()`` true -- nullability does not change
that behavior, it only changes whether a *missing* value is allowed.
Declaring the Python default this early was tried and rejected during
authoring: ``sqlmigrate`` showed Django computing the callable once and
emitting ``ADD COLUMN ... DEFAULT '<one-generated-slug>'``, which would have
handed every pre-existing ``CalendarGroup`` row the *same* slug -- the
opposite of what a unique, unguessable-per-group identifier requires, and
exactly the failure mode this three-migration chain exists to avoid.
Withholding the default from the field's state entirely at this step leaves
every pre-existing row genuinely ``NULL``, to be filled distinctly by 0053.

Reverse: ``RemoveField`` (Django's auto-generated reverse for ``AddField``)
drops the column. Nothing else exists yet at this point in the chain to
orphan.
"""

from django.db import migrations, models


HELP_TEXT = (
    "Opaque, unguessable identifier used to address this group on the "
    "unauthenticated codeless booking route, instead of the integer primary "
    "key. Uniqueness is GLOBAL (not scoped to organization) because that "
    "route carries no organization in its path -- the slug alone must "
    "identify exactly one group system-wide. Authorizes nothing by itself: "
    "accepts_public_scheduling still gates codeless booking, and a group "
    "later flipped to public already has its identifier."
)


class Migration(migrations.Migration):
    """Add CalendarGroup.public_booking_slug, nullable, no default (1/3)."""

    dependencies = [
        ("calendar_integration", "0050_calendarmanagementtoken_minted_by_membership_and_duration"),
    ]

    operations = [
        migrations.AddField(
            model_name="calendargroup",
            name="public_booking_slug",
            field=models.CharField(
                max_length=32,
                null=True,
                blank=True,
                help_text=HELP_TEXT,
            ),
        ),
    ]
