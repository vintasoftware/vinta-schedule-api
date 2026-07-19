"""Verifies the ``0008_backfill_unlimited_subscriptions`` data migration's end
state — every pre-existing billing-root organization is on ``unlimited``
afterward, never ``free`` (objective 6: no organization is blocked as a
consequence of the rollout itself).

Like ``test_plan_seed_migration.py``, the test DB is already fully migrated
before any test runs, so the migration ran once already (against an empty
``Organization`` table at that point). This re-invokes the migration's own
function directly against freshly created organizations to verify its actual
behavior, per that same file's precedent.

Deviation from strict migration-isolation testing (Phase 4 review finding 12):
this calls ``backfill_unlimited_subscriptions`` with the *live* app registry
(``django.apps.apps``), not the historical one a real ``RunPython`` step
receives via ``schema_editor``'s migration state. ``test_plan_seed_migration.py``
already establishes this precedent for ``0006``. It is safe here because the
migration function only calls ``apps.get_model(...)`` for models whose shape at
``0008`` is identical to their current shape (no field added/removed/renamed on
``Organization``, ``BillingPlan``, ``Subscription``, ``SubscriptionPlanLimit``,
or ``SubscriptionEntitlement`` by a migration between ``0008`` and head at the
time of writing) — so the live and historical model classes behave identically
for every operation this migration performs. If a future migration changes one
of those models' shape, this deviation becomes a real risk and these tests
should switch to a ``django_test_migrations``/``MigrationExecutor``-driven
historical-state fixture instead.
"""

import importlib

from django.apps import apps

import pytest
from model_bakery import baker

from organizations.models import Organization
from payments.billing_constants import LimitedResource
from payments.exceptions import MissingSeedBillingPlanError
from payments.models import BillingPlan, Subscription


migration_module = importlib.import_module(
    "payments.migrations.0008_backfill_unlimited_subscriptions"
)
backfill_unlimited_subscriptions = migration_module.backfill_unlimited_subscriptions


@pytest.mark.django_db
class TestBackfillUnlimitedSubscriptionsMigration:
    def test_every_preexisting_root_organization_ends_up_on_unlimited(self):
        root_a = baker.make(Organization, parent=None)
        root_b = baker.make(Organization, parent=None)

        backfill_unlimited_subscriptions(apps, None)

        for org in (root_a, root_b):
            subscription = Subscription.objects.get(organization=org)
            assert subscription.plan.slug == "unlimited"
            assert subscription.plan.slug != "free"

    def test_backfilled_subscriptions_are_stamped_for_a_safe_reverse(self):
        """`Subscription.plan` is `on_delete=PROTECT`, so `payments.0006`'s
        reverse (deleting the seeded plans) would raise `ProtectedError` unless
        `0008`'s reverse can identify and delete exactly the rows it created —
        the `meta.backfilled_by` stamp is what makes that possible (BLOCKER 5,
        Phase 4 review)."""
        org = baker.make(Organization, parent=None)

        backfill_unlimited_subscriptions(apps, None)

        subscription = Subscription.objects.get(organization=org)
        assert subscription.meta.get("backfilled_by") == "payments.0008"

    def test_reverse_deletes_only_the_subscriptions_it_backfilled(self):
        """An organically-created (non-backfilled) `unlimited` subscription — the
        documented support rollback, `change_plan` back to `unlimited` — must
        survive the reverse untouched."""
        backfilled_org = baker.make(Organization, parent=None)
        organic_org = baker.make(Organization, parent=None)

        backfill_unlimited_subscriptions(apps, None)

        # Simulate an org legitimately placed on `unlimited` some other way (e.g.
        # the documented support rollback) by clearing the migration's stamp on
        # its subscription — this is what distinguishes it from a truly
        # backfilled row, since both otherwise reference the same plan.
        organic_subscription = Subscription.objects.get(organization=organic_org)
        organic_subscription.meta = {}
        organic_subscription.save(update_fields=["meta"])

        migration_module.delete_backfilled_subscriptions(apps, None)

        assert not Subscription.objects.filter(organization=backfilled_org).exists()
        assert Subscription.objects.filter(organization=organic_org).exists()

    def test_reseller_child_gets_no_subscription(self):
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        child = baker.make(Organization, parent=root, can_invite_organizations=False)

        backfill_unlimited_subscriptions(apps, None)

        assert Subscription.objects.filter(organization=root).exists()
        assert not Subscription.objects.filter(organization=child).exists()

    def test_nested_reseller_gets_its_own_subscription(self):
        """BLOCKER 1, Phase 4 review: a nested reseller
        (`can_invite_organizations=True` with `parent` set) is its own billing
        root and must be backfilled onto its own `Subscription`, not skipped as
        if it were an ordinary child."""
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        mid = baker.make(Organization, parent=root, can_invite_organizations=True)
        leaf = baker.make(Organization, parent=mid, can_invite_organizations=False)

        backfill_unlimited_subscriptions(apps, None)

        assert Subscription.objects.filter(organization=root).exists()
        assert Subscription.objects.filter(organization=mid).exists()
        assert not Subscription.objects.filter(organization=leaf).exists()

    def test_copies_every_limited_resource_as_a_subscription_plan_limit(self):
        org = baker.make(Organization, parent=None)

        backfill_unlimited_subscriptions(apps, None)

        subscription = Subscription.objects.get(organization=org)
        assert subscription.limits.count() == len(LimitedResource.values)
        assert all(limit.limit_value is None for limit in subscription.limits.all())

    def test_idempotent_does_not_duplicate_or_touch_existing_subscriptions(self):
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

    def test_missing_unlimited_plan_raises_instead_of_silently_no_opping(self):
        """BLOCKER 2, Phase 4 review: a missing seeded plan means a corrupted or
        out-of-order deploy — every organization would otherwise stay plan-less
        permanently with no signal and no re-run path. Must fail loudly."""
        baker.make(Organization, parent=None)
        BillingPlan.objects.filter(slug="unlimited").delete()

        with pytest.raises(MissingSeedBillingPlanError):
            backfill_unlimited_subscriptions(apps, None)
