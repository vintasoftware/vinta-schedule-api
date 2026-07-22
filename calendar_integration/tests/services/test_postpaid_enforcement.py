"""The post-paid ``event_occurrences`` allowance check.

Enforcement half: an organization **with** a payment method accrues past its
included allowance and is never interrupted; one **without** a payment method is
blocked at the allowance.

These tests drive the real enforcement path (``CalendarService.create_event`` /
``EntitlementService.check_postpaid_allowance`` / ``has_payment_method``) rather
than asserting on a hand-built queryset -- a check tested only against itself
would otherwise pass whether or not the rule holds.
"""

import datetime
from decimal import Decimal

from django.utils import timezone

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import Calendar, CalendarEvent
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import CalendarEventInputData
from organizations.models import Organization
from payments.billing_constants import BillingState, LimitedResource, LimitKind, LimitRemedy
from payments.constants import PaymentProviders
from payments.exceptions import OverLimitError
from payments.models import (
    BillingPlan,
    MeteredOccurrence,
    PaymentMethod,
    Subscription,
    SubscriptionPlanLimit,
)
from payments.services.entitlement_service import EntitlementService


# This module builds its own Subscription rows (OneToOne with Organization), so it
# opts out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription


def _organization_with_postpaid_limit(
    limit_value: int | None, billing_state: str = BillingState.FREE
) -> tuple[Organization, Subscription]:
    """A standalone (non-reseller) organization with a ceiling on ``event_occurrences``.

    ``limit_value=None`` builds an ``unlimited``-shaped subscription (NULL ceiling) --
    every organization's actual state for the whole rollout, and the property these
    tests must prove is inert.
    """
    organization = baker.make(Organization, parent=None, can_invite_organizations=False)
    now = timezone.now()
    subscription = baker.make(
        Subscription,
        organization=organization,
        plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
        billing_state=billing_state,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )
    baker.make(
        SubscriptionPlanLimit,
        subscription=subscription,
        resource_key=LimitedResource.EVENT_OCCURRENCES,
        limit_value=limit_value,
        kind=LimitKind.POSTPAID,
    )
    return organization, subscription


def _seed_metered_occurrences(organization: Organization, subscription: Subscription, count: int):
    """Write ``count`` already-metered occurrences into the subscription's current
    billing period -- what ``_count_event_occurrences`` reads back as usage."""
    MeteredOccurrence.objects.bulk_create(
        [
            MeteredOccurrence(
                organization=organization,
                subscription=subscription,
                event_id=900000 + i,
                occurrence_start=subscription.current_period_start + datetime.timedelta(hours=i),
                billing_period_start=subscription.current_period_start,
                is_within_allowance=True,
                unit_price=Decimal("0"),
            )
            for i in range(count)
        ]
    )


def _attach_payment_method(organization: Organization, is_active: bool = True) -> PaymentMethod:
    """A confirmed, chargeable instrument on file for ``organization`` -- the real
    record ``has_payment_method`` reads, replacing the old ``billing_state``
    allow-list proxy this module used to drive through
    ``_organization_with_postpaid_limit``'s ``billing_state`` parameter."""
    return baker.make(
        PaymentMethod,
        organization=organization,
        provider=PaymentProviders.MERCADOPAGO,
        external_id="pm-test-token",
        is_active=is_active,
    )


def _bookable_calendar(organization: Organization) -> Calendar:
    """A calendar any unauthenticated caller may book on, with no adapter round-trip.

    ``accepts_public_scheduling=True`` grants the booking permission check
    unconditionally (mirrors the codeless public-scheduling flows elsewhere), and
    ``manage_available_windows=False`` (the model default) makes the whole range
    bookable with no ``AvailableTime`` rows to seed. ``provider=INTERNAL`` (the
    model default) means no entitlement gate and no external adapter is resolved.
    """
    return baker.make(
        Calendar,
        organization=organization,
        calendar_type=CalendarType.PERSONAL,
        provider=CalendarProvider.INTERNAL,
        accepts_public_scheduling=True,
        manage_available_windows=False,
    )


def _event_input(start: datetime.datetime, minutes: int = 60) -> CalendarEventInputData:
    return CalendarEventInputData(
        title="Postpaid event",
        description="",
        start_time=start,
        end_time=start + datetime.timedelta(minutes=minutes),
        timezone="UTC",
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )


def _service_for(organization: Organization) -> CalendarService:
    service = CalendarService()
    service.initialize_without_provider(organization=organization)
    return service


