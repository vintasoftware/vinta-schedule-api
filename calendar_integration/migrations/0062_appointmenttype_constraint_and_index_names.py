# ``to_fields=(None, 'organization')`` is what ``ForeignObject.deconstruct()``
# emits, and ``None`` means "the target's primary key" -- which Django accepts and
# django-stubs' ``Sequence[str]`` annotation does not. Same suppression, same
# reason, as ``calendar_integration/migrations/0045``, ``0050`` and ``0060``; the
# error code differs only because this generator emitted tuples where 0060
# emitted lists.
# mypy: disable-error-code="arg-type"

"""Carry the constraint and index names over to the new vocabulary.

The rename in ``0061`` moves the tables and columns but leaves every
explicitly-named constraint and index spelled the old way -- Postgres does
not rename those with the table. This migration finishes the job, and picks
up the ``related_name`` / ``help_text`` wording that changed alongside.

Auto-generated: the autodetector produces this correctly once ``0061`` has
settled the state, which is exactly why the renames live there and not here.

Three index names are abbreviated rather than spelled out, because Django
caps an index name at 30 characters: ``availabletime_appt_slot_idx``,
``blockedtime_appt_slot_idx`` and ``bookingpolicy_org_appt_idx``.
"""

import calendar_integration.models
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("calendar_integration", "0061_rename_calendargroup_to_appointmenttype"),
        migrations.swappable_dependency(settings.ORGANIZATION_MEMBERSHIP_MODEL),
        migrations.swappable_dependency(settings.ORGANIZATION_MODEL),
    ]

    operations = [
        migrations.RenameIndex(
            model_name="appointmenttype",
            new_name="calendar_in_organiz_6cf30a_idx",
            old_name="calendar_in_organiz_6bc339_idx",
        ),
        migrations.RenameIndex(
            model_name="appointmenttypeslot",
            new_name="calendar_in_organiz_89ac5e_idx",
            old_name="calendar_in_organiz_189571_idx",
        ),
        migrations.RenameIndex(
            model_name="appointmenttypeslotmembership",
            new_name="calendar_in_organiz_60ad53_idx",
            old_name="calendar_in_organiz_a2192d_idx",
        ),
        migrations.RenameIndex(
            model_name="appointmenttypeslotpool",
            new_name="calendar_in_organiz_6a6563_idx",
            old_name="calendar_in_organiz_338f57_idx",
        ),
        migrations.RenameIndex(
            model_name="appointmenttypeslotquotarule",
            new_name="calendar_in_organiz_058aed_idx",
            old_name="calendar_in_organiz_5b3dc3_idx",
        ),
        migrations.RenameIndex(
            model_name="calendareventappointmenttypeselection",
            new_name="calendar_in_organiz_ea5670_idx",
            old_name="calendar_in_organiz_7750fa_idx",
        ),
        migrations.AlterField(
            model_name="appointmenttype",
            name="accepts_public_scheduling",
            field=models.BooleanField(
                default=False,
                help_text="If true, this appointment type can be booked by external users through public scheduling links without a scheduling code. If false (default), the appointment type is restricted: booking requires a token or a single-use scheduling code.",
            ),
        ),
        migrations.AlterField(
            model_name="appointmenttype",
            name="duration",
            field=models.DurationField(
                blank=True,
                help_text="When set, an event booked or rescheduled through this appointment type must span exactly this duration. Enforced by CalendarPermissionService. Duration pinning lives here rather than on CalendarManagementToken because a codeless public-appointment-type booking (accepts_public_scheduling=True) presents no code, so it inherits no per-code pin -- the appointment type being booked is the only place a length constraint can live for that path. An appointment type that accepts public scheduling MUST have this set (enforced by AppointmentTypeService.create_appointment_type / update_appointment_type, not a DB constraint -- pre-existing public appointment_types with no duration are grandfathered at rest and refused at booking time instead, fail-closed, by CalendarPermissionService). Null is otherwise unpinned, matching every restricted appointment type and every appointment type created before this field existed.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="appointmenttype",
            name="public_booking_slug",
            field=models.CharField(
                default=calendar_integration.models.generate_public_booking_slug,
                help_text="Opaque, unguessable identifier used to address this appointment type on the unauthenticated codeless booking route, instead of the integer primary key. Uniqueness is GLOBAL (not scoped to organization) because that route carries no organization in its path -- the slug alone must identify exactly one appointment type system-wide. Authorizes nothing by itself: accepts_public_scheduling still gates codeless booking, and an appointment type later flipped to public already has its identifier.",
                max_length=32,
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name="appointmenttypeslot",
            name="calendars",
            field=models.ManyToManyField(
                related_name="appointment_type_slots",
                through="calendar_integration.AppointmentTypeSlotMembership",
                through_fields=("slot", "calendar"),
                to="calendar_integration.calendar",
            ),
        ),
        migrations.AlterField(
            model_name="appointmenttypeslot",
            name="pools",
            field=models.ManyToManyField(
                related_name="appointment_type_slots",
                through="calendar_integration.AppointmentTypeSlotPool",
                through_fields=("slot", "pool"),
                to="calendar_integration.calendarpool",
            ),
        ),
        migrations.AlterField(
            model_name="appointmenttypeslotmembership",
            name="calendar",
            field=models.ForeignObject(
                editable=False,
                from_fields=("calendar_fk", "organization"),
                on_delete=django.db.models.deletion.CASCADE,
                related_name="appointment_type_slot_memberships",
                to="calendar_integration.calendar",
                to_fields=(None, "organization"),
            ),
        ),
        migrations.AlterField(
            model_name="appointmenttypeslotmembership",
            name="calendar_fk",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="appointment_type_slot_memberships_fk_rel",
                to="calendar_integration.calendar",
            ),
        ),
        migrations.AlterField(
            model_name="appointmenttypeslotmembership",
            name="source_pool_fk",
            field=models.ForeignKey(
                blank=True,
                help_text="The pool this roster row was projected from, or NULL when the calendar was added to the slot directly. Only AppointmentTypeService._reconcile_slot_pools writes non-NULL rows, and only it deletes them; the inline path never reads or writes them. CASCADE is safe here because AppointmentTypeSlotPool.pool PROTECTs the pool for as long as any slot references it, so a pool with projected rows cannot reach this cascade.",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="projected_slot_memberships_fk_rel",
                to="calendar_integration.calendarpool",
            ),
        ),
        migrations.AlterField(
            model_name="appointmenttypeslotquotarule",
            name="calendar",
            field=models.ForeignObject(
                editable=False,
                from_fields=("calendar_fk", "organization"),
                on_delete=django.db.models.deletion.CASCADE,
                related_name="appointment_type_slot_quota_rules",
                to="calendar_integration.calendar",
                to_fields=(None, "organization"),
            ),
        ),
        migrations.AlterField(
            model_name="appointmenttypeslotquotarule",
            name="calendar_fk",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="appointment_type_slot_quota_rules_fk_rel",
                to="calendar_integration.calendar",
            ),
        ),
        migrations.AlterField(
            model_name="appointmenttypeslotquotarule",
            name="cap",
            field=models.PositiveIntegerField(
                help_text="Maximum number of live bookings made through this appointment type slot a calendar may hold within one period. Must be at least 1."
            ),
        ),
        migrations.AlterField(
            model_name="availabletime",
            name="appointment_type_slot",
            field=models.ForeignObject(
                editable=False,
                from_fields=("appointment_type_slot_fk", "organization"),
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="appointment_type_scoped_available_times",
                to="calendar_integration.appointmenttypeslot",
                to_fields=(None, "organization"),
            ),
        ),
        migrations.AlterField(
            model_name="availabletime",
            name="appointment_type_slot_fk",
            field=models.ForeignKey(
                blank=True,
                help_text="If set, this available time applies only when the calendar is evaluated inside this appointment type slot, narrowing (never widening) base availability there. Null (the default) means a base row that applies everywhere the calendar is evaluated.",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="appointment_type_scoped_available_times_fk_rel",
                to="calendar_integration.appointmenttypeslot",
            ),
        ),
        migrations.AlterField(
            model_name="blockedtime",
            name="appointment_type_slot",
            field=models.ForeignObject(
                editable=False,
                from_fields=("appointment_type_slot_fk", "organization"),
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="appointment_type_scoped_blocked_times",
                to="calendar_integration.appointmenttypeslot",
                to_fields=(None, "organization"),
            ),
        ),
        migrations.AlterField(
            model_name="blockedtime",
            name="appointment_type_slot_fk",
            field=models.ForeignKey(
                blank=True,
                help_text="If set, this blocked time applies only when the calendar is evaluated inside this appointment type slot, and nowhere else. Null (the default) means a base row that blocks time everywhere the calendar is evaluated.",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="appointment_type_scoped_blocked_times_fk_rel",
                to="calendar_integration.appointmenttypeslot",
            ),
        ),
        migrations.AlterField(
            model_name="calendarevent",
            name="appointment_type_fk",
            field=models.ForeignKey(
                blank=True,
                help_text="If this event was booked through an AppointmentType, references it",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="events_fk_rel",
                to="calendar_integration.appointmenttype",
            ),
        ),
        migrations.AlterField(
            model_name="calendareventappointmenttypeselection",
            name="calendar",
            field=models.ForeignObject(
                editable=False,
                from_fields=("calendar_fk", "organization"),
                on_delete=django.db.models.deletion.PROTECT,
                related_name="appointment_type_selections",
                to="calendar_integration.calendar",
                to_fields=(None, "organization"),
            ),
        ),
        migrations.AlterField(
            model_name="calendareventappointmenttypeselection",
            name="calendar_fk",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="appointment_type_selections_fk_rel",
                to="calendar_integration.calendar",
            ),
        ),
        migrations.AlterField(
            model_name="calendareventappointmenttypeselection",
            name="event",
            field=models.ForeignObject(
                editable=False,
                from_fields=("event_fk", "organization"),
                on_delete=django.db.models.deletion.CASCADE,
                related_name="appointment_type_selections",
                to="calendar_integration.calendarevent",
                to_fields=(None, "organization"),
            ),
        ),
        migrations.AlterField(
            model_name="calendareventappointmenttypeselection",
            name="event_fk",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="appointment_type_selections_fk_rel",
                to="calendar_integration.calendarevent",
            ),
        ),
        migrations.AlterField(
            model_name="calendarmanagementtoken",
            name="appointment_type_fk",
            field=models.ForeignKey(
                blank=True,
                help_text="If set, this token is scoped to an appointment type (for appointment type booking codes). Mutually exclusive with the ``calendar`` scope for booking codes.",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="management_tokens_fk_rel",
                to="calendar_integration.appointmenttype",
            ),
        ),
        migrations.AddIndex(
            model_name="appointmenttypeslotquotarule",
            index=models.Index(
                fields=["organization", "appointment_type_slot_fk"],
                name="cgsquotarule_org_slot_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="availabletime",
            index=models.Index(
                condition=models.Q(("appointment_type_slot_fk__isnull", False)),
                fields=["organization", "appointment_type_slot_fk"],
                name="availabletime_appt_slot_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="blockedtime",
            index=models.Index(
                condition=models.Q(("appointment_type_slot_fk__isnull", False)),
                fields=["organization", "appointment_type_slot_fk"],
                name="blockedtime_appt_slot_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="bookingpolicy",
            index=models.Index(
                fields=["organization", "appointment_type_fk"], name="bookingpolicy_org_appt_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="appointmenttype",
            constraint=models.UniqueConstraint(
                fields=("organization", "name"), name="appointmenttype_unique_name_per_org"
            ),
        ),
        migrations.AddConstraint(
            model_name="appointmenttypeslot",
            constraint=models.UniqueConstraint(
                fields=("appointment_type_fk", "name"),
                name="appointmenttypeslot_unique_name_per_appointment_type",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointmenttypeslotmembership",
            constraint=models.UniqueConstraint(
                condition=models.Q(("source_pool_fk__isnull", True)),
                fields=("slot_fk", "calendar_fk"),
                name="appointmenttypeslotmembership_uniq_inline",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointmenttypeslotmembership",
            constraint=models.UniqueConstraint(
                condition=models.Q(("source_pool_fk__isnull", False)),
                fields=("slot_fk", "calendar_fk", "source_pool_fk"),
                name="appointmenttypeslotmembership_uniq_projected",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointmenttypeslotpool",
            constraint=models.UniqueConstraint(
                fields=("slot_fk", "pool_fk"), name="appointmenttypeslotpool_unique_slot_pool"
            ),
        ),
        migrations.AddConstraint(
            model_name="appointmenttypeslotquotarule",
            constraint=models.UniqueConstraint(
                fields=("appointment_type_slot_fk", "calendar_fk", "period"),
                name="appointmenttypeslotquotarule_unique_slot_calendar_period",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointmenttypeslotquotarule",
            constraint=models.CheckConstraint(
                condition=models.Q(("cap__gt", 0)), name="appointmenttypeslotquotarule_cap_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="bookingpolicy",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("appointment_type_fk__isnull", True),
                        ("calendar_fk__isnull", False),
                        ("is_organization_default", False),
                        ("membership_user_id__isnull", True),
                    ),
                    models.Q(
                        ("appointment_type_fk__isnull", True),
                        ("calendar_fk__isnull", True),
                        ("is_organization_default", False),
                        ("membership_user_id__isnull", False),
                    ),
                    models.Q(
                        ("appointment_type_fk__isnull", False),
                        ("calendar_fk__isnull", True),
                        ("is_organization_default", False),
                        ("membership_user_id__isnull", True),
                    ),
                    models.Q(
                        ("appointment_type_fk__isnull", True),
                        ("calendar_fk__isnull", True),
                        ("is_organization_default", True),
                        ("membership_user_id__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="bookingpolicy_exactly_one_target",
            ),
        ),
        migrations.AddConstraint(
            model_name="bookingpolicy",
            constraint=models.UniqueConstraint(
                condition=models.Q(("appointment_type_fk__isnull", False)),
                fields=("organization", "appointment_type_fk"),
                name="bookingpolicy_uniq_appointment_type",
            ),
        ),
        migrations.AddConstraint(
            model_name="calendareventappointmenttypeselection",
            constraint=models.UniqueConstraint(
                fields=("event_fk", "slot_fk", "calendar_fk"),
                name="calendareventappointmenttypeselection_unique",
            ),
        ),
    ]
