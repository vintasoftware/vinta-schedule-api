"""Integration test for ``GET /billing/usage/`` -- an anti-drift check: **the
usage number the API reports and the number enforcement actually counts
against must be the same derivation.**

The resource set under test is derived from ``LimitedResource`` itself (its
whole member list, ``LimitedResource.values``), not a hand-typed subset. A
newly added ``LimitedResource`` member must fail this test until the usage API
covers it, the same anti-drift discipline ``test_prepaid_resource_coverage.py``
and ``test_entitlement_service.py::test_every_limited_resource_has_a_counter``
already apply elsewhere.

For each resource, real usage is seeded (not left at zero, which even a
broken implementation could trivially "agree" on) and the endpoint's reported
``current_usage``/``limit_value`` are compared against the **enforcement
primitive itself** -- ``EntitlementService.check_limit`` for prepaid
resources, ``check_postpaid_allowance`` for the one postpaid resource
(``event_occurrences``) -- not against a second, hand-rolled count.
"""

import datetime
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

import pytest
from model_bakery import baker
from rest_framework import status

from calendar_integration.constants import CalendarType
from calendar_integration.models import AvailableTime, Calendar, CalendarGroup
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from payments.billing_constants import LimitedResource, LimitKind
from payments.models import BillingPlan, MeteredOccurrence, PlanLimit
from payments.services.entitlement_service import EntitlementService
from payments.services.subscription_service import (
    SubscriptionService,
    current_billing_period_start,
)
from public_api.models import SystemUser
from webhooks.constants import WebhookEventType
from webhooks.models import WebhookConfiguration


# This module places the organization on a hand-built plan/subscription directly,
# so it opts out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription

#: Large enough that every resource seeded below sits well under it -- the
#: point of this test is comparing two *readouts* of the same real usage, not
#: exercising the block itself (that's `test_prepaid_resource_coverage.py`'s
#: job).
LIMIT_VALUE = 1000


def make_complete_plan() -> BillingPlan:
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
            limit_value=LIMIT_VALUE,
            kind=(
                LimitKind.POSTPAID
                if resource_key == LimitedResource.EVENT_OCCURRENCES
                else LimitKind.PREPAID
            ),
            overage_unit_price=(
                Decimal("0.05") if resource_key == LimitedResource.EVENT_OCCURRENCES else None
            ),
        )
    return plan


def usage_url() -> str:
    return reverse("api:BillingUsage-retrieve")


def _seed_organization_members(organization: Organization) -> None:
    baker.make(OrganizationMembership, organization=organization, is_active=True, _quantity=2)


def _seed_resource_calendars(organization: Organization) -> None:
    for i in range(2):
        baker.make(
            Calendar,
            organization=organization,
            calendar_type=CalendarType.RESOURCE,
            external_id=f"usage-view-resource-{i}",
        )


def _seed_calendar_groups(organization: Organization) -> None:
    baker.make(CalendarGroup, organization=organization, _quantity=2)


def _seed_bundle_calendars(organization: Organization) -> None:
    for i in range(2):
        baker.make(
            Calendar,
            organization=organization,
            calendar_type=CalendarType.BUNDLE,
            external_id=f"usage-view-bundle-{i}",
        )


def _seed_availability_windows(organization: Organization) -> None:
    calendar = baker.make(
        Calendar,
        organization=organization,
        calendar_type=CalendarType.RESOURCE,
        manage_available_windows=True,
        external_id="usage-view-availability-host",
    )
    baker.make(
        AvailableTime, organization=organization, calendar=calendar, timezone="UTC", _quantity=2
    )


def _seed_webhook_subscriptions(organization: Organization) -> None:
    baker.make(
        WebhookConfiguration,
        organization=organization,
        event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
        url="https://example.com/usage-view-hook",
        _quantity=2,
    )


def _seed_public_api_system_users(organization: Organization) -> None:
    for i in range(2):
        baker.make(
            SystemUser,
            organization=organization,
            integration_name=f"usage-view-integration-{i}",
            long_lived_token_hash=f"usage-view-hash-{i}",
        )