# ----------------------------------------------------------------------------------
# EntitlementService.has_payment_method / check_postpaid_allowance -- direct unit tests
# ----------------------------------------------------------------------------------


#: Every ``BillingState``. ``has_payment_method`` used to be keyed off
#: ``billing_state`` alone, because ``billing_state`` *was* the proxy answer. It
#: now reads the real ``PaymentMethod`` record and the proxy is gone, so the
#: meaningful question is no longer "state -> answer" (a card can be present or
#: absent on *any* state) -- it is "does presence/absence of a real row still
#: decide it correctly regardless of state", exercised below by
#: ``TestHasPaymentMethod`` and
#: ``TestCheckPostpaidAllowance.test_accrual_past_the_allowance_follows_the_payment_method_record``.
#: Kept as the enumeration of every state this module's fixtures must cover.
ALL_BILLING_STATES = list(BillingState)

#: ``ALL_BILLING_STATES`` minus ``RESTRICTED`` -- ``RESTRICTED`` blocks
#: unconditionally, ahead of the payment-method check, so it is no longer true
#: that a payment method lets accrual through "regardless of billing state". See
#: ``TestCheckPostpaidAllowance.test_restricted_blocks_even_with_a_payment_method``
#: for the ``RESTRICTED``-specific behavior this excludes.
NON_RESTRICTED_BILLING_STATES = [
    state for state in ALL_BILLING_STATES if state != BillingState.RESTRICTED
]


@pytest.mark.django_db
class TestHasPaymentMethod:
    """``has_payment_method`` reads the real ``PaymentMethod`` record, not
    ``Subscription.billing_state``. These tests replace the old state -> answer
    table, which pinned the dead proxy rather than the real source of truth.
    """

    @pytest.mark.parametrize(
        "billing_state",
        ALL_BILLING_STATES,
        ids=[state.value for state in ALL_BILLING_STATES],
    )
    def test_active_payment_method_is_true_regardless_of_billing_state(self, billing_state):
        """The key case the old proxy could never express: a card can be
        on file while the subscription sits in *any* billing state -- most notably
        ``GRACE``, which the old allow-list had to hard-pin ``False`` even when the
        organization's card was perfectly fine and the very next retry would
        succeed."""
        organization, _subscription = _organization_with_postpaid_limit(1, billing_state)
        _attach_payment_method(organization)

        assert EntitlementService().has_payment_method(organization) is True

    @pytest.mark.parametrize(
        "billing_state",
        ALL_BILLING_STATES,
        ids=[state.value for state in ALL_BILLING_STATES],
    )
    def test_no_payment_method_is_false_regardless_of_billing_state(self, billing_state):
        """An organization can be ``ACTIVE`` from a past cycle with no *current*
        instrument on file (e.g. an admin removed it) -- ``billing_state`` alone
        must not manufacture a ``True`` answer."""
        organization, _subscription = _organization_with_postpaid_limit(1, billing_state)

        assert EntitlementService().has_payment_method(organization) is False

    def test_inactive_payment_method_is_false(self):
        """A deactivated instrument (e.g. removed/replaced) does not count, even
        though a row for it still exists."""
        organization, _subscription = _organization_with_postpaid_limit(1, BillingState.ACTIVE)
        _attach_payment_method(organization, is_active=False)

        assert EntitlementService().has_payment_method(organization) is False

    def test_no_subscription_has_no_payment_method(self):
        """Nothing to charge, so ``False``. (On the post-paid path this rarely
        decides anything: a subscription-less pool resolves to an unlimited ceiling
        and returns before this is consulted.)"""
        organization = baker.make(Organization, parent=None, can_invite_organizations=False)
        assert EntitlementService().has_payment_method(organization) is False

    def test_payment_method_of_a_different_organization_does_not_count(self):
        """Tenant isolation on the new record: org A's card must never answer for
        org B."""
        organization_a, _sub_a = _organization_with_postpaid_limit(1, BillingState.FREE)
        organization_b, _sub_b = _organization_with_postpaid_limit(1, BillingState.FREE)
        _attach_payment_method(organization_b)

        assert EntitlementService().has_payment_method(organization_a) is False
        assert EntitlementService().has_payment_method(organization_b) is True


