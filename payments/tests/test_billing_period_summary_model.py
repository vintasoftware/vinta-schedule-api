"""Phase 0 scaffolding tests for `BillingPeriodSummary` / `BillingPeriodResourceUsage`.

This phase ships no reader or writer for these tables — `CycleCloseService`
starts persisting to them in Phase 2. What has to hold *now* is the schema
itself: the two unique constraints that make cycle-close idempotent (the same
pattern `MeteredOccurrence` and `ProviderWebhookEvent` already use), the
two-nulls distinction on `BillingPeriodResourceUsage` (`total=None` means "not
recorded", `limit_value=None` means "unlimited" -- collapsing either into `0`
is the bug a reviewer should catch), the `for_organizations` pool scope, and
that the migration creating both tables reverses cleanly.
"""

import datetime

from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor

import pytest
from model_bakery import baker

from organizations.models import Organization
from payments.models import BillingPeriodResourceUsage, BillingPeriodSummary, Subscription


PERIOD_START = datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC)
PERIOD_END = datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC)


@pytest.fixture
def organization() -> Organization:
    return baker.make(Organization)


@pytest.fixture
def subscription(organization: Organization) -> Subscription:
    # `provision_default_subscription` (root conftest, autouse) already gave this
    # organization a `Subscription` on creation -- reuse it rather than trying to
    # create a second one, which would raise on the `OneToOneField`.
    return organization.subscription


@pytest.fixture
def summary(organization: Organization, subscription: Subscription) -> BillingPeriodSummary:
    return baker.make(
        BillingPeriodSummary,
        subscription=subscription,
        organization=organization,
        billing_period_start=PERIOD_START,
        billing_period_end=PERIOD_END,
        overage_total="12.5000",
        charged=True,
        reconciliation_unmetered=0,
        reconciliation_orphaned=0,
        closed_at=datetime.datetime.now(tz=datetime.UTC),
    )


@pytest.mark.django_db
class TestBillingPeriodSummaryUniqueConstraint:
    def test_duplicate_subscription_and_period_start_rejected(
        self, summary: BillingPeriodSummary, subscription: Subscription, organization: Organization
    ):
        """`uniq_billing_period_summary` on `(subscription, billing_period_start)`
        is the correctness mechanism cycle close relies on for idempotent catch-up
        re-entry -- a duplicate write for the same period must fail at the
        database level."""
        with pytest.raises(IntegrityError):
            baker.make(
                BillingPeriodSummary,
                subscription=subscription,
                organization=organization,
                billing_period_start=PERIOD_START,
                billing_period_end=PERIOD_END,
                overage_total="0.0000",
                charged=False,
                reconciliation_unmetered=0,
                reconciliation_orphaned=0,
                closed_at=datetime.datetime.now(tz=datetime.UTC),
            )

    def test_same_subscription_different_period_start_allowed(
        self, summary: BillingPeriodSummary, subscription: Subscription, organization: Organization
    ):
        next_period_start = PERIOD_END
        next_period_end = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)

        second = baker.make(
            BillingPeriodSummary,
            subscription=subscription,
            organization=organization,
            billing_period_start=next_period_start,
            billing_period_end=next_period_end,
            overage_total="0.0000",
            charged=False,
            reconciliation_unmetered=0,
            reconciliation_orphaned=0,
            closed_at=datetime.datetime.now(tz=datetime.UTC),
        )

        assert second.pk is not None
        assert BillingPeriodSummary.objects.filter(subscription=subscription).count() == 2


@pytest.mark.django_db
class TestBillingPeriodResourceUsageUniqueConstraint:
    def test_duplicate_summary_and_resource_key_rejected(self, summary: BillingPeriodSummary):
        baker.make(
            BillingPeriodResourceUsage,
            summary=summary,
            resource_key="event_occurrences",
            total=100,
        )

        with pytest.raises(IntegrityError):
            baker.make(
                BillingPeriodResourceUsage,
                summary=summary,
                resource_key="event_occurrences",
                total=200,
            )

    def test_same_summary_different_resource_key_allowed(self, summary: BillingPeriodSummary):
        baker.make(BillingPeriodResourceUsage, summary=summary, resource_key="event_occurrences")
        second = baker.make(
            BillingPeriodResourceUsage, summary=summary, resource_key="organization_members"
        )

        assert second.pk is not None
        assert summary.resources.count() == 2


