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
# Reverse safety (second-pass Tier 4 fix): a blanket "every row back to
# mercadopago" reverse -- what this migration originally did -- would also
# rewrite `Subscription` rows created *after* this migration under
# `DEFAULT_PAYMENT_PROVIDER=stripe`, destroying the exact evidence the plan's
# rollback runbook (Risk & Rollout Notes) says to check before reversing:
# "verify ... that no Stripe-provider Payment or Subscription rows were created
# in the window". So the forward pass stamps the pre-repoint value onto each row
# it actually changes (`meta[REPOINT_META_KEY]`, following the stamp-and-scope
# precedent in `payments.0009_backfill_unlimited_subscriptions`), and the
# reverse restores only rows carrying that stamp -- from the value stamped on
# each one, not a single hardcoded literal -- leaving every other row (including
# ones created after this migration ran) untouched.
from django.conf import settings
from django.db import migrations


#: Meta key the forward pass stamps with a row's pre-repoint `payment_provider`,
#: so the reverse can restore exactly the rows this migration changed -- and
#: only those -- instead of every `Subscription` in the table. Mirrors
#: `payments.0009_backfill_unlimited_subscriptions`'s `BACKFILL_META_KEY`.
REPOINT_META_KEY = "repointed_by_0018_previous_payment_provider"


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
            subscription.meta[REPOINT_META_KEY] = subscription.payment_provider
            subscription.payment_provider = provider
            updated.append(subscription)
    if updated:
        Subscription.objects.bulk_update(updated, ["payment_provider", "meta"], batch_size=500)


def restore_previous_payment_provider(apps, schema_editor):
    """Reverse: restore only the rows the forward pass touched, each to the
    value it individually carried before -- not a blanket "mercadopago" write.
    See the module docstring above for why."""
    Subscription = apps.get_model("payments", "Subscription")

    updated = []
    for subscription in Subscription.objects.filter(
        **{"meta__has_key": REPOINT_META_KEY}
    ).iterator():
        subscription.payment_provider = subscription.meta.pop(REPOINT_META_KEY)
        updated.append(subscription)
    if updated:
        Subscription.objects.bulk_update(updated, ["payment_provider", "meta"], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0017_backfill_billingprofile_payment_provider"),
    ]

    operations = [
        migrations.RunPython(
            repoint_to_organization_provider, reverse_code=restore_previous_payment_provider
        ),
    ]
