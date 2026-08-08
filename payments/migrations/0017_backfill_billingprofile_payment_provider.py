# Payment Provider Selection, Phase 2: every existing `BillingProfile` is
# explicitly pinned to `stripe` (the system default -- see
# `settings.DEFAULT_PAYMENT_PROVIDER`) so that a future change to that setting
# can never silently move an already-provisioned organization onto a different
# provider. No organization has a paid subscription yet (per the plan's
# Guiding Decisions), so this is a no-op in effect -- it only makes the pin
# explicit.
#
# The literal "stripe" is hardcoded rather than importing
# `payments.constants.PaymentProviders.STRIPE`, mirroring the precedent set by
# `payments.0009_backfill_unlimited_subscriptions` (which hardcodes
# "mercadopago" for the same reason): a migration should stay correct against
# the historical `apps.get_model(...)` model even if the live constants module
# is renamed or restructured later.
#
# Forward and reverse are both plain, idempotent bulk updates: forward moves
# every `""` row to "stripe", reverse moves every "stripe" row back to `""`.
# Re-running either matches nothing the second time.
from django.db import migrations


def backfill_payment_provider(apps, schema_editor):
    BillingProfile = apps.get_model("payments", "BillingProfile")
    BillingProfile.objects.filter(payment_provider="").update(payment_provider="stripe")


def unset_payment_provider(apps, schema_editor):
    BillingProfile = apps.get_model("payments", "BillingProfile")
    BillingProfile.objects.filter(payment_provider="stripe").update(payment_provider="")


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0016_billingprofile_payment_provider"),
    ]

    operations = [
        migrations.RunPython(backfill_payment_provider, reverse_code=unset_payment_provider),
    ]
