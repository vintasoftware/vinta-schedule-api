from django.db import migrations

from payments.billing_plans_catalog import (
    FREE_PLAN_SLUG,
    UNLIMITED_PLAN_SLUG,
    seed_billing_plans_v1,
)


def seed_billing_plans(apps, schema_editor):
    BillingPlan = apps.get_model("payments", "BillingPlan")
    PlanLimit = apps.get_model("payments", "PlanLimit")
    PlanEntitlement = apps.get_model("payments", "PlanEntitlement")
    seed_billing_plans_v1(BillingPlan, PlanLimit, PlanEntitlement)

def unseed_billing_plans(apps, schema_editor):
    """Reverse: delete the two seeded plans (and, via CASCADE, their limits and
    entitlements).

    Safe only if no `Subscription` still references these plans — true at this
    phase (3) on its own, since organizations are not placed on a plan until
    Phase 4. From Phase 4 (`payments.0009`) onward, `Subscription.plan` is
    `on_delete=PROTECT`, so reversing the full chain to before this migration
    requires reversing `0009` first — its own reverse deletes exactly the
    `Subscription` rows *it* created (tagged `meta.backfilled_by`), which is what
    keeps this delete free of a `ProtectedError`. Reversing `0007` directly while
    any organically-created (non-backfilled) `Subscription` still references
    `unlimited` or `free` still raises `ProtectedError`, by design."""
    BillingPlan = apps.get_model("payments", "BillingPlan")
    BillingPlan.objects.filter(slug__in=[UNLIMITED_PLAN_SLUG, FREE_PLAN_SLUG]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0006_planentitlement_planlimit"),
    ]

    operations = [
        migrations.RunPython(seed_billing_plans, reverse_code=unseed_billing_plans),
    ]
