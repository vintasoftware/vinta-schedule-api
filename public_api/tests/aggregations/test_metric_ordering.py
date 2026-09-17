"""Ordering by a metric, and the top-N that falls out of it with a limit.

Two things are being asserted, and only the first is obvious. The first is that
`orderBy: [{metric: COUNT, direction: DESC}] limit: 3` returns the three
busiest calendars in the right order. The second is that it returns the *same*
three in the *same* order every time -- ties inside a metric ordering are
broken by the group key the executor appends, and without that a repeated
query returns whatever the database found convenient, which is how paging
starts dropping and duplicating groups.
"""

import datetime

from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest

from calendar_integration.models import Calendar
from public_api.constants import PublicAPIResources
from public_api.tests.aggregations.conftest import (
    WINDOW_END,
    WINDOW_START,
    make_calendar,
    make_event,
    org_wide_token,
    post_graphql,
)


ORDERED_QUERY = """
query EventAggregate(
  $start: DateTime!
  $end: DateTime!
  $orderBy: [CalendarEventAggregateOrderInput!]
  $limit: Int!
) {
  calendarEventAggregate(
    filter: {startDatetime: $start, endDatetime: $end}
    groupBy: [{scalar: CALENDAR_ID}]
    timezone: "UTC"
    orderBy: $orderBy
    limit: $limit
  ) {
    key { calendarId }
    count
  }
}
"""

ORDERED_WITH_HAVING_QUERY = """
query EventAggregate(
  $start: DateTime!
  $end: DateTime!
  $having: CalendarEventHavingInput
  $orderBy: [CalendarEventAggregateOrderInput!]
  $limit: Int!
) {
  calendarEventAggregate(
    filter: {startDatetime: $start, endDatetime: $end}
    groupBy: [{scalar: CALENDAR_ID}]
    timezone: "UTC"
    having: $having
    orderBy: $orderBy
    limit: $limit
  ) {
    key { calendarId }
    count
  }
}
"""


def _window() -> dict[str, str]:
    return {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()}


def _busy_calendars(organization, counts: dict[str, int]) -> dict[str, Calendar]:
    """One calendar per entry, holding that many 30-minute events."""
    base = datetime.datetime(2026, 3, 10, 9, 0)
    calendars: dict[str, Calendar] = {}
    for name, event_count in counts.items():
        calendar = make_calendar(organization, name=name)
        for index in range(event_count):
            make_event(
                organization,
                calendar,
                title=f"{name}-{index}",
                start=base + datetime.timedelta(hours=index),
                minutes=30,
            )
        calendars[name] = calendar
    return calendars


