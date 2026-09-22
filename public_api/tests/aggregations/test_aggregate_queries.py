"""End-to-end aggregate queries through the schema, for all six entities.

Every test goes through ``/graphql/`` with a real token -- never a bare
resolver call -- so permissions, middleware and the optimizer extension are
all exercised the way a partner would hit them. Three properties matter:

* the numbers are right (``count``, ``sum``/``avg`` on a numeric field);
* a selection-driven metric with arguments -- ``title { concat(separator:
  "; ") } `` -- actually reaches the SQL that produced it, rather than being
  computed with some other, fixed separator;
* the whole document costs exactly one data-fetching query, regardless of
  how many metrics or groups it asks for -- the executor's single
  ``.values().annotate()`` queryset is not defeated by the selection-driven
  metric-building layer on top of it.
"""

import datetime
import uuid

from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import Calendar
from organizations.models import Organization
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


def _count_data_queries(ctx: CaptureQueriesContext, table: str) -> int:
    """How many of the captured statements actually read ``table``.

    Every authenticated GraphQL request pays a fixed, resolver-independent
    cost (system user, organization, billing scope lookups for the
    permission check) before a resolver ever runs, plus the statement-timeout
    guard's own ``SAVEPOINT`` / ``SET LOCAL`` / ``RELEASE SAVEPOINT``
    bookkeeping around the aggregate query itself. None of those name the
    aggregated table, so filtering on it isolates the one query this test is
    actually about: whether the resolver's own data fetch is single-shot.
    """
    return sum(1 for query in ctx.captured_queries if table in query["sql"])


