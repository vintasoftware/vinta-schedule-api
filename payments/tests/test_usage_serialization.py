"""Unit tests pinning two serialization/resolution rules of ``GET
/billing/usage/`` that are easy to get subtly wrong:

- **Unlimited serializes as ``null``, never ``0``.** An unlimited resource has
  no ceiling. Reporting ``0`` would read as "0 of 0 -- fully consumed," which
  is the opposite of what unlimited means.
- **A reseller child reports the pooled *root* figures**, not its own. These
  resolve at the billing root, consistent with every other read/check in this
  domain (``EntitlementService``).

Also covers additive fields: attribution (``by_organization``), the
plan snapshot, and the plan/add-on decomposition of ``limit_value``.
"""

import datetime
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient
from vinta_billing.constants import BillingState, LimitKind
from vinta_billing.models import BillingPlan, PlanLimit, SubscriptionAddOn
from vinta_billing.serializers import UsageResponseSerializer
from vinta_billing.services.subscription_service import SubscriptionService

from calendar_integration.constants import CalendarType
from calendar_integration.models import Calendar
from organizations.models import Organization, OrganizationMembership
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN
from organizations.tests.helpers import make_membership
from payments.seams.resource_keys import (
    CALENDAR_GROUPS,
    EVENT_OCCURRENCES,
    ORGANIZATION_MEMBERS,
    RESOURCE_CALENDARS,
    RESOURCE_KEYS,
)
from users.factories import UserFactory


# This module places organizations on hand-built plans/subscriptions directly, so
# it opts out of conftest's autouse `provision_default_subscription`.
pytestmark = pytest.mark.no_auto_subscription


def make_complete_plan(limit_values: dict[str, int | None] | None = None) -> BillingPlan:
    limit_values = limit_values or {}
    plan = baker.make(
        BillingPlan,
        is_default_for_new_organizations=False,
        monthly_price=Decimal("0"),
        annual_price=None,
    )
    for resource_key in RESOURCE_KEYS:
        baker.make(
            PlanLimit,
            plan=plan,
            resource_key=resource_key,
            limit_value=limit_values.get(resource_key, 0),
            kind=LimitKind.PREPAID,
        )
    return plan


def usage_url() -> str:
    return reverse("api:BillingUsage-retrieve")


@pytest.mark.django_db
class TestUnlimitedResourceSerializesAsNull:
    def test_null_not_zero(self, auth_client, user):
        organization = baker.make(Organization, parent=None, can_invite_organizations=False)
        make_membership(
            organization=organization,
            user=user,
            groups=[GROUP_ORGANIZATION_ADMIN],
            is_active=True,
        )
        plan = make_complete_plan({RESOURCE_CALENDARS: None})
        SubscriptionService().create_subscription_for_organization(organization, plan=plan)

        response = auth_client.get(usage_url())

        assert response.status_code == status.HTTP_200_OK
        rows = {row["resource_key"]: row for row in response.data["limits"]}
        row = rows[RESOURCE_CALENDARS]
        assert row["limit_value"] is None
        # Pinned at the wire level too: `null`, not the string `"0"` or absent.
        assert (
            b'"limit_value":null' in response.content or b'"limit_value": null' in response.content
        )


