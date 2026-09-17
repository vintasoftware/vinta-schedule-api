"""Window functions end to end: real rows, real `OVER` clauses, hand-checked numbers.

A running total that is subtly wrong -- an off-by-one frame bound, a partition
that leaks across calendars -- returns confident numbers that no type check and
no smoke test catches. So every case here asserts **literal expected values**
computed by hand from the fixture, not properties like "is non-decreasing" on
their own, and every case asserts the SQL actually contains `OVER (`. A Python
fallback that reshaped rows after the fact would satisfy the numbers and fail
the SQL assertion, which is exactly the substitution this plan forbids.
"""

import datetime
import itertools

from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest

from public_api.constants import PublicAPIResources
from public_api.tests.aggregations.conftest import (
    WINDOW_END,
    WINDOW_START,
    make_calendar,
    make_event,
    org_wide_token,
    post_graphql,
)


RUNNING_QUERY = """
query EventWindow($start: DateTime!, $end: DateTime!, $window: CalendarEventWindowInput) {
  calendarEventAggregate(
    filter: {startDatetime: $start, endDatetime: $end}
    groupBy: [{temporal: {field: START_TIME, granularity: DAY}}]
    timezone: "UTC"
    orderBy: [{key: START_TIME, direction: ASC}]
    window: $window
  ) {
    key { startTime }
    count
    window { runningCount movingAvgCount rank percentOfTotal }
  }
}
"""

PARTITIONED_QUERY = """
query EventWindow($start: DateTime!, $end: DateTime!, $window: CalendarEventWindowInput) {
  calendarEventAggregate(
    filter: {startDatetime: $start, endDatetime: $end}
    groupBy: [{scalar: CALENDAR_ID}, {temporal: {field: START_TIME, granularity: DAY}}]
    timezone: "UTC"
    orderBy: [{key: CALENDAR_ID, direction: ASC}, {key: START_TIME, direction: ASC}]
    window: $window
  ) {
    key { calendarId startTime }
    count
    window { runningCount }
  }
}
"""

DURATION_WINDOW_QUERY = """
query EventWindow($start: DateTime!, $end: DateTime!, $window: CalendarEventWindowInput) {
  calendarEventAggregate(
    filter: {startDatetime: $start, endDatetime: $end}
    groupBy: [{temporal: {field: START_TIME, granularity: DAY}}]
    timezone: "UTC"
    orderBy: [{key: START_TIME, direction: ASC}]
    window: $window
  ) {
    key { startTime }
    count
    window { runningDurationMinutes }
  }
}
"""

BY_DAY = [{"key": "START_TIME", "direction": "ASC"}]


def _window() -> dict[str, str]:
    return {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()}


def _grouped_sql(captured) -> list[str]:
    return [query["sql"] for query in captured.captured_queries if "GROUP BY" in query["sql"]]


@pytest.fixture
def five_days(organization):
    """One calendar with 1, 2, 3, 4 and 5 events on five consecutive days.

    Fifteen events in all. Deliberately distinct per-day counts so a running
    total, a moving average and a rank each have a different expected series --
    equal counts would let several wrong implementations pass.
    """
    calendar = make_calendar(organization, name="five-days")
    for day in range(1, 6):
        for index in range(day):
            make_event(
                organization,
                calendar,
                title=f"d{day}-{index}",
                start=datetime.datetime(2026, 3, day, 9 + index, 0),
                minutes=30,
            )
    return calendar


