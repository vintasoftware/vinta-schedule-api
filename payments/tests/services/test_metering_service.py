"""Unit tests for ``MeteringService`` — the meter behind post-paid event billing.

Every assertion here is about a number that ends up on an invoice, so the tests
are written to fail loudly on *silence*: the failure mode these tests prevent
produces no exception, no log line, and no red test — just a wrong count.

Three properties are key, and each has a test that fails if the mechanism behind
it is removed rather than merely if the code changes shape:

- expansion is **window-bounded**, so an open-ended weekly series contributes a
  handful of occurrences per cycle rather than an unbounded charge at creation;
- re-metering a window is a **no-op**, which is what makes the overlapping sweep
  window safe;
- overlapping windows **do not double-count**, which is what makes a missed run
  self-heal.
"""

import datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from dateutil.relativedelta import relativedelta
from freezegun import freeze_time

from calendar_integration.constants import CalendarProvider, RecurrenceFrequency
from calendar_integration.factories import CalendarEventFactory
from calendar_integration.models import Calendar, CalendarEvent
from organizations.models import Organization
from payments.billing_constants import LimitedResource
from payments.exceptions import BillingPeriodResolutionError
from payments.models import MeteredOccurrence, Subscription
from payments.services.entitlement_service import EntitlementService
from payments.services.metering_service import MAX_SERIES_CHAIN_DEPTH, MeteringService
from payments.services.subscription_service import resolve_billing_period


#: A whole calendar month used as the subscription's billing period, chosen so the
#: weekly series below lands on five Mondays (2, 9, 16, 23, 30 June 2025). Fixed
#: dates rather than offsets from ``now`` so the expected counts in these tests are
#: arithmetic a reader can check by hand.
PERIOD_START = datetime.datetime(2025, 6, 1, 0, 0, tzinfo=datetime.UTC)
PERIOD_END = datetime.datetime(2025, 7, 1, 0, 0, tzinfo=datetime.UTC)
FIRST_MONDAY = datetime.datetime(2025, 6, 2, 10, 0, tzinfo=datetime.UTC)

#: A moment inside ``[PERIOD_START, PERIOD_END)``, for freezing "now" whenever a
#: test exercises something that resolves *the current* billing cycle. The usage
#: counter derives its period from ``timezone.now()`` rather than from the stored
#: ``current_period_start`` (see ``current_billing_period_start``), so without
#: freezing, these assertions would depend on the date the suite runs.
INSIDE_PERIOD = datetime.datetime(2025, 6, 17, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def organization(db) -> Organization:
    return Organization.objects.create(name="Metering Org", should_sync_rooms=False)


@pytest.fixture
def subscription(organization: Organization) -> Subscription:
    """The organization's auto-provisioned subscription, pinned to a known cycle.

    ``conftest.provision_default_subscription`` already placed it on the seeded
    ``unlimited`` plan (every ``limit_value`` NULL) — the state every organization
    is in for the whole rollout, and therefore the state that has to be right.
    """
    subscription = Subscription.objects.get(organization=organization)
    subscription.current_period_start = PERIOD_START
    subscription.current_period_end = PERIOD_END
    subscription.save(update_fields=["current_period_start", "current_period_end", "modified"])
    return subscription


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Metering Calendar",
        description="",
        external_id="metering_cal_1",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )


@pytest.fixture
def metering_service() -> MeteringService:
    # Deferred import: `di_core.containers.container` is only assigned in
    # `DICoreConfig.ready()`, so a module-level import binds `None` forever. Same
    # pattern as the root conftest and the other billing test modules.
    from di_core.containers import container

    assert container is not None
    return container.metering_service()


@pytest.fixture
def open_ended_weekly_event(calendar: Calendar) -> CalendarEvent:
    """A weekly series with **no** ``count`` and **no** ``until`` — infinite.

    The spec's *Counting* rule in one fixture: this series has unboundedly many
    occurrences, and the meter must charge for the ones that happened in the
    window, not for the series.
    """
    return CalendarEventFactory.create_recurring_event(
        calendar=calendar,
        title="Weekly Sync",
        description="",
        start_time=FIRST_MONDAY,
        end_time=FIRST_MONDAY + datetime.timedelta(hours=1),
        frequency=RecurrenceFrequency.WEEKLY,
        by_weekday="MO",
        external_id="weekly_master_metering",
    )


