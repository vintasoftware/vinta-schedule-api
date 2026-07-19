"""Verifies the ``0008_backfill_unlimited_subscriptions`` data migration's end
state — every pre-existing organization is on ``unlimited`` afterward, never
``free`` (objective 6: no organization is blocked as a consequence of the
rollout itself).

Like ``test_plan_seed_migration.py``, the test DB is already fully migrated
before any test runs, so the migration ran once already (against an empty
``Organization`` table at that point). This re-invokes the migration's own
function directly against freshly created organizations to verify its actual
behavior, per that same file's precedent.
"""

import importlib

import pytest
from model_bakery import baker

from organizations.models import Organization
from payments.billing_constants import LimitedResource
from payments.models import BillingPlan, Subscription


migration_module = importlib.import_module(
    "payments.migrations.0008_backfill_unlimited_subscriptions"
)
backfill_unlimited_subscriptions = migration_module.backfill_unlimited_subscriptions


@pytest.mark.django_db
class TestBackfillUnlimitedSubscriptionsMigration:
    def test_every_preexisting_root_organization_ends_up_on_unlimited(self):
        from django.apps import apps

        root_a = baker.make(Organization, parent=None)
        root_b = baker.make(Organization, parent=None)

        backfill_unlimited_subscriptions(apps, None)

        for org in (root_a, root_b):
            subscription = Subscription.objects.get(organization=org)
            assert subscription.plan.slug == "unlimited"
            assert subscription.plan.slug != "free"

    def test_reseller_child_gets_no_subscription(self):
        from django.apps import apps

        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        child = baker.make(Organization, parent=root, can_invite_organizations=False)

        backfill_unlimited_subscriptions(apps, None)

        assert Subscription.objects.filter(organization=root).exists()
        assert not Subscription.objects.filter(organization=child).exists()

    def test_copies_every_limited_resource_as_a_subscription_plan_limit(self):
        from django.apps import apps

        org = baker.make(Organization, parent=None)

        backfill_unlimited_subscriptions(apps, None)

        subscription = Subscription.objects.get(organization=org)
        assert subscription.limits.count() == len(LimitedResource.values)
        assert all(limit.limit_value is None for limit in subscription.limits.all())

    def test_idempotent_does_not_duplicate_or_touch_existing_subscriptions(self):
        from django.apps import apps

        org = baker.make(Organization, parent=None)

        backfill_unlimited_subscriptions(apps, None)
        first_subscription = Subscription.objects.get(organization=org)

        # An org already placed on a real (non-unlimited) plan by the time the
        # migration re-runs (e.g. a re-deploy) must not be touched.
        free_plan = BillingPlan.objects.get(slug="free")
        first_subscription.plan = free_plan
        first_subscription.save(update_fields=["plan"])

        backfill_unlimited_subscriptions(apps, None)

        assert Subscription.objects.filter(organization=org).count() == 1
        first_subscription.refresh_from_db()
        assert first_subscription.plan.slug == "free"