@pytest.mark.django_db
class TestRunningTotals:
    def test_running_count_accumulates_and_ends_at_the_plain_total(self, organization, five_days):
        """Counts 1,2,3,4,5 accumulate to 1,3,6,10,15 -- and 15 is the total."""
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {"window": {"orderBy": BY_DAY}}

        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(RUNNING_QUERY, system_user, token, auth, variables)

        assert response.status_code == 200
        payload = response.json()
        assert payload.get("errors", []) == []
        rows = payload["data"]["calendarEventAggregate"]

        assert [row["count"] for row in rows] == [1, 2, 3, 4, 5]
        running = [row["window"]["runningCount"] for row in rows]
        assert running == [1, 3, 6, 10, 15]

        # The properties the phase names, on top of the literal series.
        assert all(b >= a for a, b in itertools.pairwise(running))
        assert running[-1] == sum(row["count"] for row in rows) == 15

        # One query, and the database did the accumulating.
        grouped = _grouped_sql(captured)
        assert len(grouped) == 1, grouped
        assert "OVER (" in grouped[0], grouped[0]
        assert "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW" in grouped[0]

    def test_running_sum_over_a_numeric_metric(self, organization, five_days):
        """Each event is 30 minutes, so the running minutes are 30x the counts."""
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {"window": {"orderBy": BY_DAY}}
        response = post_graphql(DURATION_WINDOW_QUERY, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        assert [row["window"]["runningDurationMinutes"] for row in rows] == [
            pytest.approx(v) for v in (30.0, 90.0, 180.0, 300.0, 450.0)
        ]

    def test_a_running_total_ignores_the_callers_frame(self, organization, five_days):
        """A running total over a three-row frame would not be a running total.

        The same document with and without a frame must produce the same
        `runningCount`; only `movingAvgCount` may move.
        """
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        framed = _window() | {
            "window": {
                "orderBy": BY_DAY,
                "frame": {"start": "PRECEDING", "startOffset": 2, "end": "CURRENT_ROW"},
            }
        }
        unframed = _window() | {"window": {"orderBy": BY_DAY}}

        framed_rows = post_graphql(RUNNING_QUERY, system_user, token, auth, framed).json()["data"][
            "calendarEventAggregate"
        ]
        plain_rows = post_graphql(RUNNING_QUERY, system_user, token, auth, unframed).json()["data"][
            "calendarEventAggregate"
        ]

        assert [r["window"]["runningCount"] for r in framed_rows] == [1, 3, 6, 10, 15]
        assert [r["window"]["runningCount"] for r in plain_rows] == [1, 3, 6, 10, 15]
        # The frame did reach the moving average, so the two are not identical.
        assert [r["window"]["movingAvgCount"] for r in framed_rows] != [
            r["window"]["movingAvgCount"] for r in plain_rows
        ]


@pytest.mark.django_db
class TestMovingAverage:
    def test_a_three_row_moving_average_matches_a_hand_computation(self, organization, five_days):
        """Counts 1,2,3,4,5 over `ROWS BETWEEN 2 PRECEDING AND CURRENT ROW`.

        Row 1: 1/1 = 1.0        (only itself)
        Row 2: (1+2)/2 = 1.5    (frame not yet full)
        Row 3: (1+2+3)/3 = 2.0
        Row 4: (2+3+4)/3 = 3.0
        Row 5: (3+4+5)/3 = 4.0
        """
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {
            "window": {
                "orderBy": BY_DAY,
                "frame": {"start": "PRECEDING", "startOffset": 2, "end": "CURRENT_ROW"},
            }
        }

        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(RUNNING_QUERY, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        assert [row["window"]["movingAvgCount"] for row in rows] == [
            pytest.approx(v) for v in (1.0, 1.5, 2.0, 3.0, 4.0)
        ]

        sql = _grouped_sql(captured)[0]
        assert "OVER (" in sql
        assert "ROWS BETWEEN 2 PRECEDING AND CURRENT ROW" in sql

    def test_an_unframed_moving_average_is_cumulative(self, organization, five_days):
        """With the default frame it averages everything so far: 1, 1.5, 2, 2.5, 3."""
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {"window": {"orderBy": BY_DAY}}
        response = post_graphql(RUNNING_QUERY, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        assert [row["window"]["movingAvgCount"] for row in rows] == [
            pytest.approx(v) for v in (1.0, 1.5, 2.0, 2.5, 3.0)
        ]


@pytest.mark.django_db
class TestRankAndPercentOfTotal:
    def test_percent_of_total_sums_to_one_hundred(self, organization, five_days):
        """Counts 1,2,3,4,5 of 15 are 6.67, 13.3, 20, 26.7 and 33.3 percent."""
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {"window": {"orderBy": BY_DAY}}
        response = post_graphql(RUNNING_QUERY, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        percentages = [row["window"]["percentOfTotal"] for row in rows]

        assert percentages == [pytest.approx(count / 15 * 100) for count in (1, 2, 3, 4, 5)]
        assert sum(percentages) == pytest.approx(100.0)

    def test_rank_follows_the_windows_own_ordering(self, organization, five_days):
        """Ranked by descending count, the busiest day is rank 1."""
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {"window": {"orderBy": [{"metric": "COUNT", "direction": "DESC"}]}}
        response = post_graphql(RUNNING_QUERY, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        # Rows still come back by day ascending (counts 1..5), but the rank is
        # computed over the window's own descending-count ordering.
        assert [row["count"] for row in rows] == [1, 2, 3, 4, 5]
        assert [row["window"]["rank"] for row in rows] == [5, 4, 3, 2, 1]


@pytest.mark.django_db
class TestPartitioning:
    def test_each_calendars_running_total_is_independent(self, organization):
        """Two calendars interleaved by date; neither total may leak into the other.

        Calendar A has 1, 2, 3 events on days 1-3; calendar B has 10, 20, 30 on
        the same three days. Partitioned, A's running total ends at 6 and B's at
        60. Unpartitioned it would be a single series ending at 66, so a
        partition that did nothing is visible in every row but the first.
        """
        calendar_a = make_calendar(organization, name="A")
        calendar_b = make_calendar(organization, name="B")
        for day, (a_count, b_count) in enumerate([(1, 10), (2, 20), (3, 30)], start=1):
            for index in range(a_count):
                make_event(
                    organization,
                    calendar_a,
                    title=f"a{day}-{index}",
                    start=datetime.datetime(2026, 3, day, 6, 0) + datetime.timedelta(minutes=index),
                    minutes=1,
                )
            for index in range(b_count):
                make_event(
                    organization,
                    calendar_b,
                    title=f"b{day}-{index}",
                    start=datetime.datetime(2026, 3, day, 6, 0) + datetime.timedelta(minutes=index),
                    minutes=1,
                )

        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {"window": {"orderBy": BY_DAY, "partitionBy": ["CALENDAR_ID"]}}

        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(PARTITIONED_QUERY, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]

        per_calendar: dict[int, list[int]] = {}
        for row in rows:
            per_calendar.setdefault(row["key"]["calendarId"], []).append(
                row["window"]["runningCount"]
            )

        assert per_calendar[calendar_a.id] == [1, 3, 6]
        assert per_calendar[calendar_b.id] == [10, 30, 60]
        # The tell-tale of a leaking partition: a combined series reaching 66.
        assert 66 not in [value for series in per_calendar.values() for value in series]

        sql = _grouped_sql(captured)[0]
        assert "PARTITION BY" in sql, sql

    def test_without_a_partition_the_calendars_share_one_running_total(self, organization):
        """The same fixture unpartitioned really does reach 66 -- the contrast case."""
        calendar_a = make_calendar(organization, name="A")
        calendar_b = make_calendar(organization, name="B")
        for day, (a_count, b_count) in enumerate([(1, 10), (2, 20), (3, 30)], start=1):
            for index in range(a_count):
                make_event(
                    organization,
                    calendar_a,
                    title=f"a{day}-{index}",
                    start=datetime.datetime(2026, 3, day, 6, 0) + datetime.timedelta(minutes=index),
                    minutes=1,
                )
            for index in range(b_count):
                make_event(
                    organization,
                    calendar_b,
                    title=f"b{day}-{index}",
                    start=datetime.datetime(2026, 3, day, 6, 0) + datetime.timedelta(minutes=index),
                    minutes=1,
                )

        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {"window": {"orderBy": BY_DAY}}
        response = post_graphql(RUNNING_QUERY, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        assert [row["count"] for row in rows] == [11, 22, 33]
        assert [row["window"]["runningCount"] for row in rows] == [11, 33, 66]


@pytest.mark.django_db
class TestWindowsComposeWithTheRestOfThePipeline:
    def test_a_window_runs_over_the_groups_that_survived_having(self, organization, five_days):
        """`having` drops groups before the window accumulates over them.

        Days of 1 and 2 events are filtered out, so the running total starts at
        3 and ends at 12 -- not at 15, which is what a window computed before
        the filter would give.
        """
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        query = RUNNING_QUERY.replace(
            "$window: CalendarEventWindowInput",
            "$window: CalendarEventWindowInput\n  $having: CalendarEventHavingInput",
        ).replace("window: $window", "window: $window\n    having: $having")
        variables = _window() | {
            "window": {"orderBy": BY_DAY},
            "having": {"count": {"gt": 2}},
        }

        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(query, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        assert [row["count"] for row in rows] == [3, 4, 5]
        assert [row["window"]["runningCount"] for row in rows] == [3, 7, 12]

        sql = _grouped_sql(captured)[0]
        assert "HAVING" in sql
        assert "OVER (" in sql

    def test_selecting_no_window_field_emits_no_over_clause(self, organization, five_days):
        """A `window` argument nobody read from costs nothing."""
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        query = """
        query EventWindow($start: DateTime!, $end: DateTime!, $window: CalendarEventWindowInput) {
          calendarEventAggregate(
            filter: {startDatetime: $start, endDatetime: $end}
            groupBy: [{temporal: {field: START_TIME, granularity: DAY}}]
            timezone: "UTC"
            window: $window
          ) {
            key { startTime }
            count
          }
        }
        """
        variables = _window() | {"window": {"orderBy": BY_DAY}}

        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(query, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        assert "OVER (" not in _grouped_sql(captured)[0]

    def test_window_is_null_when_no_window_argument_is_given(self, organization, five_days):
        """The field exists on every row type; it is populated only on request."""
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        response = post_graphql(RUNNING_QUERY, system_user, token, auth, _window())

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        assert [row["window"] for row in rows] == [None] * 5


@pytest.mark.django_db
class TestOrderingDeterminism:
    """A window ordering that leaves rows tied is completed by the group key.

    Without the appended tiebreak the database chooses the order of tied rows,
    so a running total's intermediate values differ between two runs of the
    same query while the final value stays right -- the shape of wrongness
    nothing downstream would catch.
    """

    def test_a_running_total_over_a_tied_ordering_is_the_same_every_run(self, organization):
        """Five calendars with one event each, all on the same day.

        Ordered by day, every row ties. The running count must still be
        1, 2, 3, 4, 5 against a stable sequence of calendars on every run.
        """
        calendars = [make_calendar(organization, name=f"T{index}") for index in range(5)]
        for calendar in calendars:
            make_event(
                organization,
                calendar,
                title=calendar.name,
                start=datetime.datetime(2026, 3, 2, 9, 0),
                minutes=30,
            )

        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        # Grouped by calendar *and* day, ordered by day only -- so the window's
        # own ordering ties every row against every other.
        query = """
        query EventWindow($start: DateTime!, $end: DateTime!, $window: CalendarEventWindowInput) {
          calendarEventAggregate(
            filter: {startDatetime: $start, endDatetime: $end}
            groupBy: [{scalar: CALENDAR_ID}, {temporal: {field: START_TIME, granularity: DAY}}]
            timezone: "UTC"
            window: $window
          ) {
            key { calendarId }
            window { runningCount }
          }
        }
        """
        variables = _window() | {"window": {"orderBy": BY_DAY}}

        runs = []
        for _ in range(4):
            response = post_graphql(query, system_user, token, auth, variables)
            assert response.json().get("errors", []) == []
            rows = response.json()["data"]["calendarEventAggregate"]
            runs.append([(r["key"]["calendarId"], r["window"]["runningCount"]) for r in rows])

        assert all(run == runs[0] for run in runs), runs
        assert [running for _calendar, running in runs[0]] == [1, 2, 3, 4, 5]

    def test_rank_still_lets_tied_rows_share_a_rank(self, organization):
        """The tiebreak must not reach `rank`, which is defined to tie.

        Three calendars with two events each: ranked by count they are all
        equal, so all three are rank 1. A tiebreak appended here would return
        1, 2, 3 and quietly turn `rank` into a row number.
        """
        for index in range(3):
            calendar = make_calendar(organization, name=f"R{index}")
            for event in range(2):
                make_event(
                    organization,
                    calendar,
                    title=f"r{index}-{event}",
                    start=datetime.datetime(2026, 3, 2, 9 + event, 0),
                    minutes=30,
                )

        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        query = """
        query EventWindow($start: DateTime!, $end: DateTime!, $window: CalendarEventWindowInput) {
          calendarEventAggregate(
            filter: {startDatetime: $start, endDatetime: $end}
            groupBy: [{scalar: CALENDAR_ID}]
            timezone: "UTC"
            window: $window
          ) {
            key { calendarId }
            count
            window { rank }
          }
        }
        """
        variables = _window() | {"window": {"orderBy": [{"metric": "COUNT", "direction": "DESC"}]}}
        response = post_graphql(query, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        assert [row["count"] for row in rows] == [2, 2, 2]
        assert [row["window"]["rank"] for row in rows] == [1, 1, 1]


@pytest.mark.django_db
class TestFrameBoundsReachTheDatabaseIntact:
    def test_a_frame_ending_at_unbounded_preceding_is_refused(self, organization, five_days):
        """It would compile to a whole-partition frame, not an error."""
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {
            "window": {
                "orderBy": BY_DAY,
                "frame": {"start": "UNBOUNDED_PRECEDING", "end": "UNBOUNDED_PRECEDING"},
            }
        }
        response = post_graphql(RUNNING_QUERY, system_user, token, auth, variables)

        messages = [error["message"] for error in response.json().get("errors", [])]
        assert len(messages) == 1
        assert "UNBOUNDED_PRECEDING" in messages[0]

    def test_a_backwards_frame_is_refused_as_a_validation_error(self, organization, five_days):
        """Not as the bare `ValueError` Django raises while compiling the SQL."""
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {
            "window": {
                "orderBy": BY_DAY,
                "frame": {"start": "FOLLOWING", "startOffset": 2, "end": "CURRENT_ROW"},
            }
        }
        response = post_graphql(RUNNING_QUERY, system_user, token, auth, variables)

        assert response.status_code == 200
        messages = [error["message"] for error in response.json().get("errors", [])]
        assert messages == ["A frame's start must not come after its end"]

    def test_a_forward_looking_frame_is_honoured(self, organization, five_days):
        """`CURRENT ROW` to `2 FOLLOWING` over counts 1,2,3,4,5.

        Row 1: (1+2+3)/3 = 2.0
        Row 2: (2+3+4)/3 = 3.0
        Row 3: (3+4+5)/3 = 4.0
        Row 4: (4+5)/2   = 4.5   (frame runs off the end)
        Row 5: 5/1       = 5.0
        """
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {
            "window": {
                "orderBy": BY_DAY,
                "frame": {"start": "CURRENT_ROW", "end": "FOLLOWING", "endOffset": 2},
            }
        }

        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(RUNNING_QUERY, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        assert [row["window"]["movingAvgCount"] for row in rows] == [
            pytest.approx(v) for v in (2.0, 3.0, 4.0, 4.5, 5.0)
        ]
        assert "ROWS BETWEEN CURRENT ROW AND 2 FOLLOWING" in _grouped_sql(captured)[0]


@pytest.mark.django_db
class TestWindowValidationThroughTheSchema:
    def test_partitioning_by_an_ungrouped_dimension_is_refused(self, organization, five_days):
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        # Grouped by day only, so CALENDAR_ID is not a column these rows carry.
        variables = _window() | {"window": {"orderBy": BY_DAY, "partitionBy": ["CALENDAR_ID"]}}
        response = post_graphql(RUNNING_QUERY, system_user, token, auth, variables)

        messages = [error["message"] for error in response.json().get("errors", [])]
        assert len(messages) == 1
        assert "calendar_id" in messages[0]
        assert "groupBy" in messages[0]

    def test_a_window_without_an_ordering_is_refused(self, organization, five_days):
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {"window": {"orderBy": []}}
        response = post_graphql(RUNNING_QUERY, system_user, token, auth, variables)

        messages = [error["message"] for error in response.json().get("errors", [])]
        assert len(messages) == 1
        assert "orderBy" in messages[0]

    def test_omitting_order_by_entirely_fails_graphql_validation(self, organization, five_days):
        """`orderBy` is non-null on the input, so this never reaches a resolver."""
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {"window": {"partitionBy": []}}
        response = post_graphql(RUNNING_QUERY, system_user, token, auth, variables)

        payload = response.json()
        assert payload.get("data") is None
        assert payload["errors"]
        assert any("orderBy" in error["message"] for error in payload["errors"])
