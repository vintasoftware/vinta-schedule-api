"""Multi-tenancy tests for the meter's organization scoping.

``MeteringService`` is the first thing in this plan that *writes* money-bearing
rows derived from another app's tenant-scoped data, and it reaches that data
through ``EntitlementService.get_pooled_organization_ids`` rather than through a
single organization filter. That makes the pool boundary the tenancy surface of
this phase, and it has two failure directions that are not symmetric:

- **Too wide** is a cross-tenant data leak *and* a wrong invoice: another
  organization's calendar becomes billable usage on somebody else's subscription.
- **Too narrow** silently under-bills a reseller, which no test elsewhere would
  notice because under-billing raises nothing.

These mirror the three ``payments/tests/services/test_pooled_limits.py`` cases
that establish the same boundary for pre-paid counters, re-asserted here against
the meter because the meter derives the pool independently of those counters and
the two must agree — a metered occurrence that the ``event_occurrences`` counter
cannot see is a charge nobody can explain to the customer.
"""

import datetime
from decimal import Decimal

from django.utils import timezone

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarProvider, RecurrenceFrequency
from calendar_integration.factories import CalendarEventFactory
from calendar_integration.models import Calendar
from organizations.models import Organization
from payments.billing_constants import BillingState, LimitedResource, LimitKind
from payments.models import BillingPlan, MeteredOccurrence, Subscription, SubscriptionPlanLimit
from payments.services.entitlement_service import EntitlementService
from payments.services.metering_service import MeteringService


# This module builds its own `Subscription` rows on specific organizations
# (`OneToOneField`), so it opts out of conftest's autouse provisioning.
pytestmark = pytest.mark.no_auto_subscription


PERIOD_START = datetime.datetime(2025, 6, 1, 0, 0, tzinfo=datetime.UTC)
PERIOD_END = datetime.datetime(2025, 7, 1, 0, 0, tzinfo=datetime.UTC)
FIRST_MONDAY = datetime.datetime(2025, 6, 2, 10, 0, tzinfo=datetime.UTC)


@pytest.fixture
def plan() -> BillingPlan:
    return baker.make(BillingPlan, is_default_for_new_organizations=False)


@pytest.fixture
def metering_service() -> MeteringService:
    from di_core.containers import container

    assert container is not None
    return container.metering_service()


def make_subscription(organization: Organization, plan: BillingPlan) -> Subscription:
    """A subscription pinned to June 2025 with an unlimited occurrence allowance."""
    subscription = baker.make(
        Subscription,
        organization=organization,
        plan=plan,
        billing_state=BillingState.FREE,
        current_period_start=PERIOD_START,
        current_period_end=PERIOD_END,
    )
    baker.make(
        SubscriptionPlanLimit,
        subscription=subscription,
        resource_key=LimitedResource.EVENT_OCCURRENCES,
        limit_value=None,
        kind=LimitKind.POSTPAID,
    )
    return subscription


def make_calendar(organization: Organization, suffix: str) -> Calendar:
    return Calendar.objects.create(
        name=f"Calendar {suffix}",
        description="",
        external_id=f"tenancy_cal_{suffix}",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )


def make_weekly_series(calendar: Calendar, suffix: str, count: int = 5):
    """A Monday-weekly series inside June 2025 — five occurrences by default."""
    return CalendarEventFactory.create_recurring_event(
        calendar=calendar,
        title=f"Weekly {suffix}",
        description="",
        start_time=FIRST_MONDAY,
        end_time=FIRST_MONDAY + datetime.timedelta(hours=1),
        frequency=RecurrenceFrequency.WEEKLY,
        count=count,
        by_weekday="MO",
        external_id=f"tenancy_series_{suffix}",
    )


def metered_organization_ids(subscription: Subscription) -> set[int]:
    return set(
        MeteredOccurrence.objects.filter(subscription=subscription).values_list(
            "organization_id", flat=True
        )
    )


