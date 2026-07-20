"""Phase 8: the post-paid ``event_occurrences`` allowance guard.

Spec use-case 4 (enforcement half): an organization **with** a payment method
accrues past its included allowance and is never interrupted; one **without** a
payment method is blocked at the allowance.

These tests drive the real guarded path (``CalendarService.create_event`` /
``EntitlementService.check_postpaid_allowance`` / ``has_payment_method``) rather
than asserting on a hand-built queryset -- a guard's own test would otherwise pass
whether or not the invariant holds.
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
from payments.exceptions import OverLimitError
from payments.models import BillingPlan, MeteredOccurrence, Subscription, SubscriptionPlanLimit
from payments.services.entitlement_service import EntitlementService


# This module builds its own Subscription rows (OneToOne with Organization), so it
# opts out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription


def _organization_with_postpaid_limit(
    limit_value: int | None, billing_state: str = BillingState.FREE
) -> tuple[Organization, Subscription]:
    """A standalone (non-reseller) organization with a ceiling on ``event_occurrences``.

    ``limit_value=None`` builds an ``unlimited``-shaped subscription (NULL ceiling) --
    every organization's actual state for the whole rollout, and the property this
    phase's tests must prove is inert.
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


@pytest.mark.django_db
class TestHasPaymentMethod:
    def test_free_billing_state_has_no_payment_method(self):
        organization, _subscription = _organization_with_postpaid_limit(1, BillingState.FREE)
        assert EntitlementService().has_payment_method(organization) is False

    @pytest.mark.parametrize(
        "billing_state",
        [BillingState.ACTIVE, BillingState.GRACE, BillingState.RESTRICTED, BillingState.CANCELLED],
    )
    def test_any_non_free_billing_state_has_a_payment_method(self, billing_state):
        organization, _subscription = _organization_with_postpaid_limit(1, billing_state)
        assert EntitlementService().has_payment_method(organization) is True

    def test_no_subscription_has_no_payment_method(self):
        """Fail-closed side of postpaid enforcement: an unresolvable subscription
        must not be read as "has a payment method"."""
        organization = baker.make(Organization, parent=None, can_invite_organizations=False)
        assert EntitlementService().has_payment_method(organization) is False


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
        organization, subscription = _organization_with_postpaid_limit(5, BillingState.ACTIVE)
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

    @pytest.mark.parametrize("billing_state", [BillingState.GRACE, BillingState.RESTRICTED])
    def test_at_the_allowance_in_grace_or_restricted_still_accrues(self, billing_state):
        """Grace/restricted already have a payment method on file (see
        ``has_payment_method``) -- their payment problem is resolved through the
        separate grace/restricted machinery (Phase 10/11), not by this guard, so
        they are treated the same as an active organization: let through, not
        blocked."""
        organization, subscription = _organization_with_postpaid_limit(5, billing_state)
        _seed_metered_occurrences(organization, subscription, 5)

        result = EntitlementService().check_postpaid_allowance(organization, delta=1)

        assert result.allowed is True

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
        organization, subscription = _organization_with_postpaid_limit(1, BillingState.ACTIVE)
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
        _seed_metered_occurrences(organization, subscription, 10_000)
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