def _metered_keys(subscription: Subscription) -> set[tuple[int, datetime.datetime]]:
    return set(
        MeteredOccurrence.objects.filter(subscription=subscription).values_list(
            "event_id", "occurrence_start"
        )
    )


@pytest.mark.django_db
class TestWindowBoundedExpansion:
    """An infinite series must cost a finite amount per cycle."""

    def test_open_ended_weekly_series_meters_one_row_per_occurrence_in_the_month(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        open_ended_weekly_event: CalendarEvent,
    ):
        """Five Mondays in June 2025 → five rows, not an unbounded number.

        If expansion ever stopped being window-bounded this would not fail with a
        slightly wrong number; it would hang or explode, which is the point.
        """
        result = metering_service.meter_occurrences_for_period(
            subscription, PERIOD_START, PERIOD_END
        )

        assert result.occurrences_seen == 5
        assert result.occurrences_recorded == 5
        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 5
        assert sorted(
            MeteredOccurrence.objects.filter(subscription=subscription).values_list(
                "occurrence_start", flat=True
            )
        ) == [FIRST_MONDAY + datetime.timedelta(weeks=week) for week in range(5)]

    def test_a_narrow_window_meters_only_what_falls_inside_it(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        open_ended_weekly_event: CalendarEvent,
    ):
        """The same infinite series over one week yields exactly one occurrence."""
        result = metering_service.meter_occurrences_for_period(
            subscription,
            FIRST_MONDAY - datetime.timedelta(hours=1),
            FIRST_MONDAY + datetime.timedelta(days=1),
        )

        assert result.occurrences_recorded == 1
        assert _metered_keys(subscription) == {(open_ended_weekly_event.pk, FIRST_MONDAY)}

    def test_the_window_is_half_open(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        open_ended_weekly_event: CalendarEvent,
    ):
        """An occurrence starting exactly at ``window_end`` belongs to the next window.

        Without this, two windows that tile the timeline (``[a, b)`` then ``[b, c)``)
        would each claim the occurrence at ``b`` — the unique constraint would absorb
        it here, but the same off-by-one at a *billing period* boundary decides which
        invoice it lands on.
        """
        metering_service.meter_occurrences_for_period(subscription, PERIOD_START, FIRST_MONDAY)
        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 0

        metering_service.meter_occurrences_for_period(
            subscription, FIRST_MONDAY, FIRST_MONDAY + datetime.timedelta(seconds=1)
        )
        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 1

    def test_a_non_positive_window_meters_nothing(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        open_ended_weekly_event: CalendarEvent,
    ):
        result = metering_service.meter_occurrences_for_period(
            subscription, PERIOD_END, PERIOD_START
        )

        assert result.occurrences_recorded == 0
        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 0