@pytest.mark.django_db
class TestMeteringPoolBoundary:
    def test_a_second_organizations_events_are_never_metered(self, metering_service, plan):
        """The cross-tenant direction: two unrelated billing roots.

        Both organizations have an identical five-occurrence series in the same
        window. Metering one must record exactly its own five. This is the test
        that fails loudly if ``expand_occurrence_identities`` ever loses its
        ``organization_id__in`` filter — without it the meter would happily bill
        every calendar in the database to whichever subscription swept first.
        """
        org_a = baker.make(Organization, parent=None, can_invite_organizations=False)
        org_b = baker.make(Organization, parent=None, can_invite_organizations=False)
        subscription_a = make_subscription(org_a, plan)
        make_subscription(org_b, plan)
        make_weekly_series(make_calendar(org_a, "a"), "a")
        make_weekly_series(make_calendar(org_b, "b"), "b")

        result = metering_service.meter_occurrences_for_period(
            subscription_a, PERIOD_START, PERIOD_END
        )

        assert result.occurrences_recorded == 5
        assert metered_organization_ids(subscription_a) == {org_a.pk}, (
            "org B's occurrences must not be billed to org A's subscription"
        )
        assert MeteredOccurrence.objects.count() == 5

    def test_a_reseller_childs_occurrences_are_metered_onto_the_root(self, metering_service, plan):
        """The pooling direction, at depth.

        A reseller child holds no subscription of its own, so its usage has to be
        recorded against the root's — and *attributed* to the child's organization,
        which is what makes a per-organization usage breakdown possible later. Two
        levels deep, because a pool that covered only direct children would let a
        reseller escape billing by nesting one level further.
        """
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        child = baker.make(Organization, parent=root, can_invite_organizations=False)
        grandchild = baker.make(Organization, parent=child, can_invite_organizations=False)
        subscription = make_subscription(root, plan)

        make_weekly_series(make_calendar(root, "root"), "root")
        make_weekly_series(make_calendar(child, "child"), "child")
        make_weekly_series(make_calendar(grandchild, "grandchild"), "grandchild")

        result = metering_service.meter_occurrences_for_period(
            subscription, PERIOD_START, PERIOD_END
        )

        assert result.occurrences_recorded == 15
        assert metered_organization_ids(subscription) == {root.pk, child.pk, grandchild.pk}
        assert not Subscription.objects.filter(organization=child).exists()

    def test_the_pool_stops_at_a_nested_billing_root(self, metering_service, plan):
        """A nested reseller pays for its own subtree.

        Its occurrences must not appear on the ancestor's ledger — that would
        charge the ancestor for capacity it never sold, and bill the same
        occurrences twice once the nested root's own sweep runs. The nested root's
        subtree is metered exactly once, by its own subscription.
        """
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        child = baker.make(Organization, parent=root, can_invite_organizations=False)
        nested_root = baker.make(Organization, parent=root, can_invite_organizations=True)
        nested_child = baker.make(Organization, parent=nested_root, can_invite_organizations=False)
        root_subscription = make_subscription(root, plan)
        nested_subscription = make_subscription(nested_root, plan)

        make_weekly_series(make_calendar(root, "root"), "root")
        make_weekly_series(make_calendar(child, "child"), "child")
        make_weekly_series(make_calendar(nested_root, "nested"), "nested")
        make_weekly_series(make_calendar(nested_child, "nested_child"), "nested_child")

        metering_service.meter_occurrences_for_period(root_subscription, PERIOD_START, PERIOD_END)
        metering_service.meter_occurrences_for_period(nested_subscription, PERIOD_START, PERIOD_END)

        assert metered_organization_ids(root_subscription) == {root.pk, child.pk}
        assert metered_organization_ids(nested_subscription) == {
            nested_root.pk,
            nested_child.pk,
        }
        # Every occurrence billed exactly once across both ledgers.
        assert MeteredOccurrence.objects.count() == 20

    def test_the_meter_and_the_usage_counter_see_the_same_pool(self, metering_service, plan):
        """The two sides of the pool must agree, not merely each be correct.

        The meter derives the pool through ``get_pooled_organization_ids`` and the
        ``event_occurrences`` counter re-derives it through the same method. If
        they ever diverge, a customer is billed for occurrences their own usage
        readout cannot account for.
        """
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        child = baker.make(Organization, parent=root, can_invite_organizations=False)
        nested_root = baker.make(Organization, parent=root, can_invite_organizations=True)
        subscription = make_subscription(root, plan)
        make_subscription(nested_root, plan)

        make_weekly_series(make_calendar(root, "root"), "root")
        make_weekly_series(make_calendar(child, "child"), "child")
        make_weekly_series(make_calendar(nested_root, "nested"), "nested")

        metering_service.meter_occurrences_for_period(subscription, PERIOD_START, PERIOD_END)

        # `INSIDE_PERIOD`-equivalent: the counter resolves its period from `now`,
        # so the subscription's stored cycle is moved to contain it rather than
        # freezing the clock, which `baker`-built rows make simpler here.
        now = timezone.now()
        Subscription.objects.filter(pk=subscription.pk).update(
            current_period_start=now - datetime.timedelta(days=1),
            current_period_end=now + datetime.timedelta(days=29),
        )
        MeteredOccurrence.objects.filter(subscription=subscription).update(
            billing_period_start=now - datetime.timedelta(days=1)
        )

        usage = EntitlementService().get_current_usage(child, LimitedResource.EVENT_OCCURRENCES)
        assert usage == 10, "root + child, and specifically not the nested root's 5"


