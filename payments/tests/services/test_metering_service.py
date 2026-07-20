"""Unit tests for ``MeteringService`` — the meter behind post-paid event billing.

Every assertion here is about a number that ends up on an invoice, so the tests
are written to fail loudly on *silence*: the failure mode this phase exists to
prevent produces no exception, no log line, and no red test — just a wrong count.

Three properties are load-bearing and each has a test that fails if the mechanism
behind it is removed rather than merely if the code changes shape:

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

from calendar_integration.constants import CalendarProvider, RecurrenceFrequency
from calendar_integration.factories import CalendarEventFactory
from calendar_integration.models import Calendar, CalendarEvent
from organizations.models import Organization
from payments.billing_constants import LimitedResource
from payments.models import MeteredOccurrence, Subscription
from payments.services.entitlement_service import EntitlementService
from payments.services.metering_service import MeteringService


#: A whole calendar month used as the subscription's billing period, chosen so the
#: weekly series below lands on five Mondays (2, 9, 16, 23, 30 June 2025). Fixed
#: dates rather than offsets from ``now`` so the expected counts in these tests are
#: arithmetic a reader can check by hand.
PERIOD_START = datetime.datetime(2025, 6, 1, 0, 0, tzinfo=datetime.UTC)
PERIOD_END = datetime.datetime(2025, 7, 1, 0, 0, tzinfo=datetime.UTC)
FIRST_MONDAY = datetime.datetime(2025, 6, 2, 10, 0, tzinfo=datetime.UTC)


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
        """Allowance is consumed chronologically: the first two are included."""
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
    """The counter and the meter must not be two opinions."""

    def test_event_occurrences_usage_is_the_metered_row_count(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        organization: Organization,
        open_ended_weekly_event: CalendarEvent,
    ):
        entitlement_service = EntitlementService()
        assert (
            entitlement_service.get_current_usage(organization, LimitedResource.EVENT_OCCURRENCES)
            == 0
        )

        metering_service.meter_occurrences_for_period(subscription, PERIOD_START, PERIOD_END)

        assert (
            entitlement_service.get_current_usage(organization, LimitedResource.EVENT_OCCURRENCES)
            == 5
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
        assert (
            EntitlementService().get_current_usage(organization, LimitedResource.EVENT_OCCURRENCES)
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
