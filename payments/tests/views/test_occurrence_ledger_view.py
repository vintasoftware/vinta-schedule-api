"""Integration tests for ``GET /billing/usage/occurrences/`` -- the line-item
ledger behind post-paid charges (``MeteredOccurrenceViewSet``). This is the
one endpoint in the billing-usage surface gated by ``IsBillingOwnerOrAdmin``
rather than the bare ``IsAuthenticated`` every other read in this module uses
(see the plan's Guiding Decisions): a ledger row carries an ``event_id`` and
an exact ``occurrence_start``, which is calendar content that can span
calendars the caller has no membership scope on.

``provision_default_subscription`` (root conftest, autouse) gives every
``Organization`` created here a ``Subscription`` on the seeded ``unlimited``
plan -- reused directly (``organization.subscription``) rather than building a
second one, which would raise on the ``Subscription.organization``
``OneToOneField``.
"""

import datetime
from decimal import Decimal
from uuid import uuid4

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from calendar_integration.factories import create_calendar_ownership
from calendar_integration.models import Calendar, CalendarEvent
from organizations.models import Organization, OrganizationMembership, OrganizationRole
from payments.models import MeteredOccurrence
from payments.pagination import LargeLimitOffsetPagination
from payments.services.subscription_service import current_billing_period_start


def occurrences_url() -> str:
    return reverse("api:BillingUsageOccurrence-list")


def make_occurrence(
    *,
    organization: Organization,
    subscription,
    event_id: int,
    occurrence_start: datetime.datetime,
    billing_period_start: datetime.datetime,
    is_within_allowance: bool = True,
    unit_price: str = "0.0000",
) -> MeteredOccurrence:
    return baker.make(
        MeteredOccurrence,
        organization=organization,
        subscription=subscription,
        event_id=event_id,
        occurrence_start=occurrence_start,
        billing_period_start=billing_period_start,
        is_within_allowance=is_within_allowance,
        unit_price=Decimal(unit_price),
    )


