# Stamp a provider onto the subscription-charge `Payment` rows that were written
# without one.
#
# `PaymentService.receive_subscription_payment_update` -- the only writer of
# `Payment.subscription` anywhere in the codebase -- created its rows without
# passing `payment_provider`, so every recurring subscription charge landed a row
# stamped `""`. That was invisible while everything resolved through the single
# hardcoded MercadoPago gateway, but now that `check_payment_status` /
# `create_refund` resolve their adapter from this column, those rows are
# unroutable: `""` raises `UnknownPaymentProviderError`.
#
# The owning `Subscription`'s own `payment_provider` is the correct value: the
# charge was made by whichever provider drives that subscription, and
# `payments.0018_repoint_subscription_payment_provider` has just brought that
# column into agreement with the organization's resolution. Rows whose
# subscription somehow also carries an empty provider are left alone rather than
# guessed at -- there is nothing to derive a provider from, and inventing one
# would be worse than the loud `UnknownPaymentProviderError` they already get.
#
# Reverse safety: a blanket "blank every subscription-linked
# Payment.payment_provider" reverse -- what this migration originally did --
# would also blank rows this migration never touched (any `Payment` a caller
# stamped with a real provider through the ordinary provider-resolution code path
# after this migration ran). Combined with `0018`'s (now-fixed) reverse,
# reversing both would have rewritten every Stripe-provider subscription charge
# to `mercadopago` -- producing the wrong-provider refund this migration exists
# to prevent, via the migration path instead of the code path. The forward pass
# now stamps `meta[BACKFILL_META_KEY]` on each row it actually fills (following
# the same precedent as `payments.0009_backfill_unlimited_subscriptions` /
# `payments.0018`), and the reverse blanks only rows carrying that stamp.
from django.db import migrations


#: Meta key the forward pass stamps on each `Payment` row it fills, so the
#: reverse can blank only rows this migration actually touched -- see the module
#: docstring above.
BACKFILL_META_KEY = "backfilled_by_0019"


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
        payment.meta[BACKFILL_META_KEY] = True
        updated.append(payment)
    if updated:
        Payment.objects.bulk_update(updated, ["payment_provider", "meta"], batch_size=500)


def unset_on_subscription_payments(apps, schema_editor):
    """Reverse: blank only the rows the forward pass filled -- not every
    subscription-linked `Payment`. See the module docstring above for why."""
    Payment = apps.get_model("payments", "Payment")

    updated = []
    for payment in Payment.objects.filter(**{"meta__has_key": BACKFILL_META_KEY}).iterator():
        payment.payment_provider = ""
        payment.meta.pop(BACKFILL_META_KEY, None)
        updated.append(payment)
    if updated:
        Payment.objects.bulk_update(updated, ["payment_provider", "meta"], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0018_repoint_subscription_payment_provider"),
    ]

    operations = [
        migrations.RunPython(backfill_from_subscription, reverse_code=unset_on_subscription_payments),
    ]
