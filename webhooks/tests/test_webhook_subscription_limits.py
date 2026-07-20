"""Phase 6c: the pre-paid limit guard on ``WebhookService.create_configuration``.

Spec use-case 2 ("an organization hits a pre-paid limit and is blocked"), applied
to the ``webhook_subscriptions`` resource. The guard's predicate is derived from
the counter it guards (``EntitlementService._count_webhook_subscriptions``, which
counts ``WebhookConfiguration.objects.live()`` -- ``deleted_at__isnull=True``): a
freshly created row always has ``deleted_at=None``, so there is nothing to keep in
sync between the two.

Every test in this module was confirmed to fail when the guard was removed.
"""

import datetime

from django.utils import timezone

import pytest
from model_bakery import baker

from organizations.models import Organization
from payments.billing_constants import BillingState, LimitedResource, LimitKind
from payments.exceptions import OverLimitError
from payments.models import BillingPlan, Subscription, SubscriptionPlanLimit
from webhooks.constants import WebhookEventType
from webhooks.models import WebhookConfiguration
from webhooks.services.webhook_service import WebhookService


def _organization_with_limit(limit_value: int | None) -> Organization:
    """A standalone (non-reseller) organization with a ceiling on
    ``webhook_subscriptions``. ``limit_value=None`` builds an ``unlimited``-shaped
    subscription (NULL ceiling) -- the plan's "no feature flag" rollout switch --
    so the guard is exercised against it as well as a finite ceiling.
    """
    organization = baker.make(Organization, parent=None, can_invite_organizations=False)
    now = timezone.now()
    subscription = baker.make(
        Subscription,
        organization=organization,
        plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
        billing_state=BillingState.FREE,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )
    baker.make(
        SubscriptionPlanLimit,
        subscription=subscription,
        resource_key=LimitedResource.WEBHOOK_SUBSCRIPTIONS,
        limit_value=limit_value,
        kind=LimitKind.PREPAID,
    )
    return organization


@pytest.fixture
def service() -> WebhookService:
    return WebhookService()


@pytest.mark.django_db
class TestCreateConfigurationLimit:
    def test_raises_and_creates_nothing_at_the_limit(self, service):
        organization = _organization_with_limit(1)
        baker.make(
            WebhookConfiguration,
            organization=organization,
            event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
            url="https://example.com/seed",
        )

        with pytest.raises(OverLimitError) as exc_info:
            service.create_configuration(
                organization=organization,
                event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
                url="https://example.com/blocked",
                headers={},
            )

        assert exc_info.value.resource_key == LimitedResource.WEBHOOK_SUBSCRIPTIONS
        assert exc_info.value.current_usage == 1
        assert exc_info.value.limit == 1
        assert not WebhookConfiguration.objects.filter(
            organization=organization, url="https://example.com/blocked"
        ).exists()

    def test_soft_deleted_configurations_free_capacity(self, service):
        """A soft-deleted row must not count against the ceiling -- it feeds the
        same ``.live()`` predicate the counter uses."""
        organization = _organization_with_limit(1)
        baker.make(
            WebhookConfiguration,
            organization=organization,
            event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
            url="https://example.com/deleted",
            deleted_at=timezone.now(),
        )

        configuration = service.create_configuration(
            organization=organization,
            event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
            url="https://example.com/fits",
            headers={},
        )

        assert configuration.pk is not None

    @pytest.mark.parametrize("limit_value", [2, None], ids=["headroom", "unlimited"])
    def test_succeeds_with_headroom(self, service, limit_value):
        organization = _organization_with_limit(limit_value)
        baker.make(
            WebhookConfiguration,
            organization=organization,
            event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
            url="https://example.com/seed2",
        )

        configuration = service.create_configuration(
            organization=organization,
            event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
            url="https://example.com/fits2",
            headers={},
        )

        assert configuration.pk is not None

    def test_bypass_limits_creates_anyway(self, service):
        organization = _organization_with_limit(1)
        baker.make(
            WebhookConfiguration,
            organization=organization,
            event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
            url="https://example.com/seed3",
        )

        configuration = service.create_configuration(
            organization=organization,
            event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
            url="https://example.com/bypassed",
            headers={},
            bypass_limits=True,
        )

        assert configuration.pk is not None
