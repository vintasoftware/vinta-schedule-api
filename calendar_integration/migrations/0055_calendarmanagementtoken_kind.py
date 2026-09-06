"""Add ``CalendarManagementToken.kind`` -- nullable, no default (step 1/3).

Phase 7 of the REST_CODE_GATED_SCHEDULING plan. Phase 6 classified a
``CalendarManagementToken`` row as a booking code (and therefore revokable
via ``CalendarPermissionService.revoke_token`` / ``DELETE
/booking-codes/<id>/``) using a heuristic:
``minted_by_membership_user_id IS NOT NULL OR minted_by_system_user_id IS
NOT NULL``. That heuristic was recorded as a known fragility at Phase 6
close-out and Phase 8 breaks it outright: a codeless group booking mints a
``RESCHEDULE``/``CANCEL`` code with no authenticated user and no system
user, so under the heuristic it is silently NOT a booking code -- a
patient-facing link that can never be revoked, even after a leak.

This is the first of a three-migration chain (0055 -> 0056 -> 0057), the
same shape Phase 3b used for ``CalendarGroup.public_booking_slug`` (see that
chain's ``0052``/``0053``/``0054`` for the fuller version of this reasoning):

This migration: ``AddField`` -- ``kind`` added **nullable, with no default
declared on the field at all** (not the ``default=MANAGEMENT_TOKEN`` /
``db_default=MANAGEMENT_TOKEN`` the final model carries -- those appear only
in 0057's state). Declaring either default this early was rejected during
authoring for the same reason Phase 3b's 0052 rejected an early default:
Postgres's ``ADD COLUMN ... DEFAULT <value>`` (or the ``db_default``
equivalent) sets that **single, identical** value for every pre-existing
row. If ``kind`` started out defaulting to ``MANAGEMENT_TOKEN`` at column-add
time, every pre-existing booking code -- a row that genuinely needs
``BOOKING_CODE`` -- would already be non-NULL by the time 0056's heuristic
backfill runs, and 0056's ``WHERE kind IS NULL`` guard would skip it,
misclassifying it exactly the way this phase exists to stop happening.
Withholding the default from the field's state entirely at this step leaves
every pre-existing row genuinely ``NULL``, to be filled distinctly by 0056
according to the old heuristic.

A nullable column with no default of any kind is a metadata-only ``ALTER
TABLE`` in Postgres: no table rewrite, no scan, negligible lock duration
regardless of table size.

Reverse
-------
``RemoveField`` (Django's auto-generated reverse for ``AddField``) drops the
column. Nothing else exists yet at this point in the chain to orphan.
"""

from django.db import migrations, models


HELP_TEXT = (
    "Explicit discriminator: BOOKING_CODE tokens are single-use booking "
    "codes, selected by CalendarManagementTokenQuerySet.booking_codes and "
    "therefore revokable via CalendarPermissionService.revoke_token / "
    "DELETE /booking-codes/<id>/. Everything else (owner, attendee, "
    "external-attendee tokens) is MANAGEMENT_TOKEN and never revokable "
    "through those surfaces. Defaults to MANAGEMENT_TOKEN deliberately: a "
    "mint path that forgets to set this produces an un-revokable token "
    "rather than a wrongly-revokable one -- it fails closed. Set "
    "explicitly by every create_*_token method on "
    "CalendarPermissionService; the default exists only as a safety net, "
    "never leaned on by a known call site."
)


class Migration(migrations.Migration):
    """Add CalendarManagementToken.kind, nullable, no default (1/3)."""

    dependencies = [
        ("calendar_integration", "0054_calendargroup_public_booking_slug_unique"),
    ]

    operations = [
        migrations.AddField(
            model_name="calendarmanagementtoken",
            name="kind",
            field=models.CharField(
                max_length=20,
                choices=[
                    ("booking_code", "Booking Code"),
                    ("management_token", "Management Token"),
                ],
                null=True,
                help_text=HELP_TEXT,
            ),
        ),
    ]