@pytest.mark.django_db
class TestResellerChildReportsPooledRootFigures:
    def test_child_usage_is_the_roots_pooled_total(self, auth_client, user):
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        child = baker.make(Organization, parent=root, can_invite_organizations=False)
        root_plan = make_complete_plan({ORGANIZATION_MEMBERS: 20})
        subscription = SubscriptionService().create_subscription_for_organization(
            root, plan=root_plan
        )
        assert subscription is not None

        # Two other members directly on the root...
        baker.make(OrganizationMembership, organization=root, is_active=True, _quantity=2)
        # ...the calling user's own membership, on the *child* (single membership,
        # so the X-Organization-Id header is optional and resolves to `child`)...
        make_membership(
            organization=child,
            user=user,
            groups=[GROUP_ORGANIZATION_ADMIN],
            is_active=True,
        )
        # ...and three more members on the child.
        baker.make(OrganizationMembership, organization=child, is_active=True, _quantity=3)

        response = auth_client.get(usage_url())

        assert response.status_code == status.HTTP_200_OK
        # Reports the *root's* billing_state, not a child-local notion.
        assert response.data["billing_state"] == subscription.billing_state
        rows = {row["resource_key"]: row for row in response.data["limits"]}
        row = rows[ORGANIZATION_MEMBERS]
        assert row["limit_value"] == 20
        # 2 (root) + 1 (the calling user's own child membership) + 3 (child) == 6,
        # summed across the whole pooled subtree, not just `child`'s own rows.
        assert row["current_usage"] == 6

    def test_child_reports_the_same_figures_the_root_would(self, user, user_password):
        """The same pooled number, whichever organization in the tree asks --
        proven by hitting the endpoint as a root-side caller too."""
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        child = baker.make(Organization, parent=root, can_invite_organizations=False)
        root_plan = make_complete_plan({RESOURCE_CALENDARS: 5})
        SubscriptionService().create_subscription_for_organization(root, plan=root_plan)

        baker.make(
            Calendar,
            organization=root,
            calendar_type=CalendarType.RESOURCE,
            external_id="root-resource",
        )
        baker.make(
            Calendar,
            organization=child,
            calendar_type=CalendarType.RESOURCE,
            external_id="child-resource",
        )

        root_user = UserFactory().create_user()
        make_membership(
            organization=root,
            user=root_user,
            groups=[GROUP_ORGANIZATION_ADMIN],
            is_active=True,
        )
        make_membership(
            organization=child,
            user=user,
            groups=[GROUP_ORGANIZATION_ADMIN],
            is_active=True,
        )

        root_client = APIClient()
        root_client.login(email=root_user.email, password=user_password)
        child_client = APIClient()
        child_client.login(email=user.email, password=user_password)

        root_response = root_client.get(usage_url())
        child_response = child_client.get(usage_url())

        assert root_response.status_code == status.HTTP_200_OK
        assert child_response.status_code == status.HTTP_200_OK
        root_rows = {row["resource_key"]: row for row in root_response.data["limits"]}
        child_rows = {row["resource_key"]: row for row in child_response.data["limits"]}
        assert root_rows[RESOURCE_CALENDARS] == child_rows[RESOURCE_CALENDARS]
        assert child_rows[RESOURCE_CALENDARS]["current_usage"] == 2


@pytest.mark.django_db
class TestRestrictedOrganizationCanStillReadUsage:
    def test_restricted_org_gets_200(self, auth_client, user):
        organization = baker.make(Organization, parent=None, can_invite_organizations=False)
        make_membership(
            organization=organization,
            user=user,
            groups=[GROUP_ORGANIZATION_ADMIN],
            is_active=True,
        )
        plan = make_complete_plan({RESOURCE_CALENDARS: 5})
        subscription = SubscriptionService().create_subscription_for_organization(
            organization, plan=plan
        )
        assert subscription is not None
        subscription.billing_state = BillingState.RESTRICTED
        subscription.save(update_fields=["billing_state"])
        # Real, non-zero usage on a RESTRICTED org -- `check_limit`/
        # `check_postpaid_allowance` deliberately report a `0/0` sentinel for a
        # RESTRICTED subscription's block-decision path; this pins that the
        # usage view reports the organization's *true* usage/limit instead of
        # ever being routed through that sentinel.
        for i in range(3):
            baker.make(
                Calendar,
                organization=organization,
                calendar_type=CalendarType.RESOURCE,
                external_id=f"restricted-org-resource-{i}",
            )

        response = auth_client.get(usage_url())

        assert response.status_code == status.HTTP_200_OK
        assert response.data["billing_state"] == BillingState.RESTRICTED
        rows = {row["resource_key"]: row for row in response.data["limits"]}
        row = rows[RESOURCE_CALENDARS]
        assert row["limit_value"] == 5
        assert row["current_usage"] == 3