@pytest.mark.django_db
class TestCheckPostpaidAllowance:
    def test_unlimited_never_counts_usage_or_blocks(self):
        organization, _subscription = _organization_with_postpaid_limit(None, BillingState.FREE)
        service = EntitlementService()

        result = service.check_postpaid_allowance(organization, delta=1000)

        assert result.allowed is True
        assert result.current_usage is None
        assert result.ceiling is None

    def test_under_the_allowance_is_allowed_without_a_payment_method(self):
        organization, subscription = _organization_with_postpaid_limit(5, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 3)

        result = EntitlementService().check_postpaid_allowance(organization, delta=1)

        assert result.allowed is True
        assert result.current_usage == 3
        assert result.ceiling == 5

    def test_at_the_allowance_with_a_payment_method_accrues(self):
        organization, subscription = _organization_with_postpaid_limit(5, BillingState.FREE)
        _attach_payment_method(organization)
        _seed_metered_occurrences(organization, subscription, 5)

        result = EntitlementService().check_postpaid_allowance(organization, delta=1)

        assert result.allowed is True

    def test_at_the_allowance_without_a_payment_method_blocks(self):
        organization, subscription = _organization_with_postpaid_limit(5, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 5)

        result = EntitlementService().check_postpaid_allowance(organization, delta=1)

        assert result.allowed is False
        assert result.current_usage == 5
        assert result.ceiling == 5
        assert result.remedy == LimitRemedy.ADD_PAYMENT_METHOD

    @pytest.mark.parametrize(
        "billing_state",
        NON_RESTRICTED_BILLING_STATES,
        ids=[state.value for state in NON_RESTRICTED_BILLING_STATES],
    )
    def test_accrual_past_the_allowance_follows_the_payment_method_record(self, billing_state):
        """Who may accrue past the allowance is exactly ``has_payment_method`` --
        which is now a real ``PaymentMethod`` row, not ``billing_state``. ``GRACE``
        is the important case: an organization whose card is still on file must
        accrue even while ``GRACE`` (a failed *charge* moves ``ACTIVE -> GRACE``,
        not the card being removed), proven here for every non-``RESTRICTED``
        state. ``RESTRICTED`` is excluded -- it blocks unconditionally regardless
        of payment method, see ``test_restricted_blocks_even_with_a_payment_method``.
        """
        organization, subscription = _organization_with_postpaid_limit(5, billing_state)
        _attach_payment_method(organization)
        _seed_metered_occurrences(organization, subscription, 5)

        result = EntitlementService().check_postpaid_allowance(organization, delta=1)

        assert result.allowed is True

    def test_restricted_blocks_even_with_a_payment_method(self):
        """Unlike every other billing state, ``RESTRICTED`` blocks event creation
        outright -- a payment method on file does not lift it, because the block
        is not about capacity or ability to pay; it is a write block that only
        resolving the restriction lifts."""
        organization, subscription = _organization_with_postpaid_limit(5, BillingState.RESTRICTED)
        _attach_payment_method(organization)
        _seed_metered_occurrences(organization, subscription, 5)

        result = EntitlementService().check_postpaid_allowance(organization, delta=1)

        assert result.allowed is False
        assert result.remedy == LimitRemedy.RESOLVE_BILLING

    @pytest.mark.parametrize(
        "billing_state",
        ALL_BILLING_STATES,
        ids=[state.value for state in ALL_BILLING_STATES],
    )
    def test_accrual_past_the_allowance_blocks_with_no_payment_method(self, billing_state):
        organization, subscription = _organization_with_postpaid_limit(5, billing_state)
        _seed_metered_occurrences(organization, subscription, 5)

        result = EntitlementService().check_postpaid_allowance(organization, delta=1)

        assert result.allowed is False

    def test_delta_larger_than_headroom_blocks_without_a_payment_method(self):
        """A fan-out delta (e.g. a bundle event) is checked as one unit, not one
        check per row -- a delta that alone exceeds headroom must block even though
        a delta of 1 would not have."""
        organization, subscription = _organization_with_postpaid_limit(5, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 3)

        result = EntitlementService().check_postpaid_allowance(organization, delta=3)

        assert result.allowed is False


