"""Unit tests for ``UsageWarningService`` (Phase 12) -- the proactive "you're
approaching a limit" / "you've reached a limit" push notifications.

The two things this module exists to pin, per the phase's own warning about
its recurring failure shape:

- **"Approaching" is defined exactly once**, at
  ``usage_warning_service.APPROACHING_LIMIT_THRESHOLD``, and read from the
  identical ``EntitlementService.get_effective_limit`` /
  ``get_current_usage`` methods the enforcement guards use -- there is no
  second ratio computed anywhere else in this module.
- **The debounce is durable, not an in-memory flag**: a warning fires at most
  once per ``(subscription, resource_key, billing_period_start, level)``,
  proven by calling ``check_subscription`` more than once against the same
  usage and asserting the notification count does not grow, and proven to
  reset only when the billing cycle actually rolls over.
"""

import datetime
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time
from model_bakery import baker

from organizations.models import Organization, OrganizationMembership, OrganizationRole
from payments.billing_constants import BillingState, LimitedResource, LimitKind, LimitWarningLevel
from payments.models import (
    BillingPlan,
    LimitWarningNotification,
    Subscription,
    SubscriptionPlanLimit,
)
from payments.services.entitlement_service import EntitlementService
from payments.services.usage_warning_service import UsageWarningService
from users.models import User


# This module builds its own Subscription rows (OneToOne with Organization), so it
# opts out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription

FREEZE_START = datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.UTC)


@pytest.fixture
def entitlement_service() -> EntitlementService:
    return EntitlementService()


@pytest.fixture
def mock_notification_service() -> MagicMock:
    return MagicMock()


@pytest.fixture
def service(entitlement_service, mock_notification_service) -> UsageWarningService:
    return UsageWarningService(
        entitlement_service=entitlement_service, notification_service=mock_notification_service
    )


@pytest.fixture
def organization() -> Organization:
    return baker.make(Organization, parent=None, can_invite_organizations=False)


@pytest.fixture
def subscription(organization) -> Subscription:
    return baker.make(
        Subscription,
        organization=organization,
        plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
        billing_state=BillingState.FREE,
        billing_interval="monthly",
        current_period_start=FREEZE_START,
        current_period_end=FREEZE_START + datetime.timedelta(days=30),
    )


def _make_limit(
    subscription: Subscription, resource_key: str, limit_value: int | None
) -> SubscriptionPlanLimit:
    return baker.make(
        SubscriptionPlanLimit,
        subscription=subscription,
        resource_key=resource_key,
        limit_value=limit_value,
        kind=LimitKind.PREPAID,
    )


def _seed_members(organization: Organization, count: int) -> None:
    for _ in range(count):
        baker.make(
            OrganizationMembership,
            organization=organization,
            user=baker.make(User),
            role=OrganizationRole.MEMBER,
            is_active=True,
        )


@pytest.fixture(autouse=True)
def admin_membership(organization: Organization) -> OrganizationMembership:
    """``UsageWarningService._recipient_user_ids`` (mirroring
    ``DunningService``) only notifies admins/billing owners
    (``OrganizationMembership.objects.billing_recipients``) -- without at
    least one, every "was a notification sent?" assertion in this module
    would pass vacuously regardless of whether the threshold/debounce logic
    is actually correct."""
    return baker.make(
        OrganizationMembership,
        organization=organization,
        user=baker.make(User),
        role=OrganizationRole.ADMIN,
        is_active=True,
    )