def _seed_event_occurrences(organization: Organization, subscription) -> None:
    billing_period_start = current_billing_period_start(subscription)
    for i in range(2):
        baker.make(
            MeteredOccurrence,
            organization=organization,
            subscription=subscription,
            event_id=1000 + i,
            occurrence_start=timezone.now() + datetime.timedelta(hours=i),
            billing_period_start=billing_period_start,
            is_within_allowance=True,
            unit_price=Decimal("0"),
        )


#: One seeder per `LimitedResource` member -- keyed on the enum itself (not a
#: hand-typed string list), so `TestUsageMatchesEnforcement` below fails loudly
#: if a new `LimitedResource` member is added without a seeder registered here.
SEEDERS = {
    LimitedResource.ORGANIZATION_MEMBERS: _seed_organization_members,
    LimitedResource.RESOURCE_CALENDARS: _seed_resource_calendars,
    LimitedResource.CALENDAR_GROUPS: _seed_calendar_groups,
    LimitedResource.BUNDLE_CALENDARS: _seed_bundle_calendars,
    LimitedResource.AVAILABILITY_WINDOWS: _seed_availability_windows,
    LimitedResource.WEBHOOK_SUBSCRIPTIONS: _seed_webhook_subscriptions,
    LimitedResource.PUBLIC_API_SYSTEM_USERS: _seed_public_api_system_users,
}


@pytest.fixture
def organization() -> Organization:
    return baker.make(Organization, parent=None, can_invite_organizations=False)


@pytest.fixture
def subscription(organization):
    plan = make_complete_plan()
    return SubscriptionService().create_subscription_for_organization(organization, plan=plan)


@pytest.fixture(autouse=True)
def _seed_every_resource(organization, subscription):
    for seeder in SEEDERS.values():
        seeder(organization)
    _seed_event_occurrences(organization, subscription)


@pytest.fixture
def admin_membership(organization, user):
    return baker.make(
        OrganizationMembership,
        organization=organization,
        user=user,
        role=OrganizationRole.ADMIN,
        is_active=True,
    )


@pytest.mark.django_db
class TestUsageMatchesEnforcement:
    def test_every_seeder_is_registered_for_every_limited_resource(self):
        """The registry covers every non-postpaid ``LimitedResource`` member
        (``event_occurrences`` has its own dedicated seeder, asserted
        separately below) -- this is what makes the parametrized test below
        fail loudly, rather than silently skip, a newly added member."""
        expected = set(LimitedResource.values) - {LimitedResource.EVENT_OCCURRENCES}
        assert set(SEEDERS.keys()) == expected

    @pytest.mark.parametrize(
        "resource_key", list(LimitedResource.values), ids=LimitedResource.values
    )
    def test_usage_view_matches_the_enforcement_primitive(
        self, auth_client, admin_membership, organization, subscription, resource_key
    ):
        entitlement_service = EntitlementService()
        if resource_key == LimitedResource.EVENT_OCCURRENCES:
            enforcement_result = entitlement_service.check_postpaid_allowance(organization, delta=0)
        else:
            enforcement_result = entitlement_service.check_limit(
                organization, resource_key, delta=0
            )

        response = auth_client.get(usage_url())

        assert response.status_code == status.HTTP_200_OK
        rows = {row["resource_key"]: row for row in response.data["limits"]}
        # Every LimitedResource member appears exactly once.
        assert resource_key in rows
        row = rows[resource_key]

        assert row["current_usage"] == enforcement_result.current_usage
        assert row["limit_value"] == enforcement_result.ceiling
        # And, independently, real usage was actually seeded -- a test that
        # only proved "0 == 0" would not have caught the API and the guard
        # computing usage two different ways.
        assert enforcement_result.current_usage is not None and enforcement_result.current_usage > 0

    def test_response_covers_every_limited_resource_exactly_once(
        self, auth_client, admin_membership
    ):
        response = auth_client.get(usage_url())

        assert response.status_code == status.HTTP_200_OK
        resource_keys = [row["resource_key"] for row in response.data["limits"]]
        assert sorted(resource_keys) == sorted(LimitedResource.values)
        assert len(resource_keys) == len(set(resource_keys))
