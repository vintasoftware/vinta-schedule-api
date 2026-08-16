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

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

import pytest
from model_bakery import baker
from rest_framework import status

from calendar_integration.constants import CalendarType
from calendar_integration.models import AvailableTime, Calendar, CalendarGroup
from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN
from organizations.tests.helpers import make_membership
from payments.billing_constants import BillingState, LimitedResource, LimitKind
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
    return make_membership(
        organization=organization,
        user=user,
        groups=[GROUP_ORGANIZATION_ADMIN],
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


@pytest.mark.django_db
class TestBackwardsCompatibility:
    """**Backwards-compatibility acceptance criterion.** Every key ``GET
    /billing/usage/`` returned before the enrichment fields were added must
    still be present, with the same type and the same value, against a
    fixture that predates the change (this module's own auto-seeded
    ``_seed_every_resource`` fixture, untouched by that addition).

    A failure here means an existing caller of the original response shape
    would observe something different -- which is exactly what this
    guarantee promises never happens. Do not edit this test to make it pass;
    if it fails, the view change is wrong.
    """

    def test_every_pre_enrichment_key_is_present_and_unchanged(
        self, auth_client, admin_membership, organization, subscription
    ):
        entitlement_service = EntitlementService()

        response = auth_client.get(usage_url())

        assert response.status_code == status.HTTP_200_OK
        # The two top-level keys the endpoint has always returned.
        assert response.data["billing_state"] == subscription.billing_state
        assert isinstance(response.data["limits"], list)

        rows = {row["resource_key"]: row for row in response.data["limits"]}
        assert sorted(rows) == sorted(LimitedResource.values)

        for resource_key in LimitedResource.values:
            row = rows[resource_key]
            # Every key this row carried before the enrichment fields were added
            # is still present.
            assert {
                "resource_key",
                "kind",
                "limit_value",
                "current_usage",
                "overage_unit_price",
            } <= set(row)

            effective_limit = entitlement_service.get_effective_limit(organization, resource_key)
            if resource_key == LimitedResource.EVENT_OCCURRENCES:
                enforcement_result = entitlement_service.check_postpaid_allowance(
                    organization, delta=0
                )
            else:
                enforcement_result = entitlement_service.check_limit(
                    organization, resource_key, delta=0
                )

            assert row["resource_key"] == resource_key
            assert row["kind"] == effective_limit.kind
            assert row["limit_value"] == enforcement_result.ceiling
            assert row["limit_value"] is None or isinstance(row["limit_value"], int)
            assert row["current_usage"] == enforcement_result.current_usage
            assert isinstance(row["current_usage"], int)
            if effective_limit.overage_unit_price is None:
                assert row["overage_unit_price"] is None
            else:
                assert Decimal(row["overage_unit_price"]) == effective_limit.overage_unit_price


@pytest.mark.django_db
class TestPooledAttributionOmitsNonContributors:
    def test_reseller_subtree_attributes_usage_to_the_right_children(self, auth_client, user):
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        contributing_child = baker.make(Organization, parent=root, can_invite_organizations=False)
        silent_child = baker.make(Organization, parent=root, can_invite_organizations=False)
        plan = make_complete_plan()
        SubscriptionService().create_subscription_for_organization(root, plan=plan)
        make_membership(
            organization=contributing_child,
            user=user,
            groups=[GROUP_ORGANIZATION_ADMIN],
            is_active=True,
        )
        for i in range(2):
            baker.make(
                Calendar,
                organization=contributing_child,
                calendar_type=CalendarType.RESOURCE,
                external_id=f"attribution-{i}",
            )

        response = auth_client.get(usage_url())

        assert response.status_code == status.HTTP_200_OK
        rows = {row["resource_key"]: row for row in response.data["limits"]}
        by_organization = {
            entry["organization_id"]: entry
            for entry in rows[LimitedResource.RESOURCE_CALENDARS]["by_organization"]
        }
        assert by_organization[contributing_child.pk] == {
            "organization_id": contributing_child.pk,
            "name": contributing_child.name,
            "usage": 2,
        }
        # Neither the root nor the silent child contributed any resource
        # calendars -- both are omitted, never present with usage: 0.
        assert root.pk not in by_organization
        assert silent_child.pk not in by_organization


@pytest.mark.django_db
class TestEstimatedOverageTotal:
    def test_matches_overage_total_over_the_current_period(
        self, auth_client, admin_membership, organization, subscription
    ):
        billing_period_start = current_billing_period_start(subscription)
        for i in range(3):
            baker.make(
                MeteredOccurrence,
                organization=organization,
                subscription=subscription,
                event_id=6000 + i,
                occurrence_start=timezone.now() + datetime.timedelta(hours=i),
                billing_period_start=billing_period_start,
                is_within_allowance=False,
                unit_price=Decimal("0.05"),
            )

        response = auth_client.get(usage_url())

        assert response.status_code == status.HTTP_200_OK
        expected = (
            MeteredOccurrence.objects.for_billing_period(subscription.pk, billing_period_start)
            .for_organizations([organization.pk])
            .overage_total()
        )
        assert expected == Decimal("0.15")
        assert Decimal(response.data["estimated_overage_total"]) == expected

    def test_pooled_subtree_overage_is_included_but_sibling_root_overage_is_not(
        self, auth_client, user
    ):
        """Mirrors ``TestPooledAttributionOmitsNonContributors``'s tree shape:
        overage metered against a child in the caller's own pooled subtree
        contributes to the root's ``estimated_overage_total`` (the
        ``.for_organizations(pool)`` scope), but overage metered against an
        unrelated, sibling billing root's own subtree does not leak in."""
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        contributing_child = baker.make(Organization, parent=root, can_invite_organizations=False)
        sibling_root = baker.make(Organization, parent=None, can_invite_organizations=True)
        plan = make_complete_plan()
        subscription = SubscriptionService().create_subscription_for_organization(root, plan=plan)
        sibling_plan = make_complete_plan()
        sibling_subscription = SubscriptionService().create_subscription_for_organization(
            sibling_root, plan=sibling_plan
        )
        make_membership(
            organization=contributing_child,
            user=user,
            groups=[GROUP_ORGANIZATION_ADMIN],
            is_active=True,
        )

        billing_period_start = current_billing_period_start(subscription)
        baker.make(
            MeteredOccurrence,
            organization=contributing_child,
            subscription=subscription,
            event_id=8000,
            occurrence_start=timezone.now(),
            billing_period_start=billing_period_start,
            is_within_allowance=False,
            unit_price=Decimal("0.05"),
        )
        sibling_billing_period_start = current_billing_period_start(sibling_subscription)
        baker.make(
            MeteredOccurrence,
            organization=sibling_root,
            subscription=sibling_subscription,
            event_id=8001,
            occurrence_start=timezone.now(),
            billing_period_start=sibling_billing_period_start,
            is_within_allowance=False,
            unit_price=Decimal("0.05"),
        )

        response = auth_client.get(usage_url())

        assert response.status_code == status.HTTP_200_OK
        assert response.data["billing_root_organization_id"] == root.pk
        # Only the contributing child's overage counts -- the sibling root's own
        # subtree is a disjoint pool and must not be summed in here.
        assert Decimal(response.data["estimated_overage_total"]) == Decimal("0.05")


@pytest.mark.django_db
class TestNoSubscriptionOrganization:
    def test_free_organization_gets_200_with_null_plan_and_period(self, auth_client, user):
        free_organization = baker.make(Organization, parent=None, can_invite_organizations=False)
        make_membership(
            organization=free_organization,
            user=user,
            groups=[GROUP_ORGANIZATION_ADMIN],
            is_active=True,
        )

        response = auth_client.get(usage_url())

        assert response.status_code == status.HTTP_200_OK
        assert response.data["billing_state"] == BillingState.FREE
        assert response.data["plan"] is None
        assert response.data["billing_period"] is None
        assert response.data["estimated_overage_total"] == "0.0000"
        assert response.data["billing_root_organization_id"] == free_organization.pk


@pytest.mark.django_db
class TestRestrictedOrganizationCanStillReadEnrichedUsage:
    """The read-never-blocks rule in the viewset docstring extends to every
    additive enrichment field, not just the pre-existing ones -- a RESTRICTED
    organization needs the plan/period/overage figures to resolve billing at
    least as much as an ACTIVE one does."""

    def test_restricted_org_still_gets_plan_period_and_overage(
        self, auth_client, admin_membership, organization, subscription
    ):
        subscription.billing_state = BillingState.RESTRICTED
        subscription.save(update_fields=["billing_state"])

        response = auth_client.get(usage_url())

        assert response.status_code == status.HTTP_200_OK
        assert response.data["billing_state"] == BillingState.RESTRICTED
        assert response.data["plan"] is not None
        assert response.data["billing_period"] is not None
        assert response.data["estimated_overage_total"] is not None


@pytest.mark.django_db
class TestRootResolutionAndSubtreeWalkHappenOnce:
    """Query-count regression gate for the N+1 fix: previously, the loop
    over ``LimitedResource`` called ``get_effective_limit``/``get_current_usage``
    per resource, each independently re-walking the ``parent`` chain and
    re-running the subtree BFS -- sixteen root resolutions and eight subtree
    walks for eight resources. Both must now happen exactly once per request,
    regardless of how many resources exist.
    """

    def test_query_count_does_not_scale_with_the_number_of_resources(self, auth_client, user):
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        child = baker.make(Organization, parent=root, can_invite_organizations=False)
        plan = make_complete_plan()
        SubscriptionService().create_subscription_for_organization(root, plan=plan)
        make_membership(
            organization=child,
            user=user,
            groups=[GROUP_ORGANIZATION_ADMIN],
            is_active=True,
        )

        with CaptureQueriesContext(connection) as captured:
            response = auth_client.get(usage_url())

        assert response.status_code == status.HTTP_200_OK
        queries = [query["sql"] for query in captured.captured_queries]

        # `_get_pooled_organization_ids`'s subtree BFS issues one query per
        # level of the pool's tree -- two for this two-level tree, never one
        # per LimitedResource member (eight).
        subtree_walk_queries = [
            query for query in queries if '"organizations_organization"."parent_id" IN' in query
        ]
        assert 0 < len(subtree_walk_queries) < len(LimitedResource.values)

        # `resolve_billing_root`'s parent-chain walk issues one single-row
        # lookup per level walked -- exactly one here (child -> root), not
        # once per resource and not sixteen times across the whole response.
        root_walk_queries = [
            query for query in queries if '"organizations_organization"."id" = ' in query
        ]
        assert len(root_walk_queries) == 1

        # Explicit ceiling so a future regression that reintroduces a
        # per-resource duplicate (e.g. the `effective_limit_for_subscription`
        # N+1 this gate was built for) fails here instead of drifting
        # unnoticed. Measured at 37 queries before `effective_limit_from_resolved`
        # (16 of them were the per-resource `SubscriptionPlanLimit` lookup +
        # add-on `Sum` aggregate `effective_limit_for_subscription` re-ran on
        # every resource despite the view already batching both), 21 after.
        assert len(queries) <= 21