@pytest.mark.django_db
class TestSweepSkipsDemotedBillingRoots:
    """``subscriptions_to_sweep`` must not trust an unenforced invariant.

    "A ``Subscription`` exists only on a billing root" is true of every code path
    that *creates* one, and is enforced nowhere in the database. An ordinary admin
    edit breaks it: re-parenting an organization under a reseller, or clearing
    ``can_invite_organizations``, demotes a root and leaves its ``Subscription``
    behind.

    That is not merely redundant work. ``expand_occurrence_identities`` pools
    *its billing root's* subtree, which after demotion is the ancestor's — so
    sweeping the demoted subscription would meter the ancestor's entire subtree a
    second time under a different subscription id, corrupting both allowance
    positions.
    """

    def test_a_demoted_root_is_excluded_from_the_sweep(self, plan):
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        demoted = baker.make(Organization, parent=None, can_invite_organizations=False)
        root_subscription = make_subscription(root, plan)
        demoted_subscription = make_subscription(demoted, plan)

        assert demoted_subscription.pk in set(MeteringService.subscriptions_to_sweep())

        # The admin edit: `demoted` is re-parented under `root` and is not itself a
        # reseller, so it now pools against `root`'s subscription.
        Organization.objects.filter(pk=demoted.pk).update(parent=root)

        to_sweep = set(MeteringService.subscriptions_to_sweep())
        assert root_subscription.pk in to_sweep
        assert demoted_subscription.pk not in to_sweep

    def test_the_exclusion_is_logged_rather_than_silent(self, plan, caplog):
        """A violated invariant has to be visible.

        Silently skipping leaves a subscription whose ledger nobody is maintaining
        and nobody knows about; the log line is what turns it into something an
        operator can reconcile.
        """
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        demoted = baker.make(Organization, parent=None, can_invite_organizations=False)
        make_subscription(root, plan)
        demoted_subscription = make_subscription(demoted, plan)
        Organization.objects.filter(pk=demoted.pk).update(parent=root)

        with caplog.at_level("WARNING", logger="payments.services.metering_service"):
            MeteringService.subscriptions_to_sweep()

        assert str(demoted_subscription.pk) in caplog.text
        assert "no longer billing roots" in caplog.text

    def test_a_nested_reseller_is_still_swept(self, plan):
        """The filter must not over-exclude.

        A nested reseller (``can_invite_organizations=True`` *with* a parent) is
        its own billing root and pays for its own subtree, so it has to keep being
        swept — excluding it would silently stop billing an entire customer.
        """
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        nested_root = baker.make(Organization, parent=root, can_invite_organizations=True)
        make_subscription(root, plan)
        nested_subscription = make_subscription(nested_root, plan)

        assert nested_subscription.pk in set(MeteringService.subscriptions_to_sweep())


