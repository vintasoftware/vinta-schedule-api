"""Nested aggregates cost one query per level, not one per parent.

This is the load-bearing file for Phase 7. The failure it exists to catch is
a silent N+1: every number comes back correct, every functional test passes,
and the only symptom is database load that grows with the size of whatever
list the aggregate hangs under. So the assertions here are about *counts*, and
they are written as an equality between five parents and twenty-five rather
than a fixed number -- a fixed number can be raised until it passes, an
equality cannot.

The other half is agreement: the batched path and the root-level path have to
return the same numbers for the same rows, or the batching would be a second
implementation of the aggregation with its own bugs.
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


_EVENT_TABLE = "calendar_integration_calendarevent"


def _count_data_queries(ctx: CaptureQueriesContext, table: str) -> int:
    """How many captured statements actually read ``table``.

    Same helper, same reasoning, as ``test_aggregate_queries.py``: an
    authenticated GraphQL request pays a fixed cost in auth and permission
    lookups that names no aggregated table, and the statement-timeout guard
    adds its own ``SAVEPOINT`` bookkeeping. Filtering on the table isolates
    the query this file is about.
    """
    return sum(1 for query in ctx.captured_queries if table in query["sql"])


@pytest.mark.django_db
class TestNestedAggregateBatching:
    def setup_method(self):
        self.client = APIClient()

    # -- fixtures ------------------------------------------------------

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

    def _make_event(self, org, calendar, *, day: int, start_hour: int, end_hour: int, title: str):
        return baker.make(
            "calendar_integration.CalendarEvent",
            organization=org,
            calendar=calendar,
            start_time_tz_unaware=datetime.datetime(2026, 1, day, start_hour, 0, 0),
            end_time_tz_unaware=datetime.datetime(2026, 1, day, end_hour, 0, 0),
            timezone="UTC",
            external_id=f"e-{uuid.uuid4().hex[:8]}",
            title=title,
        )

    def _token(self, org: Organization, *resources: str):
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"orgwide_{uuid.uuid4().hex[:8]}", organization=org
        )
        for resource in resources:
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

    def _world(self, org, calendar_count: int) -> list[Calendar]:
        """``calendar_count`` calendars, each carrying two one-hour events."""
        calendars = []
        for _ in range(calendar_count):
            calendar = self._make_calendar(org)
            self._make_event(org, calendar, day=5, start_hour=10, end_hour=11, title="A")
            self._make_event(org, calendar, day=6, start_hour=10, end_hour=12, title="B")
            calendars.append(calendar)
        return calendars

    # -- the documents -------------------------------------------------

    NESTED_QUERY = """
    query Nested(
        $filter: CalendarEventAggregateFilterInput!
        $groupBy: [CalendarEventGroupByInput!]!
    ) {
        calendars(limit: 100) {
            id
            eventAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC") {
                key { calendarId }
                count
                durationMinutes { sum avg }
            }
        }
    }
    """

    ROOT_QUERY = """
    query Root(
        $filter: CalendarEventAggregateFilterInput!
        $groupBy: [CalendarEventGroupByInput!]!
    ) {
        calendarEventAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC") {
            key { calendarId }
            count
            durationMinutes { sum avg }
        }
    }
    """

    def _nested_variables(self) -> dict:
        return {
            "filter": {**self._bounds(), "calendarId": None},
            "groupBy": [{"field": "CALENDAR_ID"}],
        }

    def _run_nested(self, org, calendar_count: int):
        self._world(org, calendar_count)
        system_user, token, auth = self._token(
            org, PublicAPIResources.CALENDAR, PublicAPIResources.CALENDAR_EVENT
        )
        with CaptureQueriesContext(connection) as ctx:
            response = self._post(
                self.NESTED_QUERY, system_user, token, auth, self._nested_variables()
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload.get("errors", []) == []
        return payload["data"]["calendars"], ctx

    # -- the load-bearing assertions -----------------------------------

    def test_the_event_query_count_does_not_grow_with_the_calendar_count(self):
        """Five calendars and twenty-five cost the same one grouped query.

        Without batching this is 5 versus 25: the nested resolver would run
        once per parent and each run would be its own ``GROUP BY``.
        """
        five_rows, five_ctx = self._run_nested(self._org(), 5)
        twenty_five_rows, twenty_five_ctx = self._run_nested(self._org(), 25)

        assert len(five_rows) == 5
        assert len(twenty_five_rows) == 25

        five_queries = _count_data_queries(five_ctx, _EVENT_TABLE)
        twenty_five_queries = _count_data_queries(twenty_five_ctx, _EVENT_TABLE)

        assert five_queries == twenty_five_queries
        assert twenty_five_queries == 1

    def test_the_whole_document_costs_the_same_at_five_and_at_twenty_five(self):
        """The collector must not defeat ``DjangoOptimizerExtension`` either.

        The event query being constant is only half the claim: if collecting
        the aggregate had cost the parent list its ``prefetch_related``, the
        *total* would still grow with the calendar count while the assertion
        above stayed green. Comparing every captured statement catches that.
        """
        _, five_ctx = self._run_nested(self._org(), 5)
        _, twenty_five_ctx = self._run_nested(self._org(), 25)

        assert len(five_ctx.captured_queries) == len(twenty_five_ctx.captured_queries)

    def test_two_nested_aggregates_under_one_list_are_two_queries_not_fifty(self):
        """Sibling aggregates each get their own batch, and only one each."""
        org = self._org()
        self._world(org, 25)
        system_user, token, auth = self._token(
            org,
            PublicAPIResources.CALENDAR,
            PublicAPIResources.CALENDAR_EVENT,
            PublicAPIResources.BLOCKED_TIME,
        )
        query = """
        query Both(
            $filter: CalendarEventAggregateFilterInput!
            $blockedFilter: BlockedTimeAggregateFilterInput!
            $groupBy: [CalendarEventGroupByInput!]!
            $blockedGroupBy: [BlockedTimeGroupByInput!]!
        ) {
            calendars(limit: 100) {
                id
                eventAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC") {
                    count
                }
                blockedTimeAggregate(
                    filter: $blockedFilter
                    groupBy: $blockedGroupBy
                    timezone: "UTC"
                ) {
                    count
                }
            }
        }
        """
        variables = {
            "filter": {**self._bounds(), "calendarId": None},
            "blockedFilter": {**self._bounds(), "calendarId": None},
            "groupBy": [{"field": "CALENDAR_ID"}],
            "blockedGroupBy": [{"field": "CALENDAR_ID"}],
        }

        with CaptureQueriesContext(connection) as ctx:
            response = self._post(query, system_user, token, auth, variables)

        assert response.status_code == 200
        assert response.json().get("errors", []) == []
        assert _count_data_queries(ctx, _EVENT_TABLE) == 1
        assert _count_data_queries(ctx, "calendar_integration_blockedtime") == 1

    def test_two_aliases_of_the_same_field_do_not_share_a_batch(self):
        """Different arguments under one alias pair must not answer alike.

        One calendar's events are split across two days; asking for each day
        under its own alias is the shape that a level key built from the field
        *name* rather than the response key would collapse into one batch --
        and both aliases would then report the first one's numbers.
        """
        org = self._org()
        calendar = self._make_calendar(org)
        self._make_event(org, calendar, day=5, start_hour=10, end_hour=11, title="A")
        self._make_event(org, calendar, day=12, start_hour=10, end_hour=11, title="B")
        self._make_event(org, calendar, day=12, start_hour=14, end_hour=15, title="C")
        system_user, token, auth = self._token(
            org, PublicAPIResources.CALENDAR, PublicAPIResources.CALENDAR_EVENT
        )
        query = """
        query Aliased(
            $early: CalendarEventAggregateFilterInput!
            $late: CalendarEventAggregateFilterInput!
            $groupBy: [CalendarEventGroupByInput!]!
        ) {
            calendars(limit: 100) {
                id
                early: eventAggregate(filter: $early, groupBy: $groupBy, timezone: "UTC") {
                    count
                }
                late: eventAggregate(filter: $late, groupBy: $groupBy, timezone: "UTC") {
                    count
                }
            }
        }
        """
        variables = {
            "early": {
                "startDatetime": "2026-01-01T00:00:00Z",
                "endDatetime": "2026-01-10T00:00:00Z",
                "calendarId": None,
            },
            "late": {
                "startDatetime": "2026-01-10T00:00:00Z",
                "endDatetime": "2026-01-20T00:00:00Z",
                "calendarId": None,
            },
            "groupBy": [{"field": "CALENDAR_ID"}],
        }

        with CaptureQueriesContext(connection) as ctx:
            response = self._post(query, system_user, token, auth, variables)

        assert response.status_code == 200
        payload = response.json()
        assert payload.get("errors", []) == []
        (row,) = payload["data"]["calendars"]
        assert row["early"][0]["count"] == 1
        assert row["late"][0]["count"] == 2
        # Two levels, so two queries -- the point is that it is not one.
        assert _count_data_queries(ctx, _EVENT_TABLE) == 2

    # -- agreement with the root field ---------------------------------

    def test_nested_and_root_aggregates_over_the_same_filter_agree(self):
        """The batched path is not a second implementation of the numbers."""
        org = self._org()
        calendars = self._world(org, 4)
        system_user, token, auth = self._token(
            org, PublicAPIResources.CALENDAR, PublicAPIResources.CALENDAR_EVENT
        )
        variables = self._nested_variables()

        nested = self._post(self.NESTED_QUERY, system_user, token, auth, variables).json()
        root = self._post(self.ROOT_QUERY, system_user, token, auth, variables).json()

        assert nested.get("errors", []) == []
        assert root.get("errors", []) == []

        nested_by_calendar = {
            calendar["eventAggregate"][0]["key"]["calendarId"]: calendar["eventAggregate"][0]
            for calendar in nested["data"]["calendars"]
        }
        root_by_calendar = {
            row["key"]["calendarId"]: row for row in root["data"]["calendarEventAggregate"]
        }

        assert set(nested_by_calendar) == {calendar.id for calendar in calendars}
        assert nested_by_calendar == root_by_calendar
        # And the numbers are the ones the fixture built: 60 + 120 minutes.
        for row in nested_by_calendar.values():
            assert row["count"] == 2
            assert row["durationMinutes"]["sum"] == pytest.approx(180.0)
            assert row["durationMinutes"]["avg"] == pytest.approx(90.0)

    def test_a_calendar_with_no_events_gets_an_empty_list_not_another_query(self):
        org = self._org()
        busy = self._make_calendar(org)
        self._make_event(org, busy, day=5, start_hour=10, end_hour=11, title="A")
        self._make_calendar(org)
        system_user, token, auth = self._token(
            org, PublicAPIResources.CALENDAR, PublicAPIResources.CALENDAR_EVENT
        )

        with CaptureQueriesContext(connection) as ctx:
            response = self._post(
                self.NESTED_QUERY, system_user, token, auth, self._nested_variables()
            )

        payload = response.json()
        assert payload.get("errors", []) == []
        rows = {len(calendar["eventAggregate"]) for calendar in payload["data"]["calendars"]}
        assert rows == {0, 1}
        assert _count_data_queries(ctx, _EVENT_TABLE) == 1

    # -- the per-parent slice ------------------------------------------

    def test_limit_pages_each_parent_rather_than_the_batch(self):
        """``limit: 1`` gives every calendar one row, not the batch one row.

        A plain ``LIMIT`` over the batched query would hand the first calendar
        its row and leave the other two empty -- correct-looking for one
        parent and silently wrong for the rest.
        """
        org = self._org()
        for _ in range(3):
            calendar = self._make_calendar(org)
            self._make_event(org, calendar, day=5, start_hour=10, end_hour=11, title="A")
            self._make_event(org, calendar, day=6, start_hour=10, end_hour=11, title="B")
        system_user, token, auth = self._token(
            org, PublicAPIResources.CALENDAR, PublicAPIResources.CALENDAR_EVENT
        )
        query = """
        query Paged(
            $filter: CalendarEventAggregateFilterInput!
            $groupBy: [CalendarEventGroupByInput!]!
        ) {
            calendars(limit: 100) {
                id
                eventAggregate(
                    filter: $filter
                    groupBy: $groupBy
                    timezone: "UTC"
                    limit: 1
                ) {
                    key { startTimeBucket }
                    count
                }
            }
        }
        """
        variables = {
            "filter": {**self._bounds(), "calendarId": None},
            "groupBy": [{"temporal": {"field": "START_TIME", "granularity": "DAY"}}],
        }

        with CaptureQueriesContext(connection) as ctx:
            response = self._post(query, system_user, token, auth, variables)

        payload = response.json()
        assert payload.get("errors", []) == []
        calendars = payload["data"]["calendars"]
        assert len(calendars) == 3
        # Every calendar has two day buckets and every calendar gets one of
        # them -- the earlier, because the group-key tiebreak sorts ascending.
        for calendar in calendars:
            assert len(calendar["eventAggregate"]) == 1
            assert calendar["eventAggregate"][0]["key"]["startTimeBucket"].startswith("2026-01-05")
        assert _count_data_queries(ctx, _EVENT_TABLE) == 1

    def test_offset_pages_each_parent_too(self):
        org = self._org()
        for _ in range(2):
            calendar = self._make_calendar(org)
            self._make_event(org, calendar, day=5, start_hour=10, end_hour=11, title="A")
            self._make_event(org, calendar, day=6, start_hour=10, end_hour=11, title="B")
        system_user, token, auth = self._token(
            org, PublicAPIResources.CALENDAR, PublicAPIResources.CALENDAR_EVENT
        )
        query = """
        query Paged(
            $filter: CalendarEventAggregateFilterInput!
            $groupBy: [CalendarEventGroupByInput!]!
        ) {
            calendars(limit: 100) {
                eventAggregate(
                    filter: $filter
                    groupBy: $groupBy
                    timezone: "UTC"
                    limit: 1
                    offset: 1
                ) {
                    key { startTimeBucket }
                }
            }
        }
        """
        variables = {
            "filter": {**self._bounds(), "calendarId": None},
            "groupBy": [{"temporal": {"field": "START_TIME", "granularity": "DAY"}}],
        }

        response = self._post(query, system_user, token, auth, variables)

        payload = response.json()
        assert payload.get("errors", []) == []
        for calendar in payload["data"]["calendars"]:
            assert len(calendar["eventAggregate"]) == 1
            assert calendar["eventAggregate"][0]["key"]["startTimeBucket"].startswith("2026-01-06")

    # -- the other two parents -----------------------------------------

    def test_an_appointment_types_event_aggregate_batches_the_same_way(self):
        org = self._org()
        appointment_types = []
        for _ in range(5):
            appointment_type = baker.make(
                "calendar_integration.AppointmentType",
                organization=org,
                duration=datetime.timedelta(minutes=30),
            )
            calendar = self._make_calendar(org)
            baker.make(
                "calendar_integration.CalendarEvent",
                organization=org,
                calendar=calendar,
                appointment_type=appointment_type,
                start_time_tz_unaware=datetime.datetime(2026, 1, 5, 10, 0, 0),
                end_time_tz_unaware=datetime.datetime(2026, 1, 5, 11, 0, 0),
                timezone="UTC",
                external_id=f"e-{uuid.uuid4().hex[:8]}",
                title="A",
            )
            appointment_types.append(appointment_type)
        system_user, token, auth = self._token(
            org, PublicAPIResources.APPOINTMENT_TYPE, PublicAPIResources.CALENDAR_EVENT
        )
        query = """
        query Nested(
            $filter: CalendarEventAggregateFilterInput!
            $groupBy: [CalendarEventGroupByInput!]!
        ) {
            appointmentTypes(limit: 100) {
                id
                eventAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC") {
                    count
                }
            }
        }
        """
        variables = {
            "filter": {**self._bounds(), "calendarId": None},
            "groupBy": [{"field": "APPOINTMENT_TYPE_ID"}],
        }

        with CaptureQueriesContext(connection) as ctx:
            response = self._post(query, system_user, token, auth, variables)

        payload = response.json()
        assert payload.get("errors", []) == []
        rows = payload["data"]["appointmentTypes"]
        assert len(rows) == 5
        assert all(row["eventAggregate"][0]["count"] == 1 for row in rows)
        assert _count_data_queries(ctx, _EVENT_TABLE) == 1

    def test_a_pools_event_aggregate_counts_every_rostered_calendars_events(self):
        """The pool link reaches events through the roster's through table."""
        org = self._org()
        pool = baker.make("calendar_integration.CalendarPool", organization=org, name="Pool")
        first = self._make_calendar(org)
        second = self._make_calendar(org)
        unrostered = self._make_calendar(org)
        for calendar in (first, second):
            baker.make(
                "calendar_integration.CalendarPoolMembership",
                organization=org,
                pool=pool,
                calendar=calendar,
            )
        self._make_event(org, first, day=5, start_hour=10, end_hour=11, title="A")
        self._make_event(org, second, day=5, start_hour=12, end_hour=13, title="B")
        self._make_event(org, unrostered, day=5, start_hour=14, end_hour=15, title="C")
        system_user, token, auth = self._token(
            org, PublicAPIResources.CALENDAR_POOL, PublicAPIResources.CALENDAR_EVENT
        )
        query = """
        query Nested(
            $filter: CalendarEventAggregateFilterInput!
            $groupBy: [CalendarEventGroupByInput!]!
        ) {
            calendarPools(limit: 100) {
                id
                eventAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC") {
                    count
                }
            }
        }
        """
        variables = {
            "filter": {**self._bounds(), "calendarId": None},
            "groupBy": [{"temporal": {"field": "START_TIME", "granularity": "MONTH"}}],
        }

        with CaptureQueriesContext(connection) as ctx:
            response = self._post(query, system_user, token, auth, variables)

        payload = response.json()
        assert payload.get("errors", []) == []
        (row,) = payload["data"]["calendarPools"]
        # Two rostered calendars, one event each; the third calendar is in no
        # pool and its event belongs to nobody.
        assert row["eventAggregate"][0]["count"] == 2
        assert _count_data_queries(ctx, _EVENT_TABLE) == 1