@pytest.mark.django_db
class TestIdempotence:
    """Re-running the meter is the normal case, not the exceptional one."""

    def test_metering_the_same_window_twice_yields_identical_rows(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        open_ended_weekly_event: CalendarEvent,
    ):
        """Acceptance: metering a period twice yields identical counts."""
        first = metering_service.meter_occurrences_for_period(
            subscription, PERIOD_START, PERIOD_END
        )
        keys_after_first = _metered_keys(subscription)

        second = metering_service.meter_occurrences_for_period(
            subscription, PERIOD_START, PERIOD_END
        )

        assert first.occurrences_recorded == 5
        # The second pass sees the same occurrences and records none of them.
        assert second.occurrences_seen == 5
        assert second.occurrences_recorded == 0
        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 5
        assert _metered_keys(subscription) == keys_after_first

    def test_overlapping_windows_do_not_double_count(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        open_ended_weekly_event: CalendarEvent,
    ):
        """Two windows sharing a fortnight cover the month exactly once.

        This is the property the scheduled sweep depends on: each run re-reads a
        stretch the previous run already read, so a run that never happened is made
        up for by the next one.
        """
        overlap_start = datetime.datetime(2025, 6, 10, 0, 0, tzinfo=datetime.UTC)
        overlap_end = datetime.datetime(2025, 6, 24, 0, 0, tzinfo=datetime.UTC)

        metering_service.meter_occurrences_for_period(subscription, PERIOD_START, overlap_end)
        metering_service.meter_occurrences_for_period(subscription, overlap_start, PERIOD_END)

        rows = MeteredOccurrence.objects.filter(subscription=subscription)
        assert rows.count() == 5
        # No key appears twice — the count above would also pass if a row were
        # missing and a duplicate present, so assert distinctness directly.
        assert len(_metered_keys(subscription)) == 5

    def test_a_racing_sweep_that_sees_a_stale_ledger_still_cannot_double_record(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        open_ended_weekly_event: CalendarEvent,
    ):
        """The constraint, doing the job the application code cannot.

        In the ordinary sequential case the meter subtracts what it has already
        recorded before inserting, so no duplicate is ever offered to the database —
        which means the tests above would keep passing with the constraint dropped.
        They prove idempotence; they do not prove *what* enforces it.

        This injects the one state the subtraction cannot defend against: a second
        sweep whose read of the ledger happened before the first sweep's rows were
        visible. It then offers every occurrence for insertion a second time. The
        row count is unchanged because ``bulk_create(ignore_conflicts=True)`` hits
        the unique constraint — nothing in Python noticed.
        """
        metering_service.meter_occurrences_for_period(subscription, PERIOD_START, PERIOD_END)
        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 5

        with patch.object(
            MeteringService, "_existing_identities", staticmethod(lambda *_args: set())
        ):
            metering_service.meter_occurrences_for_period(subscription, PERIOD_START, PERIOD_END)

        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 5
        assert len(_metered_keys(subscription)) == 5

    def test_a_duplicate_row_cannot_be_inserted_at_all(
        self,
        subscription: Subscription,
        organization: Organization,
    ):
        """The constraint, asserted directly rather than through the service.

        Everything above would keep passing if idempotence were achieved by an
        application-level "have I seen this?" check instead of by the database.
        This is the test that distinguishes the two.
        """
        from django.db import IntegrityError

        def _row() -> MeteredOccurrence:
            return MeteredOccurrence(
                organization=organization,
                subscription=subscription,
                event_id=4242,
                occurrence_start=FIRST_MONDAY,
                billing_period_start=PERIOD_START,
                is_within_allowance=True,
                unit_price=Decimal("0"),
            )

        _row().save()
        with pytest.raises(IntegrityError):
            _row().save()


