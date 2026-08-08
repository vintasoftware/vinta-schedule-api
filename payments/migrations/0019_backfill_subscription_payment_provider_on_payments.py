# Payment Provider Selection, Phase 4: stamp a provider onto the
# subscription-charge `Payment` rows that were written without one.
#
# `PaymentService.receive_subscription_payment_update` -- the only writer of
# `Payment.subscription` anywhere in the codebase -- created its rows without
# passing `payment_provider`, so every recurring subscription charge landed a row
# stamped `""`. That was invisible before this phase (everything resolved through
# the single hardcoded MercadoPago gateway), but from Phase 4 on those rows are
# unroutable: `check_payment_status` / `create_refund` resolve their adapter from
# this column and `""` raises `UnknownPaymentProviderError`.
#
# The owning `Subscription`'s own `payment_provider` is the correct value: the
# charge was made by whichever provider drives that subscription, and
# `payments.0018_repoint_subscription_payment_provider` has just brought that
# column into agreement with the organization's resolution. Rows whose
# subscription somehow also carries an empty provider are left alone rather than
# guessed at -- there is nothing to derive a provider from, and inventing one
# would be worse than the loud `UnknownPaymentProviderError` they already get.
#
# Reverse restores the pre-migration state: `""` on every subscription-linked
# `Payment`, which is exactly what that code path produced for all of them.
from django.db import migrations


def backfill_from_subscription(apps, schema_editor):
    Payment = apps.get_model("payments", "Payment")
    Subscription = apps.get_model("payments", "Subscription")

    providers = dict(
        Subscription.objects.exclude(payment_provider="").values_list("id", "payment_provider")
    )
    if not providers:
        return

    updated = []
    for payment in (
        Payment.objects.filter(payment_provider="", subscription_id__in=providers).iterator()
    ):
        payment.payment_provider = providers[payment.subscription_id]
        updated.append(payment)
    if updated:
        Payment.objects.bulk_update(updated, ["payment_provider"], batch_size=500)


def unset_on_subscription_payments(apps, schema_editor):
    Payment = apps.get_model("payments", "Payment")
    Payment.objects.filter(subscription_id__isnull=False).exclude(payment_provider="").update(
        payment_provider=""
    )


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0018_repoint_subscription_payment_provider"),
    ]

    operations = [
        migrations.RunPython(backfill_from_subscription, reverse_code=unset_on_subscription_payments),
    ]