@pytest.mark.django_db
class TestPreFilterMatchesTheUniqueConstraint:
    """``_existing_identities`` must not be stricter than the constraint it predicts.

    The constraint is ``(organization, event_id, occurrence_start)`` — no
    subscription column. A pre-filter that also narrowed by ``subscription_id``
    would miss rows written under a *different* subscription for the same
    organization, and those rows still conflict on insert. The occurrence would
    then consume an allowance position without producing a row, pushing a
    genuinely new occurrence into overage while the organization is under its
    ceiling — an overcharge that ``reconcile_period`` reports as ``drift == 0``.

    Reachable without corruption: an organization that was its own billing root
    (and so had its own ``Subscription``, and rows stamped with it) is re-parented
    under a reseller, after which the ancestor's sweep meters its events.
    """

    def test_rows_from_a_previous_subscription_are_not_re_offered(self, metering_service, plan):
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        formerly_independent = baker.make(Organization, parent=None, can_invite_organizations=False)
        root_subscription = make_subscription(root, plan)
        own_subscription = make_subscription(formerly_independent, plan)

        make_weekly_series(make_calendar(formerly_independent, "own"), "own")

        # Metered while it was still its own billing root: 5 rows stamped with its
        # own subscription.
        metering_service.meter_occurrences_for_period(own_subscription, PERIOD_START, PERIOD_END)
        assert MeteredOccurrence.objects.filter(subscription=own_subscription).count() == 5

        # The demotion.
        Organization.objects.filter(pk=formerly_independent.pk).update(parent=root)

        # The ancestor now sweeps the same occurrences. They are already recorded
        # under the constraint tuple, so nothing new is written.
        result = metering_service.meter_occurrences_for_period(
            root_subscription, PERIOD_START, PERIOD_END
        )

        assert result.occurrences_seen == 5
        assert result.occurrences_recorded == 0
        assert MeteredOccurrence.objects.count() == 5, (
            "the same occurrence must not be billed twice because it changed subscription"
        )

    def test_the_allowance_is_not_spent_on_occurrences_that_cannot_be_inserted(
        self, metering_service, plan
    ):
        """The overcharge the pre-filter mismatch actually causes.

        The demoted organization's five occurrences already exist under its old
        subscription. A *new* organization in the same pool then adds occurrences.
        With an allowance of 5, those new occurrences must be inside it — they are
        the only rows this subscription is writing. If the pre-filter missed the
        pre-existing rows, they would each consume a slot first and push the new
        ones into overage.
        """
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        formerly_independent = baker.make(Organization, parent=None, can_invite_organizations=False)
        root_subscription = make_subscription(root, plan)
        own_subscription = make_subscription(formerly_independent, plan)
        root_subscription.limits.filter(resource_key=LimitedResource.EVENT_OCCURRENCES).update(
            limit_value=5, overage_unit_price=Decimal("1.0000")
        )

        make_weekly_series(make_calendar(formerly_independent, "own"), "own")
        metering_service.meter_occurrences_for_period(own_subscription, PERIOD_START, PERIOD_END)

        Organization.objects.filter(pk=formerly_independent.pk).update(parent=root)
        make_weekly_series(make_calendar(root, "root"), "root")

        metering_service.meter_occurrences_for_period(root_subscription, PERIOD_START, PERIOD_END)

        new_rows = MeteredOccurrence.objects.filter(subscription=root_subscription)
        assert new_rows.count() == 5
        assert all(row.is_within_allowance for row in new_rows), (
            "the root's own five occurrences fit its allowance of five"
        )
        assert sum(row.unit_price for row in new_rows) == Decimal("0.0000")