@pytest.mark.django_db
class TestEnrichedResponseSerialization:
    """Pure serializer-level unit tests -- no view, no ``EntitlementService`` --
    pinning that ``UsageResponseSerializer`` renders attribution, the plan
    snapshot, and the plan/add-on split exactly as documented."""

    def test_renders_attribution_plan_snapshot_and_add_on_split(self):
        period_start = timezone.now()
        period_end = period_start + datetime.timedelta(days=30)
        data = {
            "billing_state": BillingState.ACTIVE,
            "billing_root_organization_id": 12,
            "plan": {"slug": "pro", "name": "Pro", "currency": "USD"},
            "billing_period": {"start": period_start, "end": period_end},
            "estimated_overage_total": Decimal("12.5"),
            "limits": [
                {
                    "resource_key": EVENT_OCCURRENCES,
                    "kind": LimitKind.POSTPAID,
                    "limit_value": 1000,
                    "current_usage": 1250,
                    "overage_unit_price": Decimal("0.01"),
                    "included_in_plan": 500,
                    "add_on_quantity": 500,
                    "by_organization": [
                        {"organization_id": 12, "name": "Acme", "usage": 900},
                        {"organization_id": 31, "name": "Acme West", "usage": 350},
                    ],
                }
            ],
        }

        payload = UsageResponseSerializer(data).data

        assert payload["billing_root_organization_id"] == 12
        assert payload["plan"] == {"slug": "pro", "name": "Pro", "currency": "USD"}
        assert payload["billing_period"]["start"] is not None
        assert payload["billing_period"]["end"] is not None
        assert payload["estimated_overage_total"] == "12.5000"
        row = payload["limits"][0]
        assert row["included_in_plan"] == 500
        assert row["add_on_quantity"] == 500
        # The decomposition invariant: the two new fields sum back to the
        # existing (unchanged) limit_value.
        assert row["included_in_plan"] + row["add_on_quantity"] == row["limit_value"]
        assert row["by_organization"] == [
            {"organization_id": 12, "name": "Acme", "usage": 900},
            {"organization_id": 31, "name": "Acme West", "usage": 350},
        ]

    def test_null_plan_and_period_when_no_subscription(self):
        """The no-subscription path: plan/billing_period render null, and
        estimated_overage_total renders "0.0000", never absent or "0"."""
        data = {
            "billing_state": BillingState.FREE,
            "billing_root_organization_id": 7,
            "plan": None,
            "billing_period": None,
            "estimated_overage_total": Decimal("0"),
            "limits": [
                {
                    "resource_key": RESOURCE_CALENDARS,
                    "kind": None,
                    "limit_value": None,
                    "current_usage": 0,
                    "overage_unit_price": None,
                    "included_in_plan": None,
                    "add_on_quantity": 0,
                    "by_organization": [],
                }
            ],
        }

        payload = UsageResponseSerializer(data).data

        assert payload["plan"] is None
        assert payload["billing_period"] is None
        assert payload["estimated_overage_total"] == "0.0000"
        row = payload["limits"][0]
        assert row["limit_value"] is None
        assert row["included_in_plan"] is None
        assert row["by_organization"] == []


@pytest.mark.django_db
class TestPlanAddOnDecompositionInvariant:
    """Integration: the invariant holds against the real view, not just a
    hand-built dict -- purchasing an add-on must show up split correctly."""

    def test_included_in_plan_plus_add_on_quantity_equals_limit_value(self, auth_client, user):
        organization = baker.make(Organization, parent=None, can_invite_organizations=False)
        make_membership(
            organization=organization,
            user=user,
            groups=[GROUP_ORGANIZATION_ADMIN],
            is_active=True,
        )
        plan = make_complete_plan({CALENDAR_GROUPS: 5})
        subscription = SubscriptionService().create_subscription_for_organization(
            organization, plan=plan
        )
        assert subscription is not None
        baker.make(
            SubscriptionAddOn,
            subscription=subscription,
            resource_key=CALENDAR_GROUPS,
            quantity=3,
            is_recurring=True,
            is_active=True,
        )

        response = auth_client.get(usage_url())

        assert response.status_code == status.HTTP_200_OK
        rows = {row["resource_key"]: row for row in response.data["limits"]}
        for row in rows.values():
            if row["limit_value"] is not None:
                assert row["included_in_plan"] + row["add_on_quantity"] == row["limit_value"]

        add_on_row = rows[CALENDAR_GROUPS]
        assert add_on_row["included_in_plan"] == 5
        assert add_on_row["add_on_quantity"] == 3
        assert add_on_row["limit_value"] == 8


@pytest.mark.django_db
class TestAddOnPurchasedOnUnlimitedPlan:
    """An add-on purchased for a resource whose plan-limit row is explicitly
    unlimited (``limit_value=None``) still reports what was purchased via
    ``add_on_quantity`` -- it is informational and does not itself redefine an
    unlimited ceiling -- while ``included_in_plan``/``limit_value`` both stay
    ``None``, per the fail-open rule they follow."""

    def test_add_on_quantity_is_reported_while_included_in_plan_stays_null(self, auth_client, user):
        organization = baker.make(Organization, parent=None, can_invite_organizations=False)
        make_membership(
            organization=organization,
            user=user,
            groups=[GROUP_ORGANIZATION_ADMIN],
            is_active=True,
        )
        plan = make_complete_plan({CALENDAR_GROUPS: None})
        subscription = SubscriptionService().create_subscription_for_organization(
            organization, plan=plan
        )
        assert subscription is not None
        baker.make(
            SubscriptionAddOn,
            subscription=subscription,
            resource_key=CALENDAR_GROUPS,
            quantity=3,
            is_recurring=True,
            is_active=True,
        )

        response = auth_client.get(usage_url())

        assert response.status_code == status.HTTP_200_OK
        rows = {row["resource_key"]: row for row in response.data["limits"]}
        row = rows[CALENDAR_GROUPS]
        assert row["limit_value"] is None
        assert row["included_in_plan"] is None
        assert row["add_on_quantity"] == 3
