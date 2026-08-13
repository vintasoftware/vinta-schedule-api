"""Integration tests for ``GET /billing/usage/periods/`` (list) and ``GET
/billing/usage/periods/{id}/`` (detail) -- the closed-period statement
endpoints ``BillingPeriodViewSet`` serves. Bundled per the plan's "Bundled
phase granularity" decision: list and detail share a queryset, a permission,
and a serializer tree, so both live in this one module.

``provision_default_subscription`` (root conftest, autouse) already gives every
``Organization`` created here a ``Subscription`` on the seeded ``unlimited``
plan -- reused directly rather than building a second one, which would raise
on the ``Subscription.organization`` ``OneToOneField``.
"""

import datetime
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status

from organizations.models import Organization, OrganizationRole
from organizations.tests.helpers import make_membership
from payments.billing_constants import BillingInterval
from payments.models import BillingPeriodResourceUsage, BillingPeriodSummary, Payment


def periods_list_url() -> str:
    return reverse("api:BillingUsagePeriod-list")


def period_detail_url(pk: int) -> str:
    return reverse("api:BillingUsagePeriod-detail", kwargs={"pk": pk})


def make_summary(
    *,
    organization: Organization,
    subscription,
    billing_period_start: datetime.datetime,
    billing_period_end: datetime.datetime | None = None,
    overage_total: str = "0.0000",
    charged: bool = False,
    payment: Payment | None = None,
    reconciliation_unmetered: int = 0,
    reconciliation_orphaned: int = 0,
    closed_at: datetime.datetime | None = None,
    plan_slug: str = "pro",
    plan_name: str = "Pro",
    billing_interval: str = BillingInterval.MONTHLY,
    currency: str = "USD",
) -> BillingPeriodSummary:
    return baker.make(
        BillingPeriodSummary,
        subscription=subscription,
        organization=organization,
        billing_period_start=billing_period_start,
        billing_period_end=billing_period_end or billing_period_start + datetime.timedelta(days=30),
        plan_slug=plan_slug,
        plan_name=plan_name,
        billing_interval=billing_interval,
        currency=currency,
        overage_total=Decimal(overage_total),
        charged=charged,
        payment=payment,
        reconciliation_unmetered=reconciliation_unmetered,
        reconciliation_orphaned=reconciliation_orphaned,
        closed_at=closed_at or datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC),
    )


@pytest.fixture
def root() -> Organization:
    return baker.make(Organization, parent=None, can_invite_organizations=True)


@pytest.fixture
def child(root: Organization) -> Organization:
    return baker.make(Organization, parent=root, can_invite_organizations=False)


@pytest.fixture
def subscription(root: Organization):
    return root.subscription


@pytest.fixture
def child_admin_membership(user, child: Organization):
    """The caller authenticates as an admin of the *child*, not the root, to
    prove list/retrieve resolve to the pooled billing root's statements
    rather than only ones addressed to the caller's own organization."""
    return make_membership(
        organization=child,
        user=user,
        role=OrganizationRole.ADMIN,
        is_active=True,
    )


@pytest.mark.django_db
class TestListReturnsCallersPooledStatements:
    def test_list_returns_only_pooled_statements_newest_first(
        self, auth_client, child_admin_membership, root, subscription
    ):
        period_1 = make_summary(
            organization=root,
            subscription=subscription,
            billing_period_start=datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC),
        )
        period_2 = make_summary(
            organization=root,
            subscription=subscription,
            billing_period_start=datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC),
        )
        period_3 = make_summary(
            organization=root,
            subscription=subscription,
            billing_period_start=datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC),
        )
        # An unrelated billing root's statement must never leak into this pool.
        other_root = baker.make(Organization, parent=None, can_invite_organizations=True)
        make_summary(
            organization=other_root,
            subscription=other_root.subscription,
            billing_period_start=datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC),
        )

        response = auth_client.get(periods_list_url())

        assert response.status_code == status.HTTP_200_OK
        assert response.data["count"] == 3
        returned_ids = [row["id"] for row in response.data["results"]]
        assert returned_ids == [period_3.pk, period_2.pk, period_1.pk]

    def test_list_is_paginated(self, auth_client, child_admin_membership, root, subscription):
        for i in range(12):
            make_summary(
                organization=root,
                subscription=subscription,
                billing_period_start=datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
                + datetime.timedelta(days=31 * i),
            )

        response = auth_client.get(periods_list_url())

        assert response.status_code == status.HTTP_200_OK
        assert response.data["count"] == 12
        # Project default LimitOffsetPagination page size (10) -- not all 12
        # rows come back in one page.
        assert len(response.data["results"]) == 10
        assert response.data["next"] is not None

    def test_organization_with_no_closed_periods_gets_empty_200(
        self, auth_client, child_admin_membership
    ):
        response = auth_client.get(periods_list_url())

        assert response.status_code == status.HTTP_200_OK
        assert response.data["count"] == 0
        assert response.data["results"] == []