@pytest.mark.django_db
class TestMetricOrdering:
    def test_top_three_by_count_descending(self, organization):
        """The plan's acceptance shape: the busiest three, in order."""
        calendars = _busy_calendars(organization, {"a": 5, "b": 4, "c": 3, "d": 2, "e": 1})
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {
            "orderBy": [{"metric": "COUNT", "direction": "DESC"}],
            "limit": 3,
        }

        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(ORDERED_QUERY, system_user, token, auth, variables)

        assert response.status_code == 200
        payload = response.json()
        assert payload.get("errors", []) == []
        rows = payload["data"]["calendarEventAggregate"]
        assert [row["count"] for row in rows] == [5, 4, 3]
        assert [row["key"]["calendarId"] for row in rows] == [
            calendars["a"].id,
            calendars["b"].id,
            calendars["c"].id,
        ]

        # Sorted and cut by the database, in the one grouped query.
        grouped = [q["sql"] for q in captured.captured_queries if "GROUP BY" in q["sql"]]
        assert len(grouped) == 1, grouped
        assert "ORDER BY" in grouped[0]
        assert "LIMIT" in grouped[0]

    def test_ascending_returns_the_quietest(self, organization):
        calendars = _busy_calendars(organization, {"a": 5, "b": 4, "c": 1})
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {
            "orderBy": [{"metric": "COUNT", "direction": "ASC"}],
            "limit": 2,
        }
        response = post_graphql(ORDERED_QUERY, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        assert [row["count"] for row in rows] == [1, 4]
        assert rows[0]["key"]["calendarId"] == calendars["c"].id

    def test_ordering_by_an_unselected_metric_still_sorts_by_it(self, organization):
        """`durationMinutes` is not in the selection set, and still orders it.

        Without the metric being annotated anyway, the alias would resolve
        against the model and sort on something else entirely -- here the
        calendar with the single long event would not come first.
        """
        base = datetime.datetime(2026, 3, 10, 9, 0)
        many_short = make_calendar(organization, name="many-short")
        one_long = make_calendar(organization, name="one-long")
        for index in range(3):
            make_event(
                organization,
                many_short,
                title=f"s{index}",
                start=base + datetime.timedelta(hours=index),
                minutes=10,
            )
        make_event(organization, one_long, title="l", start=base, minutes=600)

        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {
            "orderBy": [{"metric": "DURATION_MINUTES_SUM", "direction": "DESC"}],
            "limit": 10,
        }
        response = post_graphql(ORDERED_QUERY, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        # By count the short calendar leads 3 to 1; by summed minutes it does not.
        assert [row["key"]["calendarId"] for row in rows] == [one_long.id, many_short.id]
        assert [row["count"] for row in rows] == [1, 3]

    def test_ordering_by_a_group_key_dimension(self, organization):
        calendars = _busy_calendars(organization, {"a": 1, "b": 1, "c": 1})
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {
            "orderBy": [{"key": "CALENDAR_ID", "direction": "ASC"}],
            "limit": 10,
        }
        response = post_graphql(ORDERED_QUERY, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        assert [row["key"]["calendarId"] for row in rows] == sorted(
            calendar.id for calendar in calendars.values()
        )


@pytest.mark.django_db
class TestOrderingIsDeterministic:
    def test_ties_break_the_same_way_across_repeated_runs(self, organization):
        """Six calendars with identical counts -- the ordering is all tiebreak."""
        _busy_calendars(organization, {name: 2 for name in "abcdef"})
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {
            "orderBy": [{"metric": "COUNT", "direction": "DESC"}],
            "limit": 4,
        }

        runs = []
        for _ in range(4):
            response = post_graphql(ORDERED_QUERY, system_user, token, auth, variables)
            assert response.json().get("errors", []) == []
            runs.append(
                [
                    row["key"]["calendarId"]
                    for row in response.json()["data"]["calendarEventAggregate"]
                ]
            )

        assert all(run == runs[0] for run in runs), runs
        assert len(runs[0]) == 4
        assert len(set(runs[0])) == 4

    def test_paging_a_tied_ordering_partitions_the_groups(self, organization):
        """Every group once across pages -- the point of the appended tiebreak."""
        calendars = _busy_calendars(organization, {name: 2 for name in "abcdef"})
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])

        paged_query = ORDERED_QUERY.replace(
            "$limit: Int!\n)", "$limit: Int!\n  $offset: Int!\n)"
        ).replace("limit: $limit", "limit: $limit\n    offset: $offset")

        seen: list[int] = []
        for offset in (0, 2, 4):
            variables = _window() | {
                "orderBy": [{"metric": "COUNT", "direction": "DESC"}],
                "limit": 2,
                "offset": offset,
            }
            response = post_graphql(paged_query, system_user, token, auth, variables)
            assert response.json().get("errors", []) == []
            seen.extend(
                row["key"]["calendarId"]
                for row in response.json()["data"]["calendarEventAggregate"]
            )

        assert sorted(seen) == sorted(calendar.id for calendar in calendars.values())
        assert len(seen) == len(set(seen))


@pytest.mark.django_db
class TestOrderingValidationThroughTheSchema:
    def test_an_entry_naming_both_key_and_metric_is_refused(self, organization):
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {
            "orderBy": [{"key": "CALENDAR_ID", "metric": "COUNT"}],
            "limit": 10,
        }
        response = post_graphql(ORDERED_QUERY, system_user, token, auth, variables)

        assert response.status_code == 200
        messages = [error["message"] for error in response.json().get("errors", [])]
        assert messages == ["Each orderBy entry must name exactly one of `key` or `metric`"]

    def test_an_entry_naming_neither_is_refused(self, organization):
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {"orderBy": [{"direction": "DESC"}], "limit": 10}
        response = post_graphql(ORDERED_QUERY, system_user, token, auth, variables)

        messages = [error["message"] for error in response.json().get("errors", [])]
        assert messages == ["Each orderBy entry must name exactly one of `key` or `metric`"]

    def test_ordering_by_an_ungrouped_dimension_is_refused(self, organization):
        """It would add a column to the GROUP BY and split the numbers further."""
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {"orderBy": [{"key": "TIMEZONE"}], "limit": 10}
        response = post_graphql(ORDERED_QUERY, system_user, token, auth, variables)

        messages = [error["message"] for error in response.json().get("errors", [])]
        assert len(messages) == 1
        assert "timezone" in messages[0]
        assert "groupBy" in messages[0]


@pytest.mark.django_db
class TestHavingAndOrderingTogether:
    def test_the_acceptance_case(self, organization):
        """`having: {count: {gt: N}}` + order by count desc + limit, in one query."""
        calendars = _busy_calendars(organization, {"a": 6, "b": 5, "c": 4, "d": 3, "e": 1})
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {
            "having": {"count": {"gt": 3}},
            "orderBy": [{"metric": "COUNT", "direction": "DESC"}],
            "limit": 2,
        }

        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(ORDERED_WITH_HAVING_QUERY, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        # Groups of 6, 5 and 4 survive the having; the limit takes the top two.
        assert [row["count"] for row in rows] == [6, 5]
        assert [row["key"]["calendarId"] for row in rows] == [
            calendars["a"].id,
            calendars["b"].id,
        ]

        grouped = [q["sql"] for q in captured.captured_queries if "GROUP BY" in q["sql"]]
        assert len(grouped) == 1, grouped
        assert "HAVING" in grouped[0]
        assert "ORDER BY" in grouped[0]
        assert "LIMIT" in grouped[0]