@pytest.mark.django_db
class TestAggregateQueriesEndToEnd:
    def setup_method(self):
        self.client = APIClient()

    def _org(self) -> Organization:
        return baker.make(Organization, name=f"Org {uuid.uuid4().hex[:6]}")

    def _make_calendar(self, org: Organization, **kwargs) -> Calendar:
        unique = uuid.uuid4().hex[:8]
        defaults = {
            "organization": org,
            "name": f"Calendar {unique}",
            "external_id": f"cal-{unique}",
            "provider": CalendarProvider.GOOGLE,
            "calendar_type": CalendarType.PERSONAL,
            "manage_available_windows": True,
        }
        defaults.update(kwargs)
        return Calendar.objects.create(**defaults)

    def _token(self, org: Organization, resource: str):
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"orgwide_{uuid.uuid4().hex[:8]}", organization=org
        )
        baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return system_user, token, auth_service

    def _post(self, query, system_user, token, auth_service, variables):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={"query": query, "variables": variables},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def _bounds(self) -> dict:
        return {
            "startDatetime": "2026-01-01T00:00:00Z",
            "endDatetime": "2026-01-20T00:00:00Z",
        }

    # ------------------------------------------------------------------
    # CalendarEvent -- the worked example, plus the concat and query-count
    # guards.
    # ------------------------------------------------------------------

    CALENDAR_EVENT_QUERY = """
    query Agg($filter: CalendarEventAggregateFilterInput!, $groupBy: [CalendarEventGroupByInput!]!) {
        calendarEventAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC") {
            key { calendarId }
            count
            durationMinutes { sum avg }
            title { concat(separator: "; ") }
        }
    }
    """

    def test_calendar_event_aggregate_grouped_by_calendar(self):
        org = self._org()
        calendar = self._make_calendar(org)
        baker.make(
            "calendar_integration.CalendarEvent",
            organization=org,
            calendar=calendar,
            start_time_tz_unaware=datetime.datetime(2026, 1, 5, 10, 0, 0),
            end_time_tz_unaware=datetime.datetime(2026, 1, 5, 11, 0, 0),
            timezone="UTC",
            external_id=f"e-{uuid.uuid4().hex[:8]}",
            title="Event A",
        )
        baker.make(
            "calendar_integration.CalendarEvent",
            organization=org,
            calendar=calendar,
            start_time_tz_unaware=datetime.datetime(2026, 1, 5, 12, 0, 0),
            end_time_tz_unaware=datetime.datetime(2026, 1, 5, 13, 30, 0),
            timezone="UTC",
            external_id=f"e-{uuid.uuid4().hex[:8]}",
            title="Event B",
        )
        system_user, token, auth = self._token(org, PublicAPIResources.CALENDAR_EVENT)

        variables = {
            "filter": {**self._bounds(), "calendarId": None},
            "groupBy": [{"field": "CALENDAR_ID"}],
        }

        with CaptureQueriesContext(connection) as ctx:
            response = self._post(self.CALENDAR_EVENT_QUERY, system_user, token, auth, variables)

        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        rows = data["data"]["calendarEventAggregate"]
        assert len(rows) == 1
        row = rows[0]
        assert row["key"]["calendarId"] == calendar.id
        assert row["count"] == 2
        # 60 minutes + 90 minutes.
        assert row["durationMinutes"]["sum"] == pytest.approx(150.0)
        assert row["durationMinutes"]["avg"] == pytest.approx(75.0)
        # StringAgg orders by the aggregated value itself, so "Event A"
        # sorts before "Event B" regardless of insertion order.
        assert row["title"]["concat"] == "Event A; Event B"

        assert _count_data_queries(ctx, "calendar_integration_calendarevent") == 1

    def test_calendar_event_aggregate_separator_argument_reaches_sql(self):
        """A different separator produces a different string -- proof the
        argument was read off the selection and baked into the query,
        not resolved after the fact against a fixed-separator value."""
        org = self._org()
        calendar = self._make_calendar(org)
        for label in ("Alpha", "Bravo"):
            baker.make(
                "calendar_integration.CalendarEvent",
                organization=org,
                calendar=calendar,
                start_time_tz_unaware=datetime.datetime(2026, 1, 5, 10, 0, 0),
                end_time_tz_unaware=datetime.datetime(2026, 1, 5, 11, 0, 0),
                timezone="UTC",
                external_id=f"e-{uuid.uuid4().hex[:8]}",
                title=label,
            )
        system_user, token, auth = self._token(org, PublicAPIResources.CALENDAR_EVENT)

        query = """
        query Agg($filter: CalendarEventAggregateFilterInput!, $groupBy: [CalendarEventGroupByInput!]!) {
            calendarEventAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC") {
                title { concat(separator: " | ") }
            }
        }
        """
        variables = {
            "filter": {**self._bounds(), "calendarId": None},
            "groupBy": [{"field": "CALENDAR_ID"}],
        }
        response = self._post(query, system_user, token, auth, variables)
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        assert data["data"]["calendarEventAggregate"][0]["title"]["concat"] == "Alpha | Bravo"

    def test_a_metric_selected_twice_through_overlapping_fragments_still_resolves(self):
        """GraphQL merges fields with the same response key, so the same
        metric operation can legitimately arrive twice -- the shape codegen
        (Apollo/Relay) emits for two overlapping fragments. An identical
        repeat must be collapsed to one metric rather than tripping the
        engine's own duplicate-alias guard."""
        org = self._org()
        calendar = self._make_calendar(org)
        baker.make(
            "calendar_integration.CalendarEvent",
            organization=org,
            calendar=calendar,
            start_time_tz_unaware=datetime.datetime(2026, 1, 5, 10, 0, 0),
            end_time_tz_unaware=datetime.datetime(2026, 1, 5, 11, 0, 0),
            timezone="UTC",
            external_id=f"e-{uuid.uuid4().hex[:8]}",
            title="Event A",
        )
        system_user, token, auth = self._token(org, PublicAPIResources.CALENDAR_EVENT)

        query = """
        query Agg($filter: CalendarEventAggregateFilterInput!, $groupBy: [CalendarEventGroupByInput!]!) {
            calendarEventAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC") {
                ...FragA
                ...FragB
            }
        }
        fragment FragA on CalendarEventAggregateRow {
            durationMinutes { sum }
        }
        fragment FragB on CalendarEventAggregateRow {
            durationMinutes { avg sum }
        }
        """
        variables = {
            "filter": {**self._bounds(), "calendarId": None},
            "groupBy": [{"field": "CALENDAR_ID"}],
        }
        response = self._post(query, system_user, token, auth, variables)
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        row = data["data"]["calendarEventAggregate"][0]
        assert row["durationMinutes"]["sum"] == pytest.approx(60.0)
        assert row["durationMinutes"]["avg"] == pytest.approx(60.0)

    def test_the_same_metric_with_conflicting_arguments_is_refused(self):
        """``title { concat(separator: ";") }`` and ``title { concat(separator:
        "|") }`` in one document would both compute under the same alias --
        silently keeping one caller's separator for both is wrong data with
        no error, so this must be refused instead."""
        org = self._org()
        calendar = self._make_calendar(org)
        baker.make(
            "calendar_integration.CalendarEvent",
            organization=org,
            calendar=calendar,
            start_time_tz_unaware=datetime.datetime(2026, 1, 5, 10, 0, 0),
            end_time_tz_unaware=datetime.datetime(2026, 1, 5, 11, 0, 0),
            timezone="UTC",
            external_id=f"e-{uuid.uuid4().hex[:8]}",
            title="Event A",
        )
        system_user, token, auth = self._token(org, PublicAPIResources.CALENDAR_EVENT)

        query = """
        query Agg($filter: CalendarEventAggregateFilterInput!, $groupBy: [CalendarEventGroupByInput!]!) {
            calendarEventAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC") {
                ...FragA
                ...FragB
            }
        }
        fragment FragA on CalendarEventAggregateRow {
            title { concat(separator: "; ") }
        }
        fragment FragB on CalendarEventAggregateRow {
            title { concat(separator: " | ") }
        }
        """
        variables = {
            "filter": {**self._bounds(), "calendarId": None},
            "groupBy": [{"field": "CALENDAR_ID"}],
        }
        response = self._post(query, system_user, token, auth, variables)
        assert response.status_code == 200
        data = response.json()
        errors = data.get("errors") or []
        assert errors, "expected a refusal, not a silently picked separator"
        assert data.get("data") in (None, {})

    # ------------------------------------------------------------------
    # The other five entities -- correctness, not exhaustive coverage.
    # ------------------------------------------------------------------

    def test_available_time_aggregate_grouped_by_calendar(self):
        org = self._org()
        calendar = self._make_calendar(org)
        baker.make(
            "calendar_integration.AvailableTime",
            organization=org,
            calendar=calendar,
            start_time_tz_unaware=datetime.datetime(2026, 1, 5, 9, 0, 0),
            end_time_tz_unaware=datetime.datetime(2026, 1, 5, 10, 0, 0),
            timezone="UTC",
        )
        system_user, token, auth = self._token(org, PublicAPIResources.AVAILABLE_TIME)

        query = """
        query Agg($filter: AvailableTimeAggregateFilterInput!, $groupBy: [AvailableTimeGroupByInput!]!) {
            availableTimeAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC") {
                key { calendarId }
                count
                durationMinutes { sum }
            }
        }
        """
        variables = {
            "filter": {**self._bounds(), "calendarId": None},
            "groupBy": [{"field": "CALENDAR_ID"}],
        }
        response = self._post(query, system_user, token, auth, variables)
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        rows = data["data"]["availableTimeAggregate"]
        assert len(rows) == 1
        assert rows[0]["key"]["calendarId"] == calendar.id
        assert rows[0]["count"] == 1
        assert rows[0]["durationMinutes"]["sum"] == pytest.approx(60.0)

    def test_blocked_time_aggregate_grouped_by_calendar(self):
        org = self._org()
        calendar = self._make_calendar(org)
        baker.make(
            "calendar_integration.BlockedTime",
            organization=org,
            calendar=calendar,
            start_time_tz_unaware=datetime.datetime(2026, 1, 5, 9, 0, 0),
            end_time_tz_unaware=datetime.datetime(2026, 1, 5, 9, 30, 0),
            timezone="UTC",
            reason="Maintenance",
        )
        system_user, token, auth = self._token(org, PublicAPIResources.BLOCKED_TIME)

        query = """
        query Agg($filter: BlockedTimeAggregateFilterInput!, $groupBy: [BlockedTimeGroupByInput!]!) {
            blockedTimeAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC") {
                key { calendarId }
                count
                reason { concat(separator: ", ") }
            }
        }
        """
        variables = {
            "filter": {**self._bounds(), "calendarId": None},
            "groupBy": [{"field": "CALENDAR_ID"}],
        }
        response = self._post(query, system_user, token, auth, variables)
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        rows = data["data"]["blockedTimeAggregate"]
        assert len(rows) == 1
        assert rows[0]["count"] == 1
        assert rows[0]["reason"]["concat"] == "Maintenance"

    def test_appointment_type_aggregate_grouped_by_public_scheduling(self):
        org = self._org()
        baker.make(
            "calendar_integration.AppointmentType",
            organization=org,
            accepts_public_scheduling=True,
            duration=datetime.timedelta(minutes=30),
        )
        baker.make(
            "calendar_integration.AppointmentType",
            organization=org,
            accepts_public_scheduling=True,
            duration=datetime.timedelta(minutes=45),
        )
        system_user, token, auth = self._token(org, PublicAPIResources.APPOINTMENT_TYPE)

        query = """
        query Agg(
            $filter: AppointmentTypeAggregateFilterInput!
            $groupBy: [AppointmentTypeGroupByInput!]!
        ) {
            appointmentTypeAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC") {
                key { acceptsPublicScheduling }
                count
                durationMinutes { sum }
            }
        }
        """
        variables = {
            "filter": {"acceptsPublicScheduling": True},
            "groupBy": [{"field": "ACCEPTS_PUBLIC_SCHEDULING"}],
        }
        response = self._post(query, system_user, token, auth, variables)
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        rows = data["data"]["appointmentTypeAggregate"]
        assert len(rows) == 1
        assert rows[0]["key"]["acceptsPublicScheduling"] is True
        assert rows[0]["count"] == 2
        assert rows[0]["durationMinutes"]["sum"] == pytest.approx(75.0)

    def test_calendar_aggregate_grouped_by_provider(self):
        org = self._org()
        self._make_calendar(org, provider=CalendarProvider.GOOGLE, capacity=2)
        self._make_calendar(org, provider=CalendarProvider.GOOGLE, capacity=4)
        system_user, token, auth = self._token(org, PublicAPIResources.CALENDAR)

        query = """
        query Agg($filter: CalendarAggregateFilterInput!, $groupBy: [CalendarGroupByInput!]!) {
            calendarAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC") {
                key { provider }
                count
                capacity { sum }
            }
        }
        """
        variables = {"filter": {}, "groupBy": [{"field": "PROVIDER"}]}
        response = self._post(query, system_user, token, auth, variables)
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        rows = data["data"]["calendarAggregate"]
        assert len(rows) == 1
        assert rows[0]["key"]["provider"] == CalendarProvider.GOOGLE.value
        assert rows[0]["count"] == 2
        assert rows[0]["capacity"]["sum"] == pytest.approx(6.0)

    def test_calendar_pool_aggregate_grouped_by_id(self):
        org = self._org()
        baker.make("calendar_integration.CalendarPool", organization=org, name="Nurses")
        system_user, token, auth = self._token(org, PublicAPIResources.CALENDAR_POOL)

        query = """
        query Agg($filter: CalendarPoolAggregateFilterInput!, $groupBy: [CalendarPoolGroupByInput!]!) {
            calendarPoolAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC") {
                key { id }
                count
                name { concat(separator: ", ") }
            }
        }
        """
        variables = {"filter": {}, "groupBy": [{"field": "ID"}]}
        response = self._post(query, system_user, token, auth, variables)
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        rows = data["data"]["calendarPoolAggregate"]
        assert len(rows) == 1
        assert rows[0]["count"] == 1
        assert rows[0]["name"]["concat"] == "Nurses"
