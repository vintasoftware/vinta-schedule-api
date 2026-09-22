"""Window functions, exercised end-to-end through the schema.

Every number here came out of Postgres. That is the property under test, not
a detail of how it was produced: a resolver that looped the executor's rows in
Python and accumulated a running total would satisfy every value assertion in
this file, and it is exactly what the plan rules out. So each test also
asserts that the emitted SQL carries an ``OVER (`` clause and that the whole
document still costs one data-fetching query.

The moving average is asserted against literal hand-computed values rather
than against a second implementation of the same arithmetic -- a frame that is
off by one row returns a plausible-looking number, and only a number worked
out by hand catches it.
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


CALENDAR_EVENT_WINDOW_QUERY = """
query Agg(
    $filter: CalendarEventAggregateFilterInput!
    $groupBy: [CalendarEventGroupByInput!]!
    $window: CalendarEventWindowInput
    $orderBy: [CalendarEventAggregateOrderInput!]
    $limit: Int = 100
) {
    calendarEventAggregate(
        filter: $filter
        groupBy: $groupBy
        timezone: "UTC"
        window: $window
        orderBy: $orderBy
        limit: $limit
    ) {
        key { startTimeBucket calendarId }
        count
        window { runningTotal movingAverage rank percentOfTotal }
    }
}
"""

#: The same document without a ``window`` argument or sub-selection, for the
#: regression case: a query that does not ask for a window must come back
#: exactly as it did before this phase existed.
CALENDAR_EVENT_PLAIN_QUERY = """
query Agg(
    $filter: CalendarEventAggregateFilterInput!
    $groupBy: [CalendarEventGroupByInput!]!
) {
    calendarEventAggregate(
        filter: $filter
        groupBy: $groupBy
        timezone: "UTC"
    ) {
        key { startTimeBucket calendarId }
        count
    }
}
"""

BY_DAY = [{"temporal": {"field": "START_TIME", "granularity": "DAY"}}]


def _over_clause(sql: str, function_prefix: str) -> str:
    """The text inside the ``OVER (...)`` of the first call to ``function_prefix``.

    Scanned with a depth counter rather than matched with a regex: a window's
    ordering holds ``DATE_TRUNC(...)`` calls of its own, so the first ``)``
    after the clause opens is not the end of it.
    """
    start = sql.index(function_prefix)
    opened = sql.index("OVER (", start) + len("OVER (")
    depth = 1
    for index in range(opened, len(sql)):
        if sql[index] == "(":
            depth += 1
        elif sql[index] == ")":
            depth -= 1
            if depth == 0:
                return sql[opened:index]
    raise AssertionError(f"unbalanced OVER clause after {function_prefix!r}")


#: Five consecutive days, and how many events each carries. Deliberately not
#: monotonic, so a running total that accidentally reported the day's own
#: count would not pass for one.
EVENTS_PER_DAY = {1: 1, 2: 3, 3: 2, 4: 5, 5: 4}


@pytest.mark.django_db
class TestWindowExecution:
    def setup_method(self):
        self.client = APIClient()

    def _org(self) -> Organization:
        return baker.make(Organization, name=f"Org {uuid.uuid4().hex[:6]}")

    def _make_calendar(self, org: Organization) -> Calendar:
        unique = uuid.uuid4().hex[:8]
        return Calendar.objects.create(
            organization=org,
            name=f"Calendar {unique}",
            external_id=f"cal-{unique}",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
            manage_available_windows=True,
        )

    def _make_events(self, org: Organization, calendar: Calendar, day: int, count: int) -> None:
        for index in range(count):
            baker.make(
                "calendar_integration.CalendarEvent",
                organization=org,
                calendar=calendar,
                start_time_tz_unaware=datetime.datetime(2026, 3, day, 10, 0, 0),
                end_time_tz_unaware=datetime.datetime(2026, 3, day, 11, 0, 0),
                timezone="UTC",
                external_id=f"e-{uuid.uuid4().hex[:8]}",
                title=f"Event {day}-{index}",
            )

    def _token(self, org: Organization):
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"orgwide_{uuid.uuid4().hex[:8]}", organization=org
        )
        baker.make(
            ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR_EVENT
        )
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

    def _variables(self, **overrides) -> dict:
        variables = {
            "filter": {
                "startDatetime": "2026-03-01T00:00:00Z",
                "endDatetime": "2026-03-20T00:00:00Z",
                "calendarId": None,
            },
            "groupBy": BY_DAY,
        }
        variables.update(overrides)
        return variables

    def _rows(self, response) -> list[dict]:
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors", []) == []
        return data["data"]["calendarEventAggregate"]

    def _aggregate_queries(self, ctx) -> list[dict]:
        return [q for q in ctx.captured_queries if "calendar_integration_calendarevent" in q["sql"]]

    def _assert_one_query_with_an_over_clause(self, ctx) -> None:
        """The whole point of the phase: one statement, and Postgres computed
        the window. A Python accumulation would leave neither mark."""
        queries = self._aggregate_queries(ctx)
        assert len(queries) == 1
        assert "OVER (" in queries[0]["sql"]

    def _five_days(self, org: Organization, calendar: Calendar) -> None:
        for day, count in EVENTS_PER_DAY.items():
            self._make_events(org, calendar, day, count)

    # -- running total ---------------------------------------------------

    def test_running_count_is_non_decreasing_and_ends_at_the_plain_total(self):
        org = self._org()
        calendar = self._make_calendar(org)
        self._five_days(org, calendar)
        system_user, token, auth = self._token(org)

        with CaptureQueriesContext(connection) as ctx:
            response = self._post(
                CALENDAR_EVENT_WINDOW_QUERY,
                system_user,
                token,
                auth,
                self._variables(
                    window={
                        "metric": "COUNT",
                        "orderBy": [
                            {
                                "key": {"temporal": {"field": "START_TIME", "granularity": "DAY"}},
                                "direction": "ASC",
                            }
                        ],
                    },
                    orderBy=[
                        {
                            "key": {"temporal": {"field": "START_TIME", "granularity": "DAY"}},
                            "direction": "ASC",
                        }
                    ],
                ),
            )

        rows = self._rows(response)
        assert [row["count"] for row in rows] == [1, 3, 2, 5, 4]

        running = [row["window"]["runningTotal"] for row in rows]
        # Hand-computed, not re-derived: 1, 1+3, 1+3+2, ...
        assert running == [1.0, 4.0, 6.0, 11.0, 15.0]
        assert running == sorted(running)
        assert running[-1] == sum(EVENTS_PER_DAY.values())

        self._assert_one_query_with_an_over_clause(ctx)

    def test_a_three_row_moving_average_matches_a_hand_computed_expectation(self):
        org = self._org()
        calendar = self._make_calendar(org)
        self._five_days(org, calendar)
        system_user, token, auth = self._token(org)

        with CaptureQueriesContext(connection) as ctx:
            response = self._post(
                CALENDAR_EVENT_WINDOW_QUERY,
                system_user,
                token,
                auth,
                self._variables(
                    window={
                        "metric": "COUNT",
                        "orderBy": [
                            {
                                "key": {"temporal": {"field": "START_TIME", "granularity": "DAY"}},
                                "direction": "ASC",
                            }
                        ],
                        "frame": {
                            "frameType": "ROWS",
                            "start": "PRECEDING",
                            "startOffset": 2,
                            "end": "CURRENT_ROW",
                        },
                    },
                    orderBy=[
                        {
                            "key": {"temporal": {"field": "START_TIME", "granularity": "DAY"}},
                            "direction": "ASC",
                        }
                    ],
                ),
            )

        rows = self._rows(response)
        moving = [row["window"]["movingAverage"] for row in rows]
        # Counts are 1, 3, 2, 5, 4. The frame is the two rows before each row
        # plus the row itself, and it is short at the start rather than
        # padded -- which is what makes the first two entries 1 and 2 rather
        # than 1/3 and 4/3.
        assert moving == pytest.approx([1.0, 2.0, 2.0, 10 / 3, 11 / 3])

        self._assert_one_query_with_an_over_clause(ctx)

    def test_percent_of_total_sums_to_one_hundred_across_the_partition(self):
        org = self._org()
        calendar = self._make_calendar(org)
        self._five_days(org, calendar)
        system_user, token, auth = self._token(org)

        with CaptureQueriesContext(connection) as ctx:
            response = self._post(
                CALENDAR_EVENT_WINDOW_QUERY,
                system_user,
                token,
                auth,
                self._variables(
                    window={
                        "metric": "COUNT",
                        "orderBy": [
                            {
                                "key": {"temporal": {"field": "START_TIME", "granularity": "DAY"}},
                                "direction": "ASC",
                            }
                        ],
                    },
                    orderBy=[
                        {
                            "key": {"temporal": {"field": "START_TIME", "granularity": "DAY"}},
                            "direction": "ASC",
                        }
                    ],
                ),
            )

        rows = self._rows(response)
        shares = [row["window"]["percentOfTotal"] for row in rows]
        total = sum(EVENTS_PER_DAY.values())
        assert shares == pytest.approx([100 * count / total for count in EVENTS_PER_DAY.values()])
        assert sum(shares) == pytest.approx(100.0)

        # A share is of the whole partition, not of the rows so far, so the
        # last row is emphatically not 100.
        assert shares[-1] != pytest.approx(100.0)

        self._assert_one_query_with_an_over_clause(ctx)

    def test_rank_orders_the_groups_by_the_windows_own_ordering(self):
        org = self._org()
        calendar = self._make_calendar(org)
        self._five_days(org, calendar)
        system_user, token, auth = self._token(org)

        with CaptureQueriesContext(connection) as ctx:
            response = self._post(
                CALENDAR_EVENT_WINDOW_QUERY,
                system_user,
                token,
                auth,
                self._variables(
                    window={
                        "metric": "COUNT",
                        "orderBy": [{"metric": "COUNT", "direction": "DESC"}],
                    },
                    orderBy=[
                        {
                            "key": {"temporal": {"field": "START_TIME", "granularity": "DAY"}},
                            "direction": "ASC",
                        }
                    ],
                ),
            )

        rows = self._rows(response)
        # Rows come back in date order (the field's own orderBy) while the
        # rank follows the window's ordering (busiest day first): counts
        # 1, 3, 2, 5, 4 rank 5, 3, 4, 1, 2.
        assert [row["count"] for row in rows] == [1, 3, 2, 5, 4]
        assert [row["window"]["rank"] for row in rows] == [5, 3, 4, 1, 2]

        self._assert_one_query_with_an_over_clause(ctx)

    # -- partitioning ----------------------------------------------------

    def test_partition_by_calendar_keeps_each_calendars_running_total_independent(self):
        org = self._org()
        first = self._make_calendar(org)
        second = self._make_calendar(org)
        # Interleaved by date, so a partition that leaked would accumulate one
        # calendar's counts into the other's totals rather than merely
        # reordering them.
        self._make_events(org, first, 1, 1)
        self._make_events(org, second, 1, 10)
        self._make_events(org, first, 2, 2)
        self._make_events(org, second, 2, 20)
        self._make_events(org, first, 3, 4)
        self._make_events(org, second, 3, 40)
        system_user, token, auth = self._token(org)

        with CaptureQueriesContext(connection) as ctx:
            response = self._post(
                CALENDAR_EVENT_WINDOW_QUERY,
                system_user,
                token,
                auth,
                self._variables(
                    groupBy=[
                        {"field": "CALENDAR_ID"},
                        {"temporal": {"field": "START_TIME", "granularity": "DAY"}},
                    ],
                    window={
                        "metric": "COUNT",
                        "partitionBy": [{"field": "CALENDAR_ID"}],
                        "orderBy": [
                            {
                                "key": {"temporal": {"field": "START_TIME", "granularity": "DAY"}},
                                "direction": "ASC",
                            }
                        ],
                    },
                    orderBy=[
                        {"key": {"field": "CALENDAR_ID"}, "direction": "ASC"},
                        {
                            "key": {"temporal": {"field": "START_TIME", "granularity": "DAY"}},
                            "direction": "ASC",
                        },
                    ],
                ),
            )

        rows = self._rows(response)
        by_calendar: dict[int, list[float]] = {}
        for row in rows:
            by_calendar.setdefault(row["key"]["calendarId"], []).append(
                row["window"]["runningTotal"]
            )

        assert by_calendar[first.id] == [1.0, 3.0, 7.0]
        assert by_calendar[second.id] == [10.0, 30.0, 70.0]

        # Each partition's share is of its own calendar, not of the result:
        # both calendars' shares sum to 100 separately, which they could not
        # do if they were shares of one combined total.
        shares: dict[int, list[float]] = {}
        for row in rows:
            shares.setdefault(row["key"]["calendarId"], []).append(row["window"]["percentOfTotal"])
        assert sum(shares[first.id]) == pytest.approx(100.0)
        assert sum(shares[second.id]) == pytest.approx(100.0)

        queries = self._aggregate_queries(ctx)
        assert len(queries) == 1
        assert "PARTITION BY" in queries[0]["sql"]

    def test_grouped_by_day_and_partitioned_by_calendar_in_one_statement(self):
        """The phase's acceptance case, whole: a day-grouped aggregate with a
        window partitioned by calendar carries a running count and a moving
        average, both computed by Postgres, both correct per calendar, in one
        query."""
        org = self._org()
        first = self._make_calendar(org)
        second = self._make_calendar(org)
        for day, count in ((1, 2), (2, 4), (3, 6)):
            self._make_events(org, first, day, count)
        for day, count in ((1, 1), (2, 1), (3, 7)):
            self._make_events(org, second, day, count)
        system_user, token, auth = self._token(org)

        with CaptureQueriesContext(connection) as ctx:
            response = self._post(
                CALENDAR_EVENT_WINDOW_QUERY,
                system_user,
                token,
                auth,
                self._variables(
                    groupBy=[
                        {"field": "CALENDAR_ID"},
                        {"temporal": {"field": "START_TIME", "granularity": "DAY"}},
                    ],
                    window={
                        "metric": "COUNT",
                        "partitionBy": [{"field": "CALENDAR_ID"}],
                        "orderBy": [
                            {
                                "key": {"temporal": {"field": "START_TIME", "granularity": "DAY"}},
                                "direction": "ASC",
                            }
                        ],
                        "frame": {
                            "frameType": "ROWS",
                            "start": "PRECEDING",
                            "startOffset": 1,
                            "end": "CURRENT_ROW",
                        },
                    },
                    orderBy=[
                        {"key": {"field": "CALENDAR_ID"}, "direction": "ASC"},
                        {
                            "key": {"temporal": {"field": "START_TIME", "granularity": "DAY"}},
                            "direction": "ASC",
                        },
                    ],
                ),
            )

        rows = self._rows(response)
        running: dict[int, list[float]] = {}
        moving: dict[int, list[float]] = {}
        for row in rows:
            calendar_id = row["key"]["calendarId"]
            running.setdefault(calendar_id, []).append(row["window"]["runningTotal"])
            moving.setdefault(calendar_id, []).append(row["window"]["movingAverage"])

        # Counts 2, 4, 6 and 1, 1, 7. Running totals accumulate within a
        # calendar; the two-row moving average reads this row and the one
        # before it, and is short rather than padded on the first row.
        assert running[first.id] == [2.0, 6.0, 12.0]
        assert running[second.id] == [1.0, 2.0, 9.0]
        assert moving[first.id] == pytest.approx([2.0, 3.0, 5.0])
        assert moving[second.id] == pytest.approx([1.0, 1.0, 4.0])

        self._assert_one_query_with_an_over_clause(ctx)

    def test_a_running_total_over_tied_rows_is_stable_across_runs(self):
        """Rows tying on the window's ordering are peers, and Postgres does not
        define where inside a peer group ``UNBOUNDED PRECEDING AND CURRENT ROW``
        cuts. Without a tiebreak each tied row's running total is whichever
        partial sum that run's physical row order produced -- the same query
        returning different numbers on different runs, which is the failure the
        mandatory ``orderBy`` exists to prevent.

        Three calendars share every day and the window orders by day alone, so
        every row has two peers.
        """
        org = self._org()
        calendars = [self._make_calendar(org) for _ in range(3)]
        for day in (1, 2, 3):
            for index, calendar in enumerate(calendars):
                self._make_events(org, calendar, day, index + 1)
        system_user, token, auth = self._token(org)

        variables = self._variables(
            groupBy=[
                {"field": "CALENDAR_ID"},
                {"temporal": {"field": "START_TIME", "granularity": "DAY"}},
            ],
            window={
                "metric": "COUNT",
                "orderBy": [
                    {
                        "key": {"temporal": {"field": "START_TIME", "granularity": "DAY"}},
                        "direction": "ASC",
                    }
                ],
            },
            orderBy=[
                {"key": {"field": "CALENDAR_ID"}, "direction": "ASC"},
                {
                    "key": {"temporal": {"field": "START_TIME", "granularity": "DAY"}},
                    "direction": "ASC",
                },
            ],
        )

        def _totals() -> list[tuple]:
            rows = self._rows(
                self._post(CALENDAR_EVENT_WINDOW_QUERY, system_user, token, auth, variables)
            )
            return [
                (row["key"]["calendarId"], row["key"]["startTimeBucket"], row["window"][name])
                for row in rows
                for name in ("runningTotal", "movingAverage")
            ]

        first_run = _totals()
        assert first_run == _totals() == _totals()

        # Repeating three times would also pass by luck, so assert the reason
        # directly: the window's own ORDER BY carries the group key the
        # caller's ordering left out, which makes the order total.
        with CaptureQueriesContext(connection) as ctx:
            rows = self._rows(
                self._post(CALENDAR_EVENT_WINDOW_QUERY, system_user, token, auth, variables)
            )
        sql = self._aggregate_queries(ctx)[0]["sql"]
        assert "calendar_fk_id" in _over_clause(sql, "SUM(COUNT(")
        # And the rank's ordering is deliberately left alone, so its peers
        # still share a rank rather than becoming a row number.
        assert "calendar_fk_id" not in _over_clause(sql, "RANK()")

        # With no partition the running total accumulates across every group,
        # so the last row of that total order carries the whole result's count.
        assert max(row["window"]["runningTotal"] for row in rows) == sum(
            row["count"] for row in rows
        )

    def test_rank_still_lets_tied_rows_share_a_rank(self):
        """The tiebreak is for the frame-sensitive functions only. A rank's
        peers are meant to share a rank -- breaking their tie would make this
        ``ROW_NUMBER`` under another name."""
        org = self._org()
        calendars = [self._make_calendar(org) for _ in range(3)]
        # Every calendar has the same count, so all three tie on the window's
        # ordering and all three should rank 1.
        for calendar in calendars:
            self._make_events(org, calendar, 1, 2)
        system_user, token, auth = self._token(org)

        response = self._post(
            CALENDAR_EVENT_WINDOW_QUERY,
            system_user,
            token,
            auth,
            self._variables(
                groupBy=[{"field": "CALENDAR_ID"}],
                window={
                    "metric": "COUNT",
                    "orderBy": [{"metric": "COUNT", "direction": "DESC"}],
                },
            ),
        )

        rows = self._rows(response)
        assert [row["count"] for row in rows] == [2, 2, 2]
        assert [row["window"]["rank"] for row in rows] == [1, 1, 1]

    # -- refusals, as a caller sees them ---------------------------------

    def test_a_window_without_an_order_by_comes_back_as_an_error(self):
        org = self._org()
        calendar = self._make_calendar(org)
        self._make_events(org, calendar, 1, 1)
        system_user, token, auth = self._token(org)

        response = self._post(
            CALENDAR_EVENT_WINDOW_QUERY,
            system_user,
            token,
            auth,
            self._variables(window={"metric": "COUNT"}),
        )

        assert response.status_code == 200
        errors = response.json().get("errors", [])
        assert [error["message"] for error in errors] == [
            "A window needs at least one orderBy entry"
        ]

    def test_partitioning_on_an_ungrouped_dimension_comes_back_as_an_error(self):
        org = self._org()
        calendar = self._make_calendar(org)
        self._make_events(org, calendar, 1, 1)
        system_user, token, auth = self._token(org)

        response = self._post(
            CALENDAR_EVENT_WINDOW_QUERY,
            system_user,
            token,
            auth,
            self._variables(
                window={
                    "metric": "COUNT",
                    "partitionBy": [{"field": "CALENDAR_ID"}],
                    "orderBy": [
                        {
                            "key": {"temporal": {"field": "START_TIME", "granularity": "DAY"}},
                            "direction": "ASC",
                        }
                    ],
                }
            ),
        )

        assert response.status_code == 200
        errors = response.json().get("errors", [])
        assert [error["message"] for error in errors] == [
            "window.partitionBy must name only this query's groupBy dimensions"
        ]

    # -- the metric a window reads ---------------------------------------

    def test_a_window_over_a_metric_the_selection_never_asked_for_still_computes(self):
        """The guard ``having`` and ``orderBy`` already have: a window may read
        a metric the row selection did not name, and the engine annotates it
        rather than erroring for want of a column."""
        org = self._org()
        calendar = self._make_calendar(org)
        for day, minutes in ((1, 30), (2, 90)):
            baker.make(
                "calendar_integration.CalendarEvent",
                organization=org,
                calendar=calendar,
                start_time_tz_unaware=datetime.datetime(2026, 3, day, 10, 0, 0),
                end_time_tz_unaware=datetime.datetime(2026, 3, day, 10, 0, 0)
                + datetime.timedelta(minutes=minutes),
                timezone="UTC",
                external_id=f"e-{uuid.uuid4().hex[:8]}",
                title=f"Event {day}",
            )
        system_user, token, auth = self._token(org)

        response = self._post(
            CALENDAR_EVENT_WINDOW_QUERY,
            system_user,
            token,
            auth,
            self._variables(
                window={
                    "metric": "DURATION_MINUTES_SUM",
                    "orderBy": [
                        {
                            "key": {"temporal": {"field": "START_TIME", "granularity": "DAY"}},
                            "direction": "ASC",
                        }
                    ],
                },
                orderBy=[
                    {
                        "key": {"temporal": {"field": "START_TIME", "granularity": "DAY"}},
                        "direction": "ASC",
                    }
                ],
            ),
        )

        rows = self._rows(response)
        # The document selects ``count`` and the window, never
        # ``durationMinutes``; the running total is still over the minutes.
        assert [row["window"]["runningTotal"] for row in rows] == [30.0, 120.0]

    def test_only_the_selected_window_functions_are_computed(self):
        org = self._org()
        calendar = self._make_calendar(org)
        self._five_days(org, calendar)
        system_user, token, auth = self._token(org)

        query = """
        query Agg(
            $filter: CalendarEventAggregateFilterInput!
            $groupBy: [CalendarEventGroupByInput!]!
            $window: CalendarEventWindowInput
        ) {
            calendarEventAggregate(
                filter: $filter
                groupBy: $groupBy
                timezone: "UTC"
                window: $window
            ) {
                count
                window { runningTotal }
            }
        }
        """

        with CaptureQueriesContext(connection) as ctx:
            response = self._post(
                query,
                system_user,
                token,
                auth,
                self._variables(
                    window={
                        "metric": "COUNT",
                        "orderBy": [
                            {
                                "key": {"temporal": {"field": "START_TIME", "granularity": "DAY"}},
                                "direction": "ASC",
                            }
                        ],
                    }
                ),
            )

        rows = self._rows(response)
        assert all(row["window"]["runningTotal"] is not None for row in rows)

        sql = self._aggregate_queries(ctx)[0]["sql"]
        assert sql.count("OVER (") == 1
        assert "RANK()" not in sql.upper()

    # -- the regression case ---------------------------------------------

    def test_a_query_without_a_window_returns_what_it_did_before(self):
        org = self._org()
        calendar = self._make_calendar(org)
        self._five_days(org, calendar)
        system_user, token, auth = self._token(org)

        with CaptureQueriesContext(connection) as ctx:
            response = self._post(
                CALENDAR_EVENT_PLAIN_QUERY, system_user, token, auth, self._variables()
            )

        rows = self._rows(response)
        assert sorted(row["count"] for row in rows) == sorted(EVENTS_PER_DAY.values())
        assert all(row["key"]["calendarId"] is None for row in rows)

        queries = self._aggregate_queries(ctx)
        assert len(queries) == 1
        assert "OVER (" not in queries[0]["sql"]

    def test_a_window_argument_does_not_move_the_rows_or_the_counts(self):
        """The same query with and without a ``window`` argument returns the
        same groups, in the same order, with the same counts -- the window
        adds a column and changes nothing else."""
        org = self._org()
        calendar = self._make_calendar(org)
        self._five_days(org, calendar)
        system_user, token, auth = self._token(org)

        plain = self._rows(
            self._post(CALENDAR_EVENT_PLAIN_QUERY, system_user, token, auth, self._variables())
        )
        windowed = self._rows(
            self._post(
                CALENDAR_EVENT_WINDOW_QUERY,
                system_user,
                token,
                auth,
                self._variables(
                    window={
                        "metric": "COUNT",
                        "orderBy": [
                            {
                                "key": {"temporal": {"field": "START_TIME", "granularity": "DAY"}},
                                "direction": "ASC",
                            }
                        ],
                    }
                ),
            )
        )

        assert [row["key"] for row in windowed] == [row["key"] for row in plain]
        assert [row["count"] for row in windowed] == [row["count"] for row in plain]
