"""Rename the billed resource key ``calendar_groups`` to ``appointment_types``.

``payments/seams/resource_keys.py`` is the host's declaration of what
``vinta-django-billing`` meters, and it moved with the ``CalendarGroup`` ->
``AppointmentType`` rename. The key is stored as a bare string on four tables,
so without this every seeded plan limit, every subscription's copy of it, every
add-on and every usage row would go on naming a resource the registry no longer
knows -- entitlement lookups would miss, and an organization would read as
having no limit at all on something that is limited.

The four tables are ``vinta_billing``'s, not this app's, but the *key* is the
host's to define, so the rewrite belongs here, next to the declaration that
changed. ``0007_seed_billing_plans`` is the migration that wrote most of these
rows; it keeps its original vocabulary, as a frozen migration should.

Idempotent (a second pass matches nothing) and reversible in both directions.

The fields need no ``AlterField``: ``vinta_billing`` declares them with
``choices=resource_choices``, a callable the registry resolves at runtime, so
changing the registry's contents is invisible to the autodetector.
"""

from django.db import migrations


OLD_KEY = "calendar_groups"
NEW_KEY = "appointment_types"

#: Every ``vinta_billing`` model that stores a resource key as a plain string.
MODELS = (
    "PlanLimit",
    "SubscriptionPlanLimit",
    "SubscriptionAddOn",
    "BillingPeriodResourceUsage",
)


def _rekey(apps, old: str, new: str) -> None:
    for model_name in MODELS:
        model = apps.get_model("vinta_billing", model_name)
        model.objects.filter(resource_key=old).update(resource_key=new)


def forwards(apps, schema_editor):
    _rekey(apps, OLD_KEY, NEW_KEY)


def backwards(apps, schema_editor):
    _rekey(apps, NEW_KEY, OLD_KEY)


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0024_move_billing_to_vinta_billing"),
        # The four tables this rewrites.
        ("vinta_billing", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