@pytest.mark.django_db
class TestApproachingThreshold:
    def test_below_threshold_does_not_warn(self, service, organization, subscription):
        _make_limit(subscription, LimitedResource.ORGANIZATION_MEMBERS, 10)
        # + the autouse admin membership = 4 of 10 (40%) -- well under 80%.
        _seed_members(organization, 3)

        with freeze_time(FREEZE_START):
            service.check_subscription(subscription)

        service.notification_service.create_notification.assert_not_called()
        assert not LimitWarningNotification.objects.filter(subscription=subscription).exists()

    def test_at_threshold_sends_exactly_one_approaching_warning(
        self, service, organization, subscription
    ):
        _make_limit(subscription, LimitedResource.ORGANIZATION_MEMBERS, 10)
        # + the autouse admin membership = 8 of 10, exactly 80%.
        _seed_members(organization, 7)

        with freeze_time(FREEZE_START):
            service.check_subscription(subscription)

        service.notification_service.create_notification.assert_called_once()
        call = service.notification_service.create_notification.call_args
        assert call.kwargs["context_name"] == "approaching_limit_context"
        assert call.kwargs["context_kwargs"]["resource_key"] == LimitedResource.ORGANIZATION_MEMBERS

        warning = LimitWarningNotification.objects.get(subscription=subscription)
        assert warning.level == LimitWarningLevel.APPROACHING
        assert warning.resource_key == LimitedResource.ORGANIZATION_MEMBERS

    def test_at_or_over_the_limit_sends_a_reached_warning_not_approaching(
        self, service, organization, subscription
    ):
        _make_limit(subscription, LimitedResource.ORGANIZATION_MEMBERS, 10)
        # + the autouse admin membership = 10 of 10, exactly 100%.
        _seed_members(organization, 9)

        with freeze_time(FREEZE_START):
            service.check_subscription(subscription)

        call = service.notification_service.create_notification.call_args
        assert call.kwargs["context_name"] == "limit_reached_context"
        warning = LimitWarningNotification.objects.get(subscription=subscription)
        assert warning.level == LimitWarningLevel.REACHED

    def test_unlimited_resource_never_warns(self, service, organization, subscription):
        _make_limit(subscription, LimitedResource.ORGANIZATION_MEMBERS, None)
        _seed_members(organization, 500)  # would be "over" any finite ceiling

        with freeze_time(FREEZE_START):
            service.check_subscription(subscription)

        service.notification_service.create_notification.assert_not_called()
        assert not LimitWarningNotification.objects.filter(subscription=subscription).exists()

    def test_zero_limit_with_no_usage_does_not_warn(self, service, organization, subscription):
        """A ``limit_value=0`` resource ("not included", per ``BillingPlan.clean``)
        that the organization has never touched has nothing to warn about."""
        _make_limit(subscription, LimitedResource.RESOURCE_CALENDARS, 0)

        with freeze_time(FREEZE_START):
            service.check_subscription(subscription)

        service.notification_service.create_notification.assert_not_called()

    def test_zero_limit_with_any_usage_reaches_immediately(
        self, service, organization, subscription
    ):
        from calendar_integration.constants import CalendarType
        from calendar_integration.models import Calendar

        _make_limit(subscription, LimitedResource.RESOURCE_CALENDARS, 0)
        baker.make(
            Calendar,
            organization=organization,
            calendar_type=CalendarType.RESOURCE,
            external_id="over-the-zero-limit",
        )

        with freeze_time(FREEZE_START):
            service.check_subscription(subscription)

        warning = LimitWarningNotification.objects.get(subscription=subscription)
        assert warning.level == LimitWarningLevel.REACHED


