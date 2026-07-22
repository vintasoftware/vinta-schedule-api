"""Integration tests for the approaching-limit warning beat entry points
(``payments.tasks.check_approaching_limits`` /
``check_approaching_limits_for_subscription``).

Drives the real, DI-wired ``UsageWarningService`` (not a hand-written double)
with only ``notification_service`` swapped out, the same pattern
``payments/tests/test_dunning_schedule.py`` uses for the dunning beat task.

The key assertion is the debounce: a warning must fire **once**, never
repeatedly across beat runs while usage stays above the threshold.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from model_bakery import baker

from organizations.models import Organization, OrganizationMembership, OrganizationRole
from payments.billing_constants import BillingState, LimitedResource, LimitKind
from payments.models import BillingPlan, LimitWarningNotification, PlanLimit, Subscription
from payments.tasks import check_approaching_limits, check_approaching_limits_for_subscription
from users.models import User


pytestmark = pytest.mark.no_auto_subscription


def make_complete_plan(limit_values: dict[str, int | None] | None = None) -> BillingPlan:
    """A catalog plan carrying a ``PlanLimit`` row for every ``LimitedResource``
    member -- mirrors ``test_dunning_schedule.py``'s helper of the same name."""
    limit_values = limit_values or {}
    plan = baker.make(
        BillingPlan,
        is_default_for_new_organizations=False,
        monthly_price=Decimal("0"),
        annual_price=None,
    )
    for resource_key in LimitedResource.values:
        baker.make(
            PlanLimit,
            plan=plan,
            resource_key=resource_key,
            limit_value=limit_values.get(resource_key, 0),
            kind=LimitKind.PREPAID,
        )
    return plan


def _subscription_for(
    subscription_service, organization: Organization, plan: BillingPlan, *, billing_state: str
) -> Subscription:
    subscription = subscription_service.create_subscription_for_organization(
        organization, plan=plan
    )
    assert subscription is not None
    subscription.billing_state = billing_state
    subscription.save(update_fields=["billing_state"])
    return subscription


def _add_admin_membership(organization: Organization) -> OrganizationMembership:
    return baker.make(
        OrganizationMembership,
        organization=organization,
        user=baker.make(User),
        role=OrganizationRole.ADMIN,
        is_active=True,
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


@pytest.fixture
def organization():
    return baker.make(Organization, parent=None, can_invite_organizations=False)


@pytest.fixture
def mock_notification_service():
    return MagicMock()


@pytest.fixture(autouse=True)
def di_overrides(di_container, mock_notification_service):
    """Real ``UsageWarningService`` from the wired container, with only the
    notification service swapped out -- proves ``@inject`` on the Celery task
    actually resolves a working service, mirroring
    ``test_dunning_schedule.py``'s ``di_overrides`` fixture."""
    with di_container.notification_service.override(mock_notification_service):
        yield


@pytest.fixture
def subscription_service(di_container):
    return di_container.subscription_service()


@pytest.mark.django_db
class TestCheckApproachingLimitsFanOut:
    def test_excludes_restricted_and_cancelled_subscriptions(self, subscription_service):
        plan = make_complete_plan()

        active_org = baker.make(Organization, parent=None, can_invite_organizations=False)
        active_sub = _subscription_for(
            subscription_service, active_org, plan, billing_state=BillingState.ACTIVE
        )

        grace_org = baker.make(Organization, parent=None, can_invite_organizations=False)
        grace_sub = _subscription_for(
            subscription_service, grace_org, plan, billing_state=BillingState.GRACE
        )

        free_org = baker.make(Organization, parent=None, can_invite_organizations=False)
        free_sub = _subscription_for(
            subscription_service, free_org, plan, billing_state=BillingState.FREE
        )

        restricted_org = baker.make(Organization, parent=None, can_invite_organizations=False)
        _subscription_for(
            subscription_service, restricted_org, plan, billing_state=BillingState.RESTRICTED
        )

        cancelled_org = baker.make(Organization, parent=None, can_invite_organizations=False)
        _subscription_for(
            subscription_service, cancelled_org, plan, billing_state=BillingState.CANCELLED
        )

        with patch("payments.tasks.check_approaching_limits_for_subscription.delay") as dispatched:
            check_approaching_limits()

        dispatched_ids = {call.args[0] for call in dispatched.call_args_list}
        assert dispatched_ids == {active_sub.pk, grace_sub.pk, free_sub.pk}


@pytest.mark.django_db
class TestWarningFiresOnceAcrossBeatRuns:
    def test_repeated_beat_ticks_send_exactly_one_warning(
        self, subscription_service, mock_notification_service, organization
    ):
        _add_admin_membership(organization)
        _seed_members(organization, 7)  # + admin = 8 of 10 (80%)
        plan = make_complete_plan({LimitedResource.ORGANIZATION_MEMBERS: 10})
        subscription = _subscription_for(
            subscription_service, organization, plan, billing_state=BillingState.ACTIVE
        )

        # Three separate beat ticks, all seeing the same above-threshold usage.
        check_approaching_limits_for_subscription(subscription.pk)
        check_approaching_limits_for_subscription(subscription.pk)
        check_approaching_limits_for_subscription(subscription.pk)

        assert mock_notification_service.create_notification.call_count == 1
        assert (
            LimitWarningNotification.objects.filter(
                subscription=subscription, resource_key=LimitedResource.ORGANIZATION_MEMBERS
            ).count()
            == 1
        )

    def test_restricted_subscription_is_never_warned_even_if_dispatched_directly(
        self, subscription_service, mock_notification_service, organization
    ):
        """Belt-and-suspenders: even if a stale task message dispatched before
        a subscription moved to ``RESTRICTED`` is delivered late, the
        per-subscription task itself must still skip it (not just the
        fan-out's query)."""
        _add_admin_membership(organization)
        _seed_members(organization, 9)
        plan = make_complete_plan({LimitedResource.ORGANIZATION_MEMBERS: 10})
        subscription = _subscription_for(
            subscription_service, organization, plan, billing_state=BillingState.RESTRICTED
        )

        check_approaching_limits_for_subscription(subscription.pk)

        mock_notification_service.create_notification.assert_not_called()

    def test_a_deleted_subscription_is_skipped_not_raised(self, mock_notification_service):
        """Mirrors ``process_dunning_for_subscription``'s handling of the same
        race -- a subscription deleted between fan-out and execution must not
        raise (a raising task is redelivered forever under
        ``CELERY_TASK_ACKS_LATE``)."""
        check_approaching_limits_for_subscription(999_999_999)

        mock_notification_service.create_notification.assert_not_called()
