"""Rename ``CalendarGroup`` and its satellites to ``AppointmentType``.

Pure renames -- every table, column and relation here already exists and
keeps its rows; only the names change. Written by hand rather than by
``makemigrations`` because the autodetector cannot see a model rename and a
field rename at the same time: with ``group_fk`` renamed to
``appointment_type_fk``, ``CalendarGroupSlot`` and ``AppointmentTypeSlot``
no longer share a field set, so ``generate_renamed_models`` stops matching
them and offers a create/delete pair -- which would drop every row.

Order matters. ``RenameModel`` runs first for all six models, so the
``RenameField`` operations that follow address them under their new names
and Django's own state bookkeeping repoints every ``to=`` reference for
free. Constraint and index names are handled in ``0062``, which the
autodetector *can* generate correctly once the state here is settled.

The two quota-counting Postgres functions are deliberately NOT touched:
they name the pre-rename tables, and dropping them here would leave a
window where nothing answers. ``0063`` installs renamed copies alongside
them; the originals are dropped in a later cleanup, once every environment
runs the new ones.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("calendar_integration", "0060_calendargroupslotpool_and_more"),
    ]

    operations = [
        # Dropped BEFORE the renames below, not after. A constraint or index
        # is removed by re-deriving its SQL from the state that defined it, so
        # ``bookingpolicy_exactly_one_target`` -- whose condition names
        # ``calendar_group_fk`` -- can only be dropped while that column still
        # exists under that name. ``0062`` adds them all back, renamed.
        migrations.RemoveConstraint(
            model_name="calendargroup",
            name="calendargroup_unique_name_per_org",
        ),
        migrations.RemoveConstraint(
            model_name="calendargroupslot",
            name="calendargroupslot_unique_name_per_group",
        ),
        migrations.RemoveConstraint(
            model_name="calendargroupslotmembership",
            name="calendargroupslotmembership_uniq_inline",
        ),
        migrations.RemoveConstraint(
            model_name="calendargroupslotmembership",
            name="calendargroupslotmembership_uniq_projected",
        ),
        migrations.RemoveConstraint(
            model_name="calendargroupslotpool",
            name="calendargroupslotpool_unique_slot_pool",
        ),
        migrations.RemoveConstraint(
            model_name="calendargroupslotquotarule",
            name="calendargroupslotquotarule_unique_slot_calendar_period",
        ),
        migrations.RemoveConstraint(
            model_name="calendargroupslotquotarule",
            name="calendargroupslotquotarule_cap_positive",
        ),
        migrations.RemoveConstraint(
            model_name="bookingpolicy",
            name="bookingpolicy_exactly_one_target",
        ),
        migrations.RemoveConstraint(
            model_name="bookingpolicy",
            name="bookingpolicy_uniq_group",
        ),
        migrations.RemoveConstraint(
            model_name="calendareventgroupselection",
            name="calendareventgroupselection_unique",
        ),
        migrations.RemoveIndex(
            model_name="calendargroupslotquotarule",
            name="cgsquotarule_org_slot_idx",
        ),
        migrations.RemoveIndex(
            model_name="availabletime",
            name="availabletime_group_slot_idx",
        ),
        migrations.RemoveIndex(
            model_name="blockedtime",
            name="blockedtime_group_slot_idx",
        ),
        migrations.RemoveIndex(
            model_name="bookingpolicy",
            name="bookingpolicy_org_group_idx",
        ),
        migrations.RenameModel(
            old_name="CalendarGroup",
            new_name="AppointmentType",
        ),
        migrations.RenameModel(
            old_name="CalendarGroupSlot",
            new_name="AppointmentTypeSlot",
        ),
        migrations.RenameModel(
            old_name="CalendarGroupSlotMembership",
            new_name="AppointmentTypeSlotMembership",
        ),
        migrations.RenameModel(
            old_name="CalendarGroupSlotQuotaRule",
            new_name="AppointmentTypeSlotQuotaRule",
        ),
        migrations.RenameModel(
            old_name="CalendarGroupSlotPool",
            new_name="AppointmentTypeSlotPool",
        ),
        migrations.RenameModel(
            old_name="CalendarEventGroupSelection",
            new_name="CalendarEventAppointmentTypeSelection",
        ),
        # ``OrganizationSafeForeignKey`` is two fields per declaration: the
        # concrete ``<name>_fk`` column and the ``<name>`` ForeignObject that
        # joins it together with ``organization_id``. Both carry the old name
        # and both are renamed here.
        migrations.RenameField(
            model_name="calendarevent",
            old_name="calendar_group",
            new_name="appointment_type",
        ),
        migrations.RenameField(
            model_name="calendarevent",
            old_name="calendar_group_fk",
            new_name="appointment_type_fk",
        ),
        migrations.RenameField(
            model_name="calendarmanagementtoken",
            old_name="calendar_group",
            new_name="appointment_type",
        ),
        migrations.RenameField(
            model_name="calendarmanagementtoken",
            old_name="calendar_group_fk",
            new_name="appointment_type_fk",
        ),
        migrations.RenameField(
            model_name="bookingpolicy",
            old_name="calendar_group",
            new_name="appointment_type",
        ),
        migrations.RenameField(
            model_name="bookingpolicy",
            old_name="calendar_group_fk",
            new_name="appointment_type_fk",
        ),
        migrations.RenameField(
            model_name="appointmenttypeslot",
            old_name="group",
            new_name="appointment_type",
        ),
        migrations.RenameField(
            model_name="appointmenttypeslot",
            old_name="group_fk",
            new_name="appointment_type_fk",
        ),
        migrations.RenameField(
            model_name="appointmenttypeslotquotarule",
            old_name="group_slot",
            new_name="appointment_type_slot",
        ),
        migrations.RenameField(
            model_name="appointmenttypeslotquotarule",
            old_name="group_slot_fk",
            new_name="appointment_type_slot_fk",
        ),
        migrations.RenameField(
            model_name="availabletime",
            old_name="group_slot",
            new_name="appointment_type_slot",
        ),
        migrations.RenameField(
            model_name="availabletime",
            old_name="group_slot_fk",
            new_name="appointment_type_slot_fk",
        ),
        migrations.RenameField(
            model_name="blockedtime",
            old_name="group_slot",
            new_name="appointment_type_slot",
        ),
        migrations.RenameField(
            model_name="blockedtime",
            old_name="group_slot_fk",
            new_name="appointment_type_slot_fk",
        ),
    ]