@pytest.mark.django_db
class TestDebounce:
    def test_repeated_checks_in_the_same_cycle_send_exactly_one_warning(
        self, service, organization, subscription
    ):
        _make_limit(subscription, LimitedResource.ORGANIZATION_MEMBERS, 10)
        _seed_members(organization, 7)  # + admin = 8 of 10 (80%)

        with freeze_time(FREEZE_START):
            service.check_subscription(subscription)
            service.check_subscription(subscription)
        with freeze_time(FREEZE_START + datetime.timedelta(hours=1)):
            service.check_subscription(subscription)

        assert service.notification_service.create_notification.call_count == 1
        assert LimitWarningNotification.objects.filter(subscription=subscription).count() == 1

    def test_crossing_a_second_threshold_in_the_same_cycle_warns_again_at_the_new_level(
        self, service, organization, subscription
    ):
        """Approaching (80%) and reached (100%) are debounced independently --
        both crossings in the same cycle must each notify exactly once."""
        _make_limit(subscription, LimitedResource.ORGANIZATION_MEMBERS, 10)
        _seed_members(organization, 7)  # + admin = 8 of 10 (80%)

        with freeze_time(FREEZE_START):
            service.check_subscription(subscription)
        assert service.notification_service.create_notification.call_count == 1

        _seed_members(organization, 2)  # now 10 of 10 (100%)
        with freeze_time(FREEZE_START + datetime.timedelta(hours=1)):
            service.check_subscription(subscription)

        assert service.notification_service.create_notification.call_count == 2
        levels = set(
            LimitWarningNotification.objects.filter(subscription=subscription).values_list(
                "level", flat=True
            )
        )
        assert levels == {LimitWarningLevel.APPROACHING, LimitWarningLevel.REACHED}

    def test_debounce_resets_on_the_next_billing_cycle(self, service, organization, subscription):
        """The marker is scoped to ``current_billing_period_start`` -- once the
        cycle rolls over, usage still above the threshold is allowed to warn
        again rather than being permanently silenced by a prior cycle's marker."""
        _make_limit(subscription, LimitedResource.ORGANIZATION_MEMBERS, 10)
        _seed_members(organization, 7)  # + admin = 8 of 10 (80%)

        with freeze_time(FREEZE_START):
            service.check_subscription(subscription)
        assert service.notification_service.create_notification.call_count == 1

        # A full billing interval (monthly) later: a new cycle, same usage.
        with freeze_time(FREEZE_START + datetime.timedelta(days=31)):
            service.check_subscription(subscription)

        assert service.notification_service.create_notification.call_count == 2


@pytest.mark.django_db
class TestAlreadyBlockedSubscriptionsAreSkipped:
    def test_restricted_subscription_is_never_warned(self, service, organization, subscription):
        _make_limit(subscription, LimitedResource.ORGANIZATION_MEMBERS, 10)
        _seed_members(organization, 10)
        subscription.billing_state = BillingState.RESTRICTED
        subscription.save(update_fields=["billing_state"])

        with freeze_time(FREEZE_START):
            service.check_subscription(subscription)

        service.notification_service.create_notification.assert_not_called()

    def test_cancelled_subscription_is_never_warned(self, service, organization, subscription):
        _make_limit(subscription, LimitedResource.ORGANIZATION_MEMBERS, 10)
        _seed_members(organization, 10)
        subscription.billing_state = BillingState.CANCELLED
        subscription.save(update_fields=["billing_state"])

        with freeze_time(FREEZE_START):
            service.check_subscription(subscription)

        service.notification_service.create_notification.assert_not_called()


@pytest.mark.django_db
class TestBestEffort:
    def test_a_failure_on_one_resource_does_not_stop_the_others(
        self, entitlement_service, organization, subscription
    ):
        """A notification-send failure on one resource must not break the
        sweep for the rest of this subscription's resources -- the beat task's
        own resilience contract."""
        _make_limit(subscription, LimitedResource.ORGANIZATION_MEMBERS, 10)
        _make_limit(subscription, LimitedResource.RESOURCE_CALENDARS, 10)
        _seed_members(organization, 8)

        from calendar_integration.constants import CalendarType
        from calendar_integration.models import Calendar

        for i in range(8):
            baker.make(
                Calendar,
                organization=organization,
                calendar_type=CalendarType.RESOURCE,
                external_id=f"cal-{i}",
            )

        flaky_notification_service = MagicMock()
        call_count = {"n": 0}

        def _raise_once_then_succeed(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated notification-send failure")

        flaky_notification_service.create_notification.side_effect = _raise_once_then_succeed
        service = UsageWarningService(
            entitlement_service=entitlement_service, notification_service=flaky_notification_service
        )

        with freeze_time(FREEZE_START):
            service.check_subscription(subscription)

        # Both resources were attempted despite the first one's failure.
        assert flaky_notification_service.create_notification.call_count == 2