@pytest.mark.django_db
class TestNoActiveOrganizationIsForbidden:
    """A caller with zero active memberships gets ``403``, not ``200`` with an
    empty list -- that ambiguity would otherwise be indistinguishable from
    this phase's expected day-one state (an organization with no closed
    periods yet), matching ``GET /billing/usage/``'s ``_require_organization``
    contract."""

    def test_list_is_403_without_an_active_organization(self, auth_client):
        response = auth_client.get(periods_list_url())

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_retrieve_is_403_without_an_active_organization(self, auth_client):
        response = auth_client.get(period_detail_url(999_999))

        assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
class TestListFilters:
    def test_billing_period_start_after_and_before_narrow_the_list(
        self, auth_client, child_admin_membership, root, subscription
    ):
        june = make_summary(
            organization=root,
            subscription=subscription,
            billing_period_start=datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC),
        )
        july = make_summary(
            organization=root,
            subscription=subscription,
            billing_period_start=datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC),
        )
        make_summary(
            organization=root,
            subscription=subscription,
            billing_period_start=datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC),
        )

        response = auth_client.get(
            periods_list_url(),
            {
                "billing_period_start_after": "2026-06-15T00:00:00Z",
                "billing_period_start_before": "2026-07-15T00:00:00Z",
            },
        )

        assert response.status_code == status.HTTP_200_OK
        returned_ids = [row["id"] for row in response.data["results"]]
        assert returned_ids == [july.pk]
        assert june.pk not in returned_ids

    def test_charged_filter_narrows_the_list(
        self, auth_client, child_admin_membership, root, subscription
    ):
        charged_period = make_summary(
            organization=root,
            subscription=subscription,
            billing_period_start=datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC),
            charged=True,
            overage_total="10.0000",
        )
        uncharged_period = make_summary(
            organization=root,
            subscription=subscription,
            billing_period_start=datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC),
            charged=False,
        )

        charged_response = auth_client.get(periods_list_url(), {"charged": "true"})
        uncharged_response = auth_client.get(periods_list_url(), {"charged": "false"})

        assert [row["id"] for row in charged_response.data["results"]] == [charged_period.pk]
        assert [row["id"] for row in uncharged_response.data["results"]] == [uncharged_period.pk]


@pytest.mark.django_db
class TestDetailReturnsResourcesAndDistinguishesNulls:
    def test_detail_returns_all_resource_rows(
        self, auth_client, child_admin_membership, root, subscription
    ):
        summary = make_summary(
            organization=root,
            subscription=subscription,
            billing_period_start=datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC),
        )
        baker.make(
            BillingPeriodResourceUsage,
            summary=summary,
            resource_key="organization_members",
            kind="prepaid",
            total=14,
            limit_value=25,
            overage_unit_price=None,
            by_organization={str(root.pk): 14},
        )
        baker.make(
            BillingPeriodResourceUsage,
            summary=summary,
            resource_key="event_occurrences",
            kind="postpaid",
            total=1250,
            limit_value=1000,
            overage_unit_price=Decimal("0.0100"),
            by_organization={str(root.pk): 1250},
        )

        response = auth_client.get(period_detail_url(summary.pk))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["id"] == summary.pk
        resources_by_key = {row["resource_key"]: row for row in response.data["resources"]}
        assert set(resources_by_key) == {"organization_members", "event_occurrences"}

        members_row = resources_by_key["organization_members"]
        assert members_row["total"] == 14
        assert members_row["limit_value"] == 25
        assert members_row["overage_unit_price"] is None
        assert members_row["by_organization"] == [
            {"organization_id": root.pk, "name": root.name, "usage": 14}
        ]

        occurrences_row = resources_by_key["event_occurrences"]
        assert Decimal(occurrences_row["overage_unit_price"]) == Decimal("0.0100")

    def test_total_null_serializes_as_null_not_zero(
        self, auth_client, child_admin_membership, root, subscription
    ):
        summary = make_summary(
            organization=root,
            subscription=subscription,
            billing_period_start=datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC),
        )
        baker.make(
            BillingPeriodResourceUsage,
            summary=summary,
            resource_key="resource_calendars",
            total=None,
            limit_value=None,
        )
        baker.make(
            BillingPeriodResourceUsage,
            summary=summary,
            resource_key="calendar_groups",
            total=0,
            limit_value=10,
        )

        response = auth_client.get(period_detail_url(summary.pk))

        assert response.status_code == status.HTTP_200_OK
        resources_by_key = {row["resource_key"]: row for row in response.data["resources"]}
        # `null` -- "not recorded" -- must never render as the integer 0.
        assert resources_by_key["resource_calendars"]["total"] is None
        assert resources_by_key["resource_calendars"]["limit_value"] is None
        # A recorded usage of exactly zero is a distinct, ordinary integer 0.
        assert resources_by_key["calendar_groups"]["total"] == 0
        assert resources_by_key["calendar_groups"]["limit_value"] == 10