def make_event(
    *, organization: Organization, calendar: Calendar, title: str = "Weekly standup"
) -> CalendarEvent:
    return baker.make(
        CalendarEvent,
        organization=organization,
        calendar_fk=calendar,
        title=title,
        external_id=f"event-{uuid4()}",
        start_time_tz_unaware=datetime.datetime(2026, 8, 3, 14, 0),
        end_time_tz_unaware=datetime.datetime(2026, 8, 3, 14, 30),
        timezone="UTC",
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
def admin_membership(user, root: Organization):
    return baker.make(
        OrganizationMembership,
        user=user,
        organization=root,
        role=OrganizationRole.ADMIN,
        is_active=True,
    )


@pytest.fixture
def billing_owner_membership(user, root: Organization):
    return baker.make(
        OrganizationMembership,
        user=user,
        organization=root,
        role=OrganizationRole.MEMBER,
        is_active=True,
        is_billing_owner=True,
    )


@pytest.fixture
def plain_member_membership(user, root: Organization):
    return baker.make(
        OrganizationMembership,
        user=user,
        organization=root,
        role=OrganizationRole.MEMBER,
        is_active=True,
        is_billing_owner=False,
    )


@pytest.mark.django_db
class TestPermissions:
    def test_plain_member_is_forbidden(
        self, auth_client, plain_member_membership, root, subscription
    ):
        response = auth_client.get(occurrences_url())

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_billing_owner_gets_200(
        self, auth_client, billing_owner_membership, root, subscription
    ):
        response = auth_client.get(occurrences_url())

        assert response.status_code == status.HTTP_200_OK

    def test_admin_gets_200(self, auth_client, admin_membership, root, subscription):
        response = auth_client.get(occurrences_url())

        assert response.status_code == status.HTTP_200_OK

    def test_root_admin_sees_a_pooled_descendants_rows(
        self, auth_client, admin_membership, root, child, subscription
    ):
        """The caller is ADMIN of the *reseller root* itself (``root``, which
        ``can_invite_organizations``) -- the same role ``IsBillingOwnerOrAdmin``
        grants object-level access to via its first branch. Because the pool
        resolved for that root includes every descendant, the ledger it can
        page through spans a descendant (``child``) it does not hold a direct
        membership in -- an "acting reseller root manages a descendant's
        ledger" outcome, driven by pooling rather than the caller ever setting
        ``X-Organization-Id`` to the descendant itself. Note: the
        ``_acting_reseller_root_permits`` permission branch is not what grants
        this access; pooling is the sole mechanism."""
        billing_period_start = current_billing_period_start(subscription)
        make_occurrence(
            organization=child,
            subscription=subscription,
            event_id=1,
            occurrence_start=datetime.datetime(2026, 8, 3, tzinfo=datetime.UTC),
            billing_period_start=billing_period_start,
        )

        response = auth_client.get(occurrences_url())

        assert response.status_code == status.HTTP_200_OK
        assert response.data["count"] == 1
        assert response.data["results"][0]["organization"]["id"] == child.pk


@pytest.mark.django_db
class TestPeriodScoping:
    def test_no_filter_covers_exactly_the_current_period(
        self, auth_client, admin_membership, root, subscription
    ):
        current_period = current_billing_period_start(subscription)
        past_period = current_period - datetime.timedelta(days=31)
        current_row = make_occurrence(
            organization=root,
            subscription=subscription,
            event_id=1,
            occurrence_start=datetime.datetime(2026, 8, 3, tzinfo=datetime.UTC),
            billing_period_start=current_period,
        )
        make_occurrence(
            organization=root,
            subscription=subscription,
            event_id=2,
            occurrence_start=datetime.datetime(2026, 7, 3, tzinfo=datetime.UTC),
            billing_period_start=past_period,
        )

        response = auth_client.get(occurrences_url())

        assert response.status_code == status.HTTP_200_OK
        returned_ids = [row["id"] for row in response.data["results"]]
        assert returned_ids == [current_row.pk]

    def test_explicit_billing_period_start_covers_that_closed_period(
        self, auth_client, admin_membership, root, subscription
    ):
        current_period = current_billing_period_start(subscription)
        past_period = current_period - datetime.timedelta(days=31)
        make_occurrence(
            organization=root,
            subscription=subscription,
            event_id=1,
            occurrence_start=datetime.datetime(2026, 8, 3, tzinfo=datetime.UTC),
            billing_period_start=current_period,
        )
        past_row = make_occurrence(
            organization=root,
            subscription=subscription,
            event_id=2,
            occurrence_start=datetime.datetime(2026, 7, 3, tzinfo=datetime.UTC),
            billing_period_start=past_period,
        )

        response = auth_client.get(
            occurrences_url(), {"billing_period_start": past_period.isoformat()}
        )

        assert response.status_code == status.HTTP_200_OK
        returned_ids = [row["id"] for row in response.data["results"]]
        assert returned_ids == [past_row.pk]


@pytest.mark.django_db
class TestOverageTiesToTheMoney:
    def test_is_within_allowance_false_sums_to_the_periods_overage_total(
        self, auth_client, admin_membership, root, subscription
    ):
        """The assertion that ties the ledger to the money: the rows returned
        by ``is_within_allowance=false`` are exactly the rows the period's
        ``overage_total`` (``MeteredOccurrenceQuerySet.overage_total()``) was
        summed from -- not a superset, not a subset."""
        billing_period_start = current_billing_period_start(subscription)
        for i in range(3):
            make_occurrence(
                organization=root,
                subscription=subscription,
                event_id=100 + i,
                occurrence_start=datetime.datetime(2026, 8, 3, tzinfo=datetime.UTC)
                + datetime.timedelta(hours=i),
                billing_period_start=billing_period_start,
                is_within_allowance=True,
                unit_price="0.0000",
            )
        overage_rows = [
            make_occurrence(
                organization=root,
                subscription=subscription,
                event_id=200 + i,
                occurrence_start=datetime.datetime(2026, 8, 3, tzinfo=datetime.UTC)
                + datetime.timedelta(hours=10 + i),
                billing_period_start=billing_period_start,
                is_within_allowance=False,
                unit_price="0.0100",
            )
            for i in range(4)
        ]

        expected_overage_total = (
            MeteredOccurrence.objects.for_billing_period(subscription.pk, billing_period_start)
            .for_organizations([root.pk])
            .overage_total()
        )
        assert expected_overage_total == Decimal("0.0400")

        response = auth_client.get(occurrences_url(), {"is_within_allowance": "false"})

        assert response.status_code == status.HTTP_200_OK
        returned_ids = {row["id"] for row in response.data["results"]}
        assert returned_ids == {row.pk for row in overage_rows}
        summed = sum(Decimal(row["unit_price"]) for row in response.data["results"])
        assert summed == expected_overage_total


@pytest.mark.django_db
class TestOrganizationFilterValidatesPoolMembership:
    def test_organization_outside_the_pool_is_a_validation_error_not_an_empty_200(
        self, auth_client, admin_membership, root, subscription
    ):
        outside_organization = baker.make(Organization, parent=None, can_invite_organizations=True)

        response = auth_client.get(occurrences_url(), {"organization": outside_organization.pk})

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "organization" in response.data

    def test_organization_inside_the_pool_narrows_normally(
        self, auth_client, admin_membership, root, child, subscription
    ):
        billing_period_start = current_billing_period_start(subscription)
        root_row = make_occurrence(
            organization=root,
            subscription=subscription,
            event_id=1,
            occurrence_start=datetime.datetime(2026, 8, 3, tzinfo=datetime.UTC),
            billing_period_start=billing_period_start,
        )
        make_occurrence(
            organization=child,
            subscription=subscription,
            event_id=2,
            occurrence_start=datetime.datetime(2026, 8, 3, tzinfo=datetime.UTC),
            billing_period_start=billing_period_start,
        )

        response = auth_client.get(occurrences_url(), {"organization": root.pk})

        assert response.status_code == status.HTTP_200_OK
        returned_ids = [row["id"] for row in response.data["results"]]
        assert returned_ids == [root_row.pk]


@pytest.mark.django_db
class TestEventEnrichment:
    def test_row_with_a_live_event_reports_title_calendar_and_owners(
        self, auth_client, admin_membership, root, subscription
    ):
        from users.factories import UserFactory

        owner = UserFactory().create_user(first_name="Dana", last_name="Reyes")
        calendar = baker.make(Calendar, organization=root, name="Team")
        create_calendar_ownership(calendar=calendar, user=owner)
        event = make_event(organization=root, calendar=calendar, title="Weekly standup")
        billing_period_start = current_billing_period_start(subscription)
        make_occurrence(
            organization=root,
            subscription=subscription,
            event_id=event.pk,
            occurrence_start=datetime.datetime(2026, 8, 3, 14, tzinfo=datetime.UTC),
            billing_period_start=billing_period_start,
            unit_price="0.0100",
        )

        response = auth_client.get(occurrences_url())

        assert response.status_code == status.HTTP_200_OK
        row = response.data["results"][0]
        assert row["event"]["id"] == event.pk
        assert row["event"]["title"] == "Weekly standup"
        assert row["event"]["calendar"] == {"id": calendar.pk, "name": "Team"}
        assert row["event"]["owners"] == [{"user_id": owner.pk, "name": "Dana Reyes"}]

    def test_row_with_a_deleted_event_serializes_event_null_with_unit_price_intact(
        self, auth_client, admin_membership, root, subscription
    ):
        billing_period_start = current_billing_period_start(subscription)
        deleted_event_row = make_occurrence(
            organization=root,
            subscription=subscription,
            # No CalendarEvent exists with this id -- models this as "the event
            # was deleted after being metered", per the model's soft-reference
            # docstring.
            event_id=999_999_999,
            occurrence_start=datetime.datetime(2026, 8, 3, tzinfo=datetime.UTC),
            billing_period_start=billing_period_start,
            is_within_allowance=False,
            unit_price="0.0250",
        )

        response = auth_client.get(occurrences_url())

        assert response.status_code == status.HTTP_200_OK
        row = response.data["results"][0]
        assert row["id"] == deleted_event_row.pk
        assert row["event"] is None
        assert Decimal(row["unit_price"]) == Decimal("0.0250")


@pytest.mark.django_db
class TestBatchedQueryCount:
    """Query-count gate proving event/calendar/owner resolution is batched --
    a constant number of queries per page, never one per row."""

    def _make_rows_with_events(self, *, organization, subscription, count: int):
        billing_period_start = current_billing_period_start(subscription)
        for i in range(count):
            calendar = baker.make(
                Calendar,
                organization=organization,
                name=f"Calendar {i}",
                external_id=f"query-count-{uuid4()}",
            )
            event = make_event(organization=organization, calendar=calendar, title=f"Event {i}")
            make_occurrence(
                organization=organization,
                subscription=subscription,
                event_id=event.pk,
                occurrence_start=datetime.datetime(2026, 8, 3, tzinfo=datetime.UTC)
                + datetime.timedelta(hours=i),
                billing_period_start=billing_period_start,
            )

    def test_query_count_is_constant_across_page_sizes(
        self, auth_client, admin_membership, root, subscription
    ):
        self._make_rows_with_events(organization=root, subscription=subscription, count=1)

        with CaptureQueriesContext(connection) as captured_one_row:
            one_row_response = auth_client.get(occurrences_url(), {"limit": 1})

        assert one_row_response.status_code == status.HTTP_200_OK
        assert len(one_row_response.data["results"]) == 1

        self._make_rows_with_events(organization=root, subscription=subscription, count=49)

        with CaptureQueriesContext(connection) as captured_fifty_rows:
            fifty_row_response = auth_client.get(occurrences_url(), {"limit": 50})

        assert fifty_row_response.status_code == status.HTTP_200_OK
        assert len(fifty_row_response.data["results"]) == 50

        # Query count does not scale with the number of rows on the page --
        # event, calendar, and owner resolution is batched, not per-row.
        assert len(captured_fifty_rows.captured_queries) == len(captured_one_row.captured_queries)


@pytest.mark.django_db
class TestPaginationLimitIsClamped:
    def test_limit_above_max_limit_is_clamped_to_1000(self):
        factory = APIRequestFactory()
        django_request = factory.get(occurrences_url(), {"limit": "5000"})
        request = Request(django_request)

        paginator = LargeLimitOffsetPagination()
        limit = paginator.get_limit(request)

        assert limit == 1000
