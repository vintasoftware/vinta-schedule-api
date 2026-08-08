# Payment Provider Selection, Phase 4: repoint every existing
# `Subscription.payment_provider` so it agrees with its organization's own
# provider resolution (Rule B: the `BillingProfile.payment_provider` pin when set,
# `settings.DEFAULT_PAYMENT_PROVIDER` otherwise).
#
# Why rows need repointing at all: until this phase,
# `SubscriptionService.create_subscription_for_organization` hardcoded
# `payment_provider="mercadopago"` on the one `Subscription` every billing-root
# organization ever gets, and `payments.0009_backfill_unlimited_subscriptions`
# stamped the same literal onto every row it created. Nothing else writes the
# column in production. So *every* pre-existing row says `mercadopago`, including
# rows belonging to organizations pinned to `stripe` -- and that column is the
# sole input to Rule A (existing-row resolution) for
# `process_subscription` / `change_subscription_plan` / `cancel_subscription` /
# `_ensure_provider_plan`, and to the write-once organization pin that
# `PaymentsViewSet._apply_subscription_payment_side_effects` drives through
# `record_payment_method`. Left alone, a Stripe-pinned organization would send a
# Stripe card token to MercadoPago and then be permanently pinned there.
#
# Safe to run: no organization has a paid subscription yet (the fact the whole
# plan's no-feature-flag decision rests on), so no row here has provider-side
# state that this could strand. It only makes the local column agree with the
# provider the organization would actually be charged through.
#
# `settings.DEFAULT_PAYMENT_PROVIDER` is read rather than hardcoded so this
# applies exactly the rule `PaymentProviderResolver.resolve_for_organization`
# applies at runtime, for organizations with no `BillingProfile` (the common case
# -- a profile is created when an org first enters billing details, a
# subscription at organization creation) or an explicitly un-pinned one. The
# setting is validated against the provider slug list at import
# (`vinta_schedule_api/settings/base.py`), so it cannot be a bad value here.
#
# Reverse restores the pre-migration state exactly: every row back to
# "mercadopago", which is what the hardcode and `0009` produced for all of them.
from django.conf import settings
from django.db import migrations


def repoint_to_organization_provider(apps, schema_editor):
    Subscription = apps.get_model("payments", "Subscription")
    BillingProfile = apps.get_model("payments", "BillingProfile")

    default_provider = settings.DEFAULT_PAYMENT_PROVIDER
    pins = dict(
        BillingProfile.objects.exclude(payment_provider="").values_list(
            "organization_id", "payment_provider"
        )
    )

    updated = []
    for subscription in Subscription.objects.all().iterator():
        provider = pins.get(subscription.organization_id, default_provider)
        if subscription.payment_provider != provider:
            subscription.payment_provider = provider
            updated.append(subscription)
    if updated:
        Subscription.objects.bulk_update(updated, ["payment_provider"], batch_size=500)


def restore_hardcoded_mercadopago(apps, schema_editor):
    Subscription = apps.get_model("payments", "Subscription")
    Subscription.objects.exclude(payment_provider="mercadopago").update(
        payment_provider="mercadopago"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0017_backfill_billingprofile_payment_provider"),
    ]

    operations = [
        migrations.RunPython(
            repoint_to_organization_provider, reverse_code=restore_hardcoded_mercadopago
        ),
    ]