@pytest.mark.django_db
class TestBillingPeriodResourceUsageTwoNulls:
    """`total=None` ("not recorded") and `limit_value=None` ("unlimited") are two
    distinct, independent nulls that must never collapse into `0` or into each
    other -- this is the assertion that protects that decision."""

    def test_total_none_round_trips_and_is_distinct_from_zero(self, summary: BillingPeriodSummary):
        not_recorded = baker.make(
            BillingPeriodResourceUsage,
            summary=summary,
            resource_key="event_occurrences",
            total=None,
        )
        zero_recorded = baker.make(
            BillingPeriodResourceUsage,
            summary=summary,
            resource_key="organization_members",
            total=0,
        )

        not_recorded.refresh_from_db()
        zero_recorded.refresh_from_db()

        assert not_recorded.total is None
        assert zero_recorded.total == 0
        assert not_recorded.total != zero_recorded.total

    def test_limit_value_none_round_trips_and_is_distinct_from_zero(
        self, summary: BillingPeriodSummary
    ):
        unlimited = baker.make(
            BillingPeriodResourceUsage,
            summary=summary,
            resource_key="event_occurrences",
            limit_value=None,
        )
        zero_limit = baker.make(
            BillingPeriodResourceUsage,
            summary=summary,
            resource_key="organization_members",
            limit_value=0,
        )

        unlimited.refresh_from_db()
        zero_limit.refresh_from_db()

        assert unlimited.limit_value is None
        assert zero_limit.limit_value == 0
        assert unlimited.limit_value != zero_limit.limit_value

    def test_total_and_limit_value_nulls_are_independent(self, summary: BillingPeriodSummary):
        """A row can be `total=None, limit_value=100` (not yet recorded, but with
        a known ceiling) or `total=50, limit_value=None` (recorded, unlimited) --
        neither null implies the other."""
        row = baker.make(
            BillingPeriodResourceUsage,
            summary=summary,
            resource_key="event_occurrences",
            total=None,
            limit_value=100,
        )
        row.refresh_from_db()

        assert row.total is None
        assert row.limit_value == 100


@pytest.mark.django_db
class TestBillingPeriodSummaryQuerySetForOrganizations:
    def test_for_organizations_restricts_to_the_given_pool(self):
        in_pool_org = baker.make(Organization)
        outside_pool_org = baker.make(Organization)

        in_pool_summary = baker.make(
            BillingPeriodSummary,
            subscription=in_pool_org.subscription,
            organization=in_pool_org,
            billing_period_start=PERIOD_START,
            billing_period_end=PERIOD_END,
            overage_total="0.0000",
            charged=False,
            reconciliation_unmetered=0,
            reconciliation_orphaned=0,
            closed_at=datetime.datetime.now(tz=datetime.UTC),
        )
        baker.make(
            BillingPeriodSummary,
            subscription=outside_pool_org.subscription,
            organization=outside_pool_org,
            billing_period_start=PERIOD_START,
            billing_period_end=PERIOD_END,
            overage_total="0.0000",
            charged=False,
            reconciliation_unmetered=0,
            reconciliation_orphaned=0,
            closed_at=datetime.datetime.now(tz=datetime.UTC),
        )

        result = BillingPeriodSummary.objects.for_organizations([in_pool_org.pk])

        assert list(result) == [in_pool_summary]

    def test_for_organizations_empty_pool_returns_nothing(self, summary: BillingPeriodSummary):
        assert not BillingPeriodSummary.objects.for_organizations([]).exists()


@pytest.mark.django_db(transaction=True)
class TestBillingPeriodSummaryMigration:
    def test_migration_applies_and_reverses_cleanly(self):
        """`0020_billing_period_summary` creates both tables in one migration with
        no data migration. Reversing it must cleanly drop both tables, and
        re-applying it must recreate them -- proving `make migrate` (forward) and
        its reverse are both safe.

        The test DB is already migrated to head before this runs, so this drives
        the executor back one migration and forward again, restoring head in a
        `finally` so later tests in this worker's database still see the tables
        regardless of assertion outcome.

        **The restore has to be the whole graph, not this app's head.** Stepping
        `payments` back to `0019` unapplies every migration in *any* app that
        depends on a later `payments` one -- `organizations.0027` onwards reach
        back to `payments.0022` -- and migrating forward to `payments.0020`
        re-applies only `payments`. That was invisible while the collateral
        migrations were data-only; `organizations.0030` (Phase 6 of the
        vinta-django-orgs migration) drops two columns, so leaving it unapplied
        restores a NOT NULL `role` column no live model writes, and every
        membership insert in every later test in this worker fails. Restoring to
        `leaf_nodes()` puts every app back at head.

        `previous` must stay the migration immediately preceding this one in the
        `payments` graph, not merely the one it was generated against: renumbering
        this migration to land after an unrelated branch's migrations (which is
        what happened when `payments` grew 0016-0019 on main) moves that neighbour,
        and stepping back to a stale name would either fail to resolve or unapply
        far more than this migration.
        """
        app_label = "payments"
        previous = "0019_backfill_subscription_payment_provider_on_payments"

        executor = MigrationExecutor(connection)
        try:
            executor.migrate([(app_label, previous)])
            executor.loader.build_graph()

            table_names = connection.introspection.table_names()
            assert "payments_billingperiodsummary" not in table_names
            assert "payments_billingperiodresourceusage" not in table_names
        finally:
            executor.migrate(executor.loader.graph.leaf_nodes())
            executor.loader.build_graph()

        table_names = connection.introspection.table_names()
        assert "payments_billingperiodsummary" in table_names
        assert "payments_billingperiodresourceusage" in table_names
