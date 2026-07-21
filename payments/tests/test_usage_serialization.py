"""Unit tests pinning two serialization/resolution rules of ``GET
/billing/usage/`` (Phase 12) that are easy to get subtly wrong:

- **Unlimited serializes as ``null``, never ``0``.** An unlimited resource has
  no ceiling; reporting ``0`` would read as "0 of 0 -- fully consumed," which
  is the opposite of what unlimited means.
- **A reseller child reports the pooled *root* figures**, not its own --
  resolved at the billing root, consistent with every other read/check in
  this domain (``EntitlementService``).
"""

from decimal import Decimal

from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarType
from calendar_integration.models import Calendar
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from payments.billing_constants import BillingState, LimitedResource, LimitKind
from payments.models import BillingPlan, PlanLimit
from payments.services.subscription_service import SubscriptionService
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
    for resource_key in LimitedResource.values:
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
        baker.make(
            OrganizationMembership,
            organization=organization,
            user=user,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        plan = make_complete_plan({LimitedResource.RESOURCE_CALENDARS: None})
        SubscriptionService().create_subscription_for_organization(organization, plan=plan)

        response = auth_client.get(usage_url())

        assert response.status_code == status.HTTP_200_OK
        rows = {row["resource_key"]: row for row in response.data["limits"]}
        row = rows[LimitedResource.RESOURCE_CALENDARS]
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
        root_plan = make_complete_plan({LimitedResource.ORGANIZATION_MEMBERS: 20})
        subscription = SubscriptionService().create_subscription_for_organization(
            root, plan=root_plan
        )
        assert subscription is not None

        # Two other members directly on the root...
        baker.make(OrganizationMembership, organization=root, is_active=True, _quantity=2)
        # ...the calling user's own membership, on the *child* (single membership,
        # so the X-Organization-Id header is optional and resolves to `child`)...
        baker.make(
            OrganizationMembership,
            organization=child,
            user=user,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        # ...and three more members on the child.
        baker.make(OrganizationMembership, organization=child, is_active=True, _quantity=3)

        response = auth_client.get(usage_url())

        assert response.status_code == status.HTTP_200_OK
        # Reports the *root's* billing_state, not a child-local notion.
        assert response.data["billing_state"] == subscription.billing_state
        rows = {row["resource_key"]: row for row in response.data["limits"]}
        row = rows[LimitedResource.ORGANIZATION_MEMBERS]
        assert row["limit_value"] == 20
        # 2 (root) + 1 (the calling user's own child membership) + 3 (child) == 6,
        # summed across the whole pooled subtree, not just `child`'s own rows.
        assert row["current_usage"] == 6

    def test_child_reports_the_same_figures_the_root_would(self, user, user_password):
        """The same pooled number, whichever organization in the tree asks --
        proven by hitting the endpoint as a root-side caller too."""
        root = baker.make(Organization, parent=None, can_invite_organizations=True)
        child = baker.make(Organization, parent=root, can_invite_organizations=False)
        root_plan = make_complete_plan({LimitedResource.RESOURCE_CALENDARS: 5})
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
        baker.make(
            OrganizationMembership,
            organization=root,
            user=root_user,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        baker.make(
            OrganizationMembership,
            organization=child,
            user=user,
            role=OrganizationRole.ADMIN,
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
        assert (
            root_rows[LimitedResource.RESOURCE_CALENDARS]
            == child_rows[LimitedResource.RESOURCE_CALENDARS]
        )
        assert child_rows[LimitedResource.RESOURCE_CALENDARS]["current_usage"] == 2


@pytest.mark.django_db
class TestRestrictedOrganizationCanStillReadUsage:
    def test_restricted_org_gets_200(self, auth_client, user):
        organization = baker.make(Organization, parent=None, can_invite_organizations=False)
        baker.make(
            OrganizationMembership,
            organization=organization,
            user=user,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        plan = make_complete_plan({LimitedResource.RESOURCE_CALENDARS: 5})
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
        row = rows[LimitedResource.RESOURCE_CALENDARS]
        assert row["limit_value"] == 5
        assert row["current_usage"] == 3