@pytest.mark.django_db
class TestAllowanceAndPriceStamping:
    """``is_within_allowance`` and ``unit_price`` freeze at meter time."""

    @staticmethod
    def _set_allowance(
        subscription: Subscription, limit_value: int | None, unit_price: str | None
    ) -> None:
        subscription.limits.filter(resource_key=LimitedResource.EVENT_OCCURRENCES).update(
            limit_value=limit_value,
            overage_unit_price=None if unit_price is None else Decimal(unit_price),
        )

    def test_unlimited_plan_stamps_everything_as_included_at_zero(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        open_ended_weekly_event: CalendarEvent,
    ):
        """The rollout state: every organization is on ``unlimited``, so nothing is
        overage and nothing is priced."""
        metering_service.meter_occurrences_for_period(subscription, PERIOD_START, PERIOD_END)

        rows = MeteredOccurrence.objects.filter(subscription=subscription)
        assert rows.count() == 5
        assert all(row.is_within_allowance for row in rows)
        assert {row.unit_price for row in rows} == {Decimal("0.0000")}

    def test_occurrences_past_the_allowance_are_priced_at_the_overage_rate(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        open_ended_weekly_event: CalendarEvent,
    ):
        """The first two occurrences of the period are included, the rest are overage.

        Chronological *here* only because this is a single sweep, which sorts its
        new identities by ``occurrence_start`` before assigning positions. Allowance
        is consumed in **insertion order** in general — rows an earlier sweep
        recorded are counted, not re-ranked — so an occurrence back-dated into an
        already-swept period lands after everything recorded before it. Harmless
        while one price applies to a whole period; see ``MeteringService._record``
        for why this will need to change.
        """
        self._set_allowance(subscription, 2, "0.2500")

        metering_service.meter_occurrences_for_period(subscription, PERIOD_START, PERIOD_END)

        rows = list(
            MeteredOccurrence.objects.filter(subscription=subscription).order_by("occurrence_start")
        )
        assert [row.is_within_allowance for row in rows] == [True, True, False, False, False]
        assert [row.unit_price for row in rows] == [
            Decimal("0.0000"),
            Decimal("0.0000"),
            Decimal("0.2500"),
            Decimal("0.2500"),
            Decimal("0.2500"),
        ]

    def test_the_allowance_is_not_re_granted_by_an_overlapping_window(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        open_ended_weekly_event: CalendarEvent,
    ):
        """Metering in two overlapping passes must price identically to one pass.

        The subtle failure, and the reason the meter subtracts what it has already
        recorded before assigning the allowance: the second pass re-sees the
        occurrences the first pass recorded. Those are already *holding* allowance
        slots. If they were allowed to consume slots a second time, genuinely new
        occurrences would be pushed past the ceiling and the customer would be
        charged for usage that was included in the plan.

        The allowance here (5) is wide enough that the whole month fits, so any
        overage at all is the bug. The first pass covers two occurrences, the second
        covers all five — the three-occurrence overlap is what re-consumes.
        """
        self._set_allowance(subscription, 5, "0.1000")

        metering_service.meter_occurrences_for_period(
            subscription, PERIOD_START, datetime.datetime(2025, 6, 10, 12, 0, tzinfo=datetime.UTC)
        )
        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 2

        metering_service.meter_occurrences_for_period(subscription, PERIOD_START, PERIOD_END)

        rows = list(
            MeteredOccurrence.objects.filter(subscription=subscription).order_by("occurrence_start")
        )
        assert [row.is_within_allowance for row in rows] == [True] * 5
        assert sum(row.unit_price for row in rows) == Decimal("0.0000")

    def test_a_later_limit_change_does_not_reprice_already_metered_occurrences(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        open_ended_weekly_event: CalendarEvent,
    ):
        """The reason the price is a stamped column rather than a lookup."""
        self._set_allowance(subscription, 2, "0.2500")
        metering_service.meter_occurrences_for_period(subscription, PERIOD_START, PERIOD_END)
        priced_before = [
            (row.is_within_allowance, row.unit_price)
            for row in MeteredOccurrence.objects.filter(subscription=subscription).order_by(
                "occurrence_start"
            )
        ]

        # The organization is moved to a bigger allowance and a cheaper rate.
        self._set_allowance(subscription, 99, "0.0100")
        metering_service.meter_occurrences_for_period(subscription, PERIOD_START, PERIOD_END)

        priced_after = [
            (row.is_within_allowance, row.unit_price)
            for row in MeteredOccurrence.objects.filter(subscription=subscription).order_by(
                "occurrence_start"
            )
        ]
        assert priced_after == priced_before

    def test_a_missing_overage_price_records_zero_rather_than_failing(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        open_ended_weekly_event: CalendarEvent,
    ):
        """A catalog gap must not stop usage being *recorded*.

        Losing the record of an occurrence is unrecoverable; recording it at a price
        of zero is a visible, fixable under-charge.
        """
        self._set_allowance(subscription, 1, None)

        metering_service.meter_occurrences_for_period(subscription, PERIOD_START, PERIOD_END)

        rows = MeteredOccurrence.objects.filter(subscription=subscription)
        assert rows.count() == 5
        assert rows.filter(is_within_allowance=False).count() == 4
        assert {row.unit_price for row in rows} == {Decimal("0.0000")}