# ----------------------------------------------------------------------------------
# CalendarService.create_event -- the guarded creation path
# ----------------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateEventPostpaidGuard:
    def test_with_payment_method_creation_past_the_allowance_succeeds_and_accrues(self):
        organization, subscription = _organization_with_postpaid_limit(1, BillingState.FREE)
        _attach_payment_method(organization)
        _seed_metered_occurrences(organization, subscription, 1)
        calendar = _bookable_calendar(organization)
        service = _service_for(organization)

        # Anchored to the subscription's own period (not a hand-picked calendar date)
        # so metering below resolves to the same billing period the seeded usage sits
        # in, regardless of when the suite runs.
        start = subscription.current_period_start + datetime.timedelta(days=1)
        event = service.create_event(calendar.id, _event_input(start))

        assert event.pk is not None
        assert CalendarEvent.objects.filter(pk=event.pk).exists()

        # "Accrues" is proven, not assumed: metering this event now must record it
        # as overage (outside the 1-occurrence allowance), not silently for free.
        from di_core.containers import container

        result = container.metering_service().meter_occurrences_for_period(
            subscription,
            window_start=event.start_time - datetime.timedelta(minutes=1),
            window_end=event.end_time + datetime.timedelta(minutes=1),
        )
        assert result.occurrences_recorded == 1
        recorded = MeteredOccurrence.objects.get(organization=organization, event_id=event.pk)
        assert recorded.is_within_allowance is False

    def test_without_payment_method_at_the_allowance_blocks(self):
        organization, subscription = _organization_with_postpaid_limit(1, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 1)
        calendar = _bookable_calendar(organization)
        service = _service_for(organization)
        start = subscription.current_period_start + datetime.timedelta(days=1)

        with pytest.raises(OverLimitError) as exc_info:
            service.create_event(calendar.id, _event_input(start))

        assert exc_info.value.resource_key == LimitedResource.EVENT_OCCURRENCES
        assert exc_info.value.remedy == LimitRemedy.ADD_PAYMENT_METHOD
        assert not CalendarEvent.objects.filter(calendar=calendar).exists()

    def test_without_payment_method_under_the_allowance_succeeds(self):
        organization, subscription = _organization_with_postpaid_limit(5, BillingState.FREE)
        _seed_metered_occurrences(organization, subscription, 2)
        calendar = _bookable_calendar(organization)
        service = _service_for(organization)
        start = subscription.current_period_start + datetime.timedelta(days=1)

        event = service.create_event(calendar.id, _event_input(start))

        assert event.pk is not None

    def test_unlimited_plan_is_unchanged_behavior(self):
        """The rollout's inertness guarantee: the ``unlimited`` plan's NULL ceiling
        means this guard can never block anybody today, with or without a payment
        method on file."""
        organization, subscription = _organization_with_postpaid_limit(None, BillingState.FREE)
        # One seeded row, not 10k: the unlimited branch returns before usage is ever
        # counted, so the count is never read. Ten thousand rows only added minutes of
        # bulk insert (enough to trip pytest-timeout under parallel contention) to
        # prove the same thing one row does.
        _seed_metered_occurrences(organization, subscription, 1)
        calendar = _bookable_calendar(organization)
        service = _service_for(organization)
        start = subscription.current_period_start + datetime.timedelta(days=1)

        event = service.create_event(calendar.id, _event_input(start))

        assert event.pk is not None

    def test_recurrence_exception_is_not_charged_a_second_unit(self):
        """A modified occurrence substitutes for a slot its master's rule already
        accounts for -- it must not independently consume headroom, or the guard
        would disagree with ``MeteringService.expand_occurrence_identities`` about
        what one booking costs (it never counts an exception as its own master).

        The master is seeded directly via the ORM (not through ``create_event``):
        the only thing this test drives through the guarded path is the exception
        create, and ``CalendarEvent.external_id`` is globally unique, so two calls
        through the un-adapted ``create_event`` path (which both stamp
        ``external_id=""``) would collide with each other regardless of billing.
        """
        organization, subscription = _organization_with_postpaid_limit(1, BillingState.FREE)
        calendar = _bookable_calendar(organization)
        service = _service_for(organization)
        start = subscription.current_period_start + datetime.timedelta(days=1)

        master = CalendarEvent.objects.create(
            calendar_fk=calendar,
            organization=organization,
            title="Master",
            start_time_tz_unaware=start,
            end_time_tz_unaware=start + datetime.timedelta(hours=1),
            timezone="UTC",
            external_id="postpaid-exception-master",
        )
        # At the allowance now (the seeded "usage" unit is what would prove blocking
        # if the exception path incorrectly re-ran the guard): the check here is
        # that a create against the same organization -- one that is an exception
        # of `master` -- does not consume a unit at all.
        _seed_metered_occurrences(organization, subscription, 1)

        exception_input = CalendarEventInputData(
            title="Rescheduled",
            description="",
            start_time=start + datetime.timedelta(days=1),
            end_time=start + datetime.timedelta(days=1, hours=1),
            timezone="UTC",
            parent_event_id=master.id,
            is_recurring_exception=True,
        )

        exception_event = service.create_event(calendar.id, exception_input)

        assert exception_event.pk is not None