@pytest.mark.django_db
class TestStatementOutsideThePoolReturns404NotForbidden:
    def test_other_billing_roots_statement_is_404(self, auth_client, child_admin_membership):
        other_root = baker.make(Organization, parent=None, can_invite_organizations=True)
        other_summary = make_summary(
            organization=other_root,
            subscription=other_root.subscription,
            billing_period_start=datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC),
        )

        response = auth_client.get(period_detail_url(other_summary.pk))

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_nonexistent_pk_is_also_404(self, auth_client, child_admin_membership):
        response = auth_client.get(period_detail_url(999_999))

        assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
class TestReconciliationFieldsAreNeverSerialized:
    """Reconciliation drift is internal investigation data, surfaced only in
    Django admin -- an explicit Non-goal of exposing it to customers. Asserted
    against the full serialized payload (as text), not just a field-name
    check, so a nested or renamed leak would still be caught."""

    def test_reconciliation_fields_absent_from_list_and_detail(
        self, auth_client, child_admin_membership, root, subscription
    ):
        summary = make_summary(
            organization=root,
            subscription=subscription,
            billing_period_start=datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC),
            reconciliation_unmetered=7,
            reconciliation_orphaned=3,
        )
        baker.make(
            BillingPeriodResourceUsage,
            summary=summary,
            resource_key="event_occurrences",
            total=100,
        )

        list_response = auth_client.get(periods_list_url())
        detail_response = auth_client.get(period_detail_url(summary.pk))

        assert list_response.status_code == status.HTTP_200_OK
        assert detail_response.status_code == status.HTTP_200_OK

        for response in (list_response, detail_response):
            body = str(response.content)
            assert "reconciliation" not in body
            assert "unmetered" not in body
            assert "orphaned" not in body


@pytest.mark.django_db
class TestDetailPrefetchesResources:
    """Query-count gate proving ``resources`` is prefetched on detail -- a
    statement is a bounded number of queries, never one per resource row."""

    def test_query_count_does_not_scale_with_resource_row_count(
        self, auth_client, child_admin_membership, root, subscription
    ):
        summary = make_summary(
            organization=root,
            subscription=subscription,
            billing_period_start=datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC),
        )
        resource_keys = [
            "organization_members",
            "resource_calendars",
            "calendar_groups",
            "bundle_calendars",
            "availability_windows",
            "webhook_subscriptions",
            "public_api_system_users",
            "event_occurrences",
        ]
        for resource_key in resource_keys:
            baker.make(BillingPeriodResourceUsage, summary=summary, resource_key=resource_key)

        with CaptureQueriesContext(connection) as captured:
            response = auth_client.get(period_detail_url(summary.pk))

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data["resources"]) == len(resource_keys)

        resource_usage_queries = [
            query
            for query in captured.captured_queries
            if "payments_billingperiodresourceusage" in query["sql"]
        ]
        # Exactly one query fetches every resource row -- the prefetch -- not
        # one per row (which would scale with `len(resource_keys)`).
        assert len(resource_usage_queries) == 1