@pytest.mark.django_db
class TestUsageCounterReadsTheMeter:
    """The counter and the meter must not be two opinions.

    Every test here pins "now" with ``freeze_time``. That is not incidental: the
    counter's billing period is derived from ``timezone.now()``, so a test that let
    the wall clock decide would be asserting against whichever cycle the suite
    happened to run in. It is also the bug these tests previously hid — see
    ``test_the_counter_still_reads_the_meter_when_the_stored_period_is_stale``.
    """

    def test_event_occurrences_usage_is_the_metered_row_count(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        organization: Organization,
        open_ended_weekly_event: CalendarEvent,
    ):
        entitlement_service = EntitlementService()
        with freeze_time(INSIDE_PERIOD):
            assert (
                entitlement_service.get_current_usage(
                    organization, LimitedResource.EVENT_OCCURRENCES
                )
                == 0
            )

            metering_service.meter_occurrences_for_period(subscription, PERIOD_START, PERIOD_END)

            assert (
                entitlement_service.get_current_usage(
                    organization, LimitedResource.EVENT_OCCURRENCES
                )
                == 5
            )

    def test_the_counter_still_reads_the_meter_when_the_stored_period_is_stale(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        organization: Organization,
        calendar: Calendar,
    ):
        """The counter and the meter must derive "this period" the same way.

        ``Subscription.current_period_start`` is written once at creation and never
        advanced — cycle close is not implemented yet — so it goes stale the moment
        one billing interval elapses. The meter has never depended on it: it stamps
        ``billing_period_start`` by resolving each occurrence's *own* start time
        through ``resolve_billing_period``, which steps whole intervals from the
        stored anchor and therefore keeps working. The counter used to read the
        stale column directly.

        The consequence was total, not marginal: once the stored period elapsed the
        meter wrote (say) August rows while the counter asked for July, and the
        counter returned **0 permanently** — a customer-facing usage number frozen
        at zero, and a post-paid limit check that could never fire.

        Here the subscription's stored period is June 2025 while "now" is two months
        later. Occurrences are metered in that later month; the counter must see
        them. Against the old implementation this returns 0.
        """
        stale_period_start = PERIOD_START + relativedelta(months=2)
        stale_period_end = PERIOD_END + relativedelta(months=2)
        assert subscription.current_period_start == PERIOD_START, (
            "fixture precondition: the stored period is the June cycle"
        )

        occurrence_moment = stale_period_start + datetime.timedelta(days=3, hours=10)
        CalendarEventFactory.create_recurring_event(
            calendar=calendar,
            title="Two cycles later",
            description="",
            start_time=occurrence_moment,
            end_time=occurrence_moment + datetime.timedelta(hours=1),
            frequency=RecurrenceFrequency.WEEKLY,
            count=1,
            external_id="stale_period_event",
        )

        metering_service.meter_occurrences_for_period(
            subscription, stale_period_start, stale_period_end
        )

        row = MeteredOccurrence.objects.get(subscription=subscription)
        assert row.billing_period_start == stale_period_start, (
            "the meter stamps the period the occurrence fell in, not the stored one"
        )

        with freeze_time(occurrence_moment):
            assert (
                EntitlementService().get_current_usage(
                    organization, LimitedResource.EVENT_OCCURRENCES
                )
                == 1
            )

    def test_usage_is_scoped_to_the_current_billing_period(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        organization: Organization,
        calendar: Calendar,
    ):
        """An occurrence in the previous cycle is metered, but is not *current* usage.

        Post-paid usage resets each cycle; a counter that summed the whole ledger
        would report a number that only ever grows.
        """
        previous_cycle_moment = PERIOD_START - datetime.timedelta(days=10)
        CalendarEventFactory.create_recurring_event(
            calendar=calendar,
            title="Last month",
            description="",
            start_time=previous_cycle_moment,
            end_time=previous_cycle_moment + datetime.timedelta(hours=1),
            frequency=RecurrenceFrequency.WEEKLY,
            count=1,
            external_id="previous_cycle_event",
        )

        metering_service.meter_occurrences_for_period(
            subscription, previous_cycle_moment - datetime.timedelta(days=1), PERIOD_START
        )

        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 1
        assert (
            MeteredOccurrence.objects.get(subscription=subscription).billing_period_start
            < PERIOD_START
        )
        with freeze_time(INSIDE_PERIOD):
            assert (
                EntitlementService().get_current_usage(
                    organization, LimitedResource.EVENT_OCCURRENCES
                )
                == 0
            )


@pytest.mark.django_db
class TestOneOffEvents:
    def test_a_non_recurring_event_is_metered_exactly_once(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        calendar: Calendar,
    ):
        event = CalendarEvent.objects.create(
            calendar_fk=calendar,
            organization=calendar.organization,
            title="One off",
            description="",
            # Aware values, matching `CalendarEventFactory`'s own convention for
            # these fields (the `_tz_unaware` name describes storage, not the input).
            start_time_tz_unaware=FIRST_MONDAY,
            end_time_tz_unaware=FIRST_MONDAY + datetime.timedelta(hours=1),
            timezone="UTC",
            external_id="one_off_metering",
        )

        metering_service.meter_occurrences_for_period(subscription, PERIOD_START, PERIOD_END)
        metering_service.meter_occurrences_for_period(subscription, PERIOD_START, PERIOD_END)

        assert _metered_keys(subscription) == {(event.pk, FIRST_MONDAY)}


@pytest.mark.django_db
class TestRecurrenceInstanceRowsAreNotEnumeratedSeparately:
    """A stored row that represents an occurrence of a series is not a second
    occurrence.

    The REST serializer accepts ``parent_recurring_object_id`` with
    ``is_recurring_exception`` left at its default ``False``
    (``calendar_integration/serializers.py``), so a client can persist a row that
    hangs off a series without being registered as an exception. Its master's rule
    still generates the slot, so enumerating the row *and* expanding the master
    would bill that slot twice — under two different event ids, which the unique
    constraint cannot catch.

    ``occurrence_bearing_masters_in_range`` excludes anything with a
    ``parent_recurring_object`` for exactly this reason.
    """

    def test_a_stored_instance_of_a_series_is_metered_once(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        calendar: Calendar,
        open_ended_weekly_event: CalendarEvent,
    ):
        second_monday = FIRST_MONDAY + datetime.timedelta(weeks=1)
        CalendarEvent.objects.create(
            calendar_fk=calendar,
            organization=calendar.organization,
            title="Stored instance",
            description="",
            start_time_tz_unaware=second_monday,
            end_time_tz_unaware=second_monday + datetime.timedelta(hours=1),
            timezone="UTC",
            external_id="stored_instance_metering",
            parent_recurring_object_fk=open_ended_weekly_event,
            recurrence_id=second_monday,
            is_recurring_exception=False,
        )

        metering_service.meter_occurrences_for_period(subscription, PERIOD_START, PERIOD_END)

        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 5
        assert (
            MeteredOccurrence.objects.filter(
                subscription=subscription, occurrence_start=second_monday
            ).count()
            == 1
        )


@pytest.mark.django_db
class TestBillingPeriodBoundary:
    """A window that straddles a cycle boundary restarts the allowance.

    ``_record`` keys its allowance arithmetic on a ``dict`` of billing period
    rather than a single running counter, specifically so occurrences either side
    of a boundary consume *different* allowances. Nothing exercised that branch:
    every other test meters wholly inside one cycle, so the dict never held more
    than one key and a single counter would have passed identically.
    """

    def test_a_straddling_window_splits_into_two_periods_with_independent_allowances(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        calendar: Calendar,
    ):
        """Meter ``[PERIOD_START - 3h, PERIOD_START + 3h)``.

        Two occurrences, one on each side of the June boundary, with an allowance
        of exactly 1. If the allowance were global to the call, the second would
        fall into overage. Because it belongs to the next cycle it gets that
        cycle's fresh allowance, and both are included at zero.
        """
        subscription.limits.filter(resource_key=LimitedResource.EVENT_OCCURRENCES).update(
            limit_value=1, overage_unit_price=Decimal("0.5000")
        )
        previous_period_start = PERIOD_START - relativedelta(months=1)

        before_boundary = PERIOD_START - datetime.timedelta(hours=2)
        after_boundary = PERIOD_START + datetime.timedelta(hours=2)
        for label, moment in (("before", before_boundary), ("after", after_boundary)):
            CalendarEvent.objects.create(
                calendar_fk=calendar,
                organization=calendar.organization,
                title=f"Straddle {label}",
                description="",
                start_time_tz_unaware=moment,
                end_time_tz_unaware=moment + datetime.timedelta(minutes=30),
                timezone="UTC",
                external_id=f"straddle_{label}",
            )

        result = metering_service.meter_occurrences_for_period(
            subscription,
            PERIOD_START - datetime.timedelta(hours=3),
            PERIOD_START + datetime.timedelta(hours=3),
        )

        assert result.occurrences_recorded == 2
        rows = list(
            MeteredOccurrence.objects.filter(subscription=subscription).order_by("occurrence_start")
        )
        assert [row.billing_period_start for row in rows] == [
            previous_period_start,
            PERIOD_START,
        ], "the two occurrences must be stamped to two different cycles"
        assert [row.is_within_allowance for row in rows] == [True, True], (
            "each cycle grants its own allowance of 1; neither occurrence is overage"
        )
        assert [row.unit_price for row in rows] == [Decimal("0.0000"), Decimal("0.0000")]

    def test_the_second_occurrence_in_one_period_is_overage(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        calendar: Calendar,
    ):
        """The control for the test above.

        Same allowance of 1 and same two occurrences, but both placed *inside* June
        — so they share one cycle's allowance and the second is overage. Without
        this, the test above would pass just as well if `billing_period_start` were
        ignored entirely and everything were always within allowance.
        """
        subscription.limits.filter(resource_key=LimitedResource.EVENT_OCCURRENCES).update(
            limit_value=1, overage_unit_price=Decimal("0.5000")
        )
        for label, offset in (("first", 2), ("second", 4)):
            moment = PERIOD_START + datetime.timedelta(hours=offset)
            CalendarEvent.objects.create(
                calendar_fk=calendar,
                organization=calendar.organization,
                title=f"Same period {label}",
                description="",
                start_time_tz_unaware=moment,
                end_time_tz_unaware=moment + datetime.timedelta(minutes=30),
                timezone="UTC",
                external_id=f"same_period_{label}",
            )

        metering_service.meter_occurrences_for_period(
            subscription, PERIOD_START, PERIOD_START + datetime.timedelta(hours=6)
        )

        rows = list(
            MeteredOccurrence.objects.filter(subscription=subscription).order_by("occurrence_start")
        )
        assert {row.billing_period_start for row in rows} == {PERIOD_START}
        assert [row.is_within_allowance for row in rows] == [True, False]
        assert [row.unit_price for row in rows] == [Decimal("0.0000"), Decimal("0.5000")]