@pytest.mark.django_db
class TestDetailByOrganizationAttribution:
    """``by_organization`` on the detail action mirrors ``GET
    /billing/usage/``'s ``UsageByOrganizationSerializer`` shape -- a list of
    ``{organization_id, name, usage}``, names batch-resolved in one extra
    query regardless of how many organizations or resource rows contributed.
    """

    def test_query_count_does_not_scale_with_resource_rows_or_organizations(
        self, auth_client, child_admin_membership, root, child, subscription
    ):
        # `AS "pk"` + `AS "name"` together uniquely identify the batched
        # `Organization.objects.filter(pk__in=...).values_list("pk", "name")`
        # lookup -- distinct from the (several) other organization-table
        # queries `TenantScopedViewMixin`/pool resolution already issue for
        # every request in this module, which this test must not conflate
        # with the fix's added query.
        def organization_name_lookup_queries(captured):
            return [
                query
                for query in captured.captured_queries
                if 'AS "pk"' in query["sql"] and 'AS "name"' in query["sql"]
            ]

        summary = make_summary(
            organization=root,
            subscription=subscription,
            billing_period_start=datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC),
        )
        baker.make(
            BillingPeriodResourceUsage,
            summary=summary,
            resource_key="organization_members",
            total=2,
            by_organization={str(root.pk): 2},
        )

        with CaptureQueriesContext(connection) as captured_one_row_one_org:
            small_response = auth_client.get(period_detail_url(summary.pk))

        assert small_response.status_code == status.HTTP_200_OK
        small_queries = organization_name_lookup_queries(captured_one_row_one_org)
        assert len(small_queries) == 1

        resource_keys = [
            "resource_calendars",
            "calendar_groups",
            "bundle_calendars",
            "availability_windows",
            "webhook_subscriptions",
            "public_api_system_users",
            "event_occurrences",
        ]
        # Every additional resource row attributes usage across both pooled
        # organizations -- the organization-pk union spans the whole pool on
        # every row, not just one row's worth.
        for resource_key in resource_keys:
            baker.make(
                BillingPeriodResourceUsage,
                summary=summary,
                resource_key=resource_key,
                total=3,
                by_organization={str(root.pk): 2, str(child.pk): 1},
            )

        with CaptureQueriesContext(connection) as captured_eight_rows_two_orgs:
            large_response = auth_client.get(period_detail_url(summary.pk))

        assert large_response.status_code == status.HTTP_200_OK
        assert len(large_response.data["resources"]) == 1 + len(resource_keys)
        large_queries = organization_name_lookup_queries(captured_eight_rows_two_orgs)
        # Exactly one query resolves organization names for the whole
        # response, in both cases -- the batched `pk__in=...` lookup -- not
        # one per resource row (1 -> 8) and not one per organization
        # referenced (1 -> 2).
        assert len(large_queries) == 1
        assert len(large_queries) == len(small_queries)
        # Total query count is identical between the two requests: the
        # per-resource-row/per-organization growth is purely in the payload,
        # never in query count.
        assert len(captured_eight_rows_two_orgs.captured_queries) == len(
            captured_one_row_one_org.captured_queries
        )

    def test_unknown_organization_renders_blank_name_and_still_counts_toward_total(
        self, auth_client, child_admin_membership, root, subscription
    ):
        deleted_organization_pk = 999_999
        summary = make_summary(
            organization=root,
            subscription=subscription,
            billing_period_start=datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC),
        )
        baker.make(
            BillingPeriodResourceUsage,
            summary=summary,
            resource_key="organization_members",
            total=6,
            by_organization={str(root.pk): 4, str(deleted_organization_pk): 2},
        )

        response = auth_client.get(period_detail_url(summary.pk))

        assert response.status_code == status.HTTP_200_OK
        row = next(
            row
            for row in response.data["resources"]
            if row["resource_key"] == "organization_members"
        )
        by_organization = {entry["organization_id"]: entry for entry in row["by_organization"]}
        assert by_organization[root.pk] == {
            "organization_id": root.pk,
            "name": root.name,
            "usage": 4,
        }
        # The no-longer-existing organization is not dropped -- its count
        # still counts toward `total` -- it just has no resolvable name.
        assert by_organization[deleted_organization_pk] == {
            "organization_id": deleted_organization_pk,
            "name": "",
            "usage": 2,
        }
        assert row["total"] == sum(entry["usage"] for entry in row["by_organization"])