@pytest.mark.django_db
class TestSafetyNets:
    """The guards that only fire on corrupt data.

    Each of these exists because the data it defends against is user-mutable and
    the failure mode without it is a hang or a wrong invoice rather than an error.
    None of them was exercised, which is the same class of gap as a unique
    constraint that is never actually hit by a test: the mechanism is asserted
    only where it does nothing.
    """

    def test_a_cycle_in_the_bulk_modification_chain_terminates(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        calendar: Calendar,
    ):
        """``bulk_modification_parent`` is ordinary mutable data; a cycle must not hang.

        Two events pointing at each other is not reachable through the service
        layer, but it is reachable through the admin or a bad migration. The walk
        in ``_resolve_series_root_ids`` carries a ``seen`` set precisely for this;
        without it the ``while`` loop never terminates and the sweep wedges.
        """
        first = CalendarEvent.objects.create(
            calendar_fk=calendar,
            organization=calendar.organization,
            title="Cycle A",
            description="",
            start_time_tz_unaware=FIRST_MONDAY,
            end_time_tz_unaware=FIRST_MONDAY + datetime.timedelta(hours=1),
            timezone="UTC",
            external_id="cycle_a",
        )
        second_moment = FIRST_MONDAY + datetime.timedelta(days=1)
        second = CalendarEvent.objects.create(
            calendar_fk=calendar,
            organization=calendar.organization,
            title="Cycle B",
            description="",
            start_time_tz_unaware=second_moment,
            end_time_tz_unaware=second_moment + datetime.timedelta(hours=1),
            timezone="UTC",
            external_id="cycle_b",
        )
        # The cycle: each is the other's bulk-modification parent.
        CalendarEvent.objects.filter(pk=first.pk).update(bulk_modification_parent_fk=second)
        CalendarEvent.objects.filter(pk=second.pk).update(bulk_modification_parent_fk=first)

        result = metering_service.meter_occurrences_for_period(
            subscription, PERIOD_START, PERIOD_END
        )

        # It terminates, and both occurrences are still recorded — the guard falls
        # back to a reachable ancestor rather than dropping usage.
        assert result.occurrences_recorded == 2
        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 2

    def test_a_chain_longer_than_the_depth_bound_still_records_every_occurrence(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        calendar: Calendar,
    ):
        """Exceeding ``MAX_SERIES_CHAIN_DEPTH`` must over-count, never lose a record.

        The documented fallback is "the deepest ancestor reached, which over-counts
        at worst and never loses a record". A chain longer than the bound is built
        here explicitly; the assertion is that every occurrence still produces a
        row, because losing a billable record is unrecoverable while an
        attribution that stops short is merely wrong in a visible way.
        """
        chain: list[CalendarEvent] = []
        for index in range(MAX_SERIES_CHAIN_DEPTH + 5):
            moment = FIRST_MONDAY + datetime.timedelta(minutes=index)
            chain.append(
                CalendarEvent.objects.create(
                    calendar_fk=calendar,
                    organization=calendar.organization,
                    title=f"Chain {index}",
                    description="",
                    start_time_tz_unaware=moment,
                    end_time_tz_unaware=moment + datetime.timedelta(seconds=30),
                    timezone="UTC",
                    external_id=f"chain_{index}",
                )
            )
        for index in range(1, len(chain)):
            CalendarEvent.objects.filter(pk=chain[index].pk).update(
                bulk_modification_parent_fk=chain[index - 1]
            )

        result = metering_service.meter_occurrences_for_period(
            subscription, PERIOD_START, PERIOD_END
        )

        assert result.occurrences_recorded == len(chain), (
            "every occurrence is recorded even when root resolution gives up early"
        )

    def test_an_inverted_stored_period_self_corrects_rather_than_raising(
        self,
        subscription: Subscription,
    ):
        """Documenting what the guard does *not* catch.

        ``BillingPeriodResolutionError``'s own docstring offers
        ``current_period_end <= current_period_start`` as the likely trigger. It is
        not one: the backwards branch reassigns ``end, start = start, start - step``,
        which repairs the inversion on the first iteration and then terminates
        normally. Asserted so nobody "fixes" a guard that is already working by
        chasing a case that cannot reach it.
        """
        Subscription.objects.filter(pk=subscription.pk).update(
            current_period_start=PERIOD_END, current_period_end=PERIOD_START
        )
        subscription.refresh_from_db()

        assert resolve_billing_period(subscription, FIRST_MONDAY) == (PERIOD_START, PERIOD_END)

    def test_an_unreachably_distant_moment_raises_rather_than_spinning(
        self,
        subscription: Subscription,
    ):
        """The case the step bound actually defends against.

        ``resolve_billing_period`` reconstructs neighbouring cycles by stepping
        whole intervals from the stored anchor, one iteration per interval. A
        moment thousands of intervals away — an event scheduled centuries out,
        whether by a client bug or a hostile input — would otherwise spin through
        that loop inside the sweep's transaction, holding the billing root's row
        lock. ``MAX_BILLING_PERIOD_STEPS`` converts that into a fast, loud failure.

        Raising is right rather than clamping: stamping ``billing_period_start``
        with whichever cycle the loop stopped on bills real usage to the wrong
        invoice, and is invisible until a customer disputes it.
        """
        far_future = PERIOD_START + relativedelta(years=200)

        with pytest.raises(BillingPeriodResolutionError) as exc_info:
            resolve_billing_period(subscription, far_future)

        assert exc_info.value.subscription_id == subscription.pk

    def test_a_metering_failure_leaves_no_partially_stamped_ledger(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        calendar: Calendar,
    ):
        """A period that cannot be resolved must abort the whole sweep.

        ``meter_occurrences_for_period`` stamps rows one occurrence at a time
        inside a single ``transaction.atomic``. If resolving the period for the
        *second* occurrence raises, the first must not survive — a half-written
        sweep is worse than no sweep, because the missing half looks metered to the
        next run's pre-filter and is never revisited.
        """
        for label, moment in (
            ("normal", FIRST_MONDAY),
            ("distant", PERIOD_START + relativedelta(years=200)),
        ):
            CalendarEvent.objects.create(
                calendar_fk=calendar,
                organization=calendar.organization,
                title=f"Partial {label}",
                description="",
                start_time_tz_unaware=moment,
                end_time_tz_unaware=moment + datetime.timedelta(hours=1),
                timezone="UTC",
                external_id=f"partial_{label}",
            )

        with pytest.raises(BillingPeriodResolutionError):
            metering_service.meter_occurrences_for_period(
                subscription, PERIOD_START, PERIOD_START + relativedelta(years=201)
            )

        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 0
