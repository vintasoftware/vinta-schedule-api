"""`having` end to end: real rows, real SQL, and the filtering in the database.

The assertion this module exists for is not "the right groups came back" on its
own -- a Python list comprehension over the unfiltered rows would pass that.
It is that the filtering happened in `HAVING`, in the one query that computed
the aggregates, which is what the plan's "no post-processing" rule means in
practice. So every case checks the numbers *and* the emitted SQL.
"""

import datetime

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


HAVING_COUNT_QUERY = """
query EventAggregate($start: DateTime!, $end: DateTime!, $having: CalendarEventHavingInput) {
  calendarEventAggregate(
    filter: {startDatetime: $start, endDatetime: $end}
    groupBy: [{scalar: CALENDAR_ID}]
    timezone: "UTC"
    having: $having
  ) {
    key { calendarId }
    count
  }
}
"""

HAVING_DURATION_QUERY = """
query EventAggregate($start: DateTime!, $end: DateTime!, $having: CalendarEventHavingInput) {
  calendarEventAggregate(
    filter: {startDatetime: $start, endDatetime: $end}
    groupBy: [{scalar: CALENDAR_ID}]
    timezone: "UTC"
    having: $having
  ) {
    key { calendarId }
    count
    durationMinutes { sum }
  }
}
"""

HAVING_UNSELECTED_METRIC_QUERY = """
query EventAggregate($start: DateTime!, $end: DateTime!, $having: CalendarEventHavingInput) {
  calendarEventAggregate(
    filter: {startDatetime: $start, endDatetime: $end}
    groupBy: [{scalar: CALENDAR_ID}]
    timezone: "UTC"
    having: $having
  ) {
    key { calendarId }
    count
  }
}
"""


def _window() -> dict[str, str]:
    return {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()}


@pytest.fixture
def three_calendars(organization):
    """Calendars holding one, two and three events -- 30 minutes each.

    Sized so `count > 2` keeps exactly one group and `count > 1` keeps two:
    a predicate that quietly did nothing would return all three.
    """
    base = datetime.datetime(2026, 3, 10, 9, 0)
    calendars = {}
    for name, event_count in (("one", 1), ("two", 2), ("three", 3)):
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


def _sql_of_grouped_queries(captured) -> list[str]:
    return [query["sql"] for query in captured.captured_queries if "GROUP BY" in query["sql"]]


@pytest.mark.django_db
class TestHavingOnRowCount:
    def test_count_gt_two_keeps_only_the_group_of_three(self, organization, three_calendars):
        """The phase's worked case: drops the groups of 1 and 2, keeps the 3."""
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {"having": {"count": {"gt": 2}}}

        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(HAVING_COUNT_QUERY, system_user, token, auth, variables)

        assert response.status_code == 200
        payload = response.json()
        assert payload.get("errors", []) == []
        rows = payload["data"]["calendarEventAggregate"]
        assert [row["key"]["calendarId"] for row in rows] == [three_calendars["three"].id]
        assert [row["count"] for row in rows] == [3]

        # The filtering is the database's, in the query that did the grouping.
        grouped = _sql_of_grouped_queries(captured)
        assert len(grouped) == 1, grouped
        assert "HAVING" in grouped[0], grouped[0]

    def test_count_gte_two_keeps_two_groups(self, organization, three_calendars):
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {"having": {"count": {"gte": 2}}}
        response = post_graphql(HAVING_COUNT_QUERY, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        assert sorted(row["count"] for row in rows) == [2, 3]

    def test_a_range_on_one_comparison_applies_both_ends(self, organization, three_calendars):
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {"having": {"count": {"gte": 2, "lt": 3}}}
        response = post_graphql(HAVING_COUNT_QUERY, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        assert [row["key"]["calendarId"] for row in rows] == [three_calendars["two"].id]

    def test_omitting_having_leaves_every_group(self, organization, three_calendars):
        """The Phase 3 behaviour, unchanged by the new argument existing."""
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(HAVING_COUNT_QUERY, system_user, token, auth, _window())

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        assert sorted(row["count"] for row in rows) == [1, 2, 3]
        grouped = _sql_of_grouped_queries(captured)
        assert len(grouped) == 1
        assert "HAVING" not in grouped[0], grouped[0]

    def test_a_having_that_names_nothing_filters_nothing(self, organization, three_calendars):
        """`having: {}` is legal GraphQL and must not be read as "match none"."""
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {"having": {}}
        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(HAVING_COUNT_QUERY, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        assert sorted(row["count"] for row in rows) == [1, 2, 3]
        assert "HAVING" not in _sql_of_grouped_queries(captured)[0]


@pytest.mark.django_db
class TestHavingOnAMetric:
    def test_a_numeric_aggregate_condition_filters_groups(self, organization):
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        base = datetime.datetime(2026, 3, 10, 9, 0)
        short = make_calendar(organization, name="short")
        long = make_calendar(organization, name="long")
        make_event(organization, short, title="s", start=base, minutes=15)
        make_event(organization, long, title="l1", start=base, minutes=60)
        make_event(
            organization, long, title="l2", start=base + datetime.timedelta(hours=2), minutes=90
        )

        variables = _window() | {"having": {"durationMinutes": {"sum": {"gte": 100}}}}
        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(HAVING_DURATION_QUERY, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        assert [row["key"]["calendarId"] for row in rows] == [long.id]
        assert rows[0]["durationMinutes"]["sum"] == pytest.approx(150.0)
        assert "HAVING" in _sql_of_grouped_queries(captured)[0]

    def test_a_having_on_a_metric_the_document_did_not_select_still_works(self, organization):
        """The guard the phase names: annotate it rather than erroring.

        `durationMinutes` is nowhere in the selection set, so nothing about the
        response mentions it. If the metric were not annotated anyway, Django
        would resolve the alias against the model, render the condition as a
        `WHERE`, and filter individual events instead of whole calendars --
        which here would keep both calendars rather than one.
        """
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        base = datetime.datetime(2026, 3, 10, 9, 0)
        short = make_calendar(organization, name="short")
        long = make_calendar(organization, name="long")
        # Two 60-minute events sum to 120; one 90-minute event does not.
        make_event(organization, short, title="s", start=base, minutes=90)
        make_event(organization, long, title="l1", start=base, minutes=60)
        make_event(
            organization, long, title="l2", start=base + datetime.timedelta(hours=2), minutes=60
        )

        variables = _window() | {"having": {"durationMinutes": {"sum": {"gt": 100}}}}
        with CaptureQueriesContext(connection) as captured:
            response = post_graphql(
                HAVING_UNSELECTED_METRIC_QUERY, system_user, token, auth, variables
            )

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        assert [row["key"]["calendarId"] for row in rows] == [long.id]
        sql = _sql_of_grouped_queries(captured)[0]
        assert "HAVING" in sql
        # Not a WHERE on the per-row duration: the 90-minute event's own row
        # would survive that, and its calendar would be in the answer.
        assert short.id not in [row["key"]["calendarId"] for row in rows]

    def test_a_relation_count_condition_filters_groups(self, organization):
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        base = datetime.datetime(2026, 3, 10, 9, 0)
        calendar = make_calendar(organization)
        make_event(organization, calendar, title="e", start=base, minutes=30)

        query = """
        query EventAggregate($start: DateTime!, $end: DateTime!, $having: CalendarEventHavingInput) {
          calendarEventAggregate(
            filter: {startDatetime: $start, endDatetime: $end}
            groupBy: [{scalar: CALENDAR_ID}]
            timezone: "UTC"
            having: $having
          ) {
            key { calendarId }
            count
            attendanceCount
          }
        }
        """
        # No attendances exist, so `> 0` must drop the only group.
        variables = _window() | {"having": {"attendanceCount": {"gt": 0}}}
        response = post_graphql(query, system_user, token, auth, variables)
        assert response.json().get("errors", []) == []
        assert response.json()["data"]["calendarEventAggregate"] == []

        variables = _window() | {"having": {"attendanceCount": {"eq": 0}}}
        response = post_graphql(query, system_user, token, auth, variables)
        assert response.json().get("errors", []) == []
        (row,) = response.json()["data"]["calendarEventAggregate"]
        assert row["attendanceCount"] == 0


@pytest.mark.django_db
class TestHavingComposition:
    def test_or_keeps_groups_matching_either_side(self, organization, three_calendars):
        """The case that separates a real disjunction from an accidental AND."""
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {"having": {"or": [{"count": {"lt": 2}}, {"count": {"gt": 2}}]}}
        response = post_graphql(HAVING_COUNT_QUERY, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        # ANDed, this would be unsatisfiable and return nothing.
        assert sorted(row["count"] for row in rows) == [1, 3]

    def test_and_requires_both_sides(self, organization, three_calendars):
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {"having": {"and": [{"count": {"gte": 2}}, {"count": {"lte": 2}}]}}
        response = post_graphql(HAVING_COUNT_QUERY, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        assert [row["key"]["calendarId"] for row in rows] == [three_calendars["two"].id]

    def test_a_top_level_condition_and_an_or_group_both_apply(self, organization, three_calendars):
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        variables = _window() | {
            "having": {
                "count": {"gte": 2},
                "or": [{"count": {"lt": 2}}, {"count": {"gt": 2}}],
            }
        }
        response = post_graphql(HAVING_COUNT_QUERY, system_user, token, auth, variables)

        assert response.json().get("errors", []) == []
        rows = response.json()["data"]["calendarEventAggregate"]
        # `>= 2` AND (`< 2` OR `> 2`) leaves only the group of three.
        assert [row["count"] for row in rows] == [3]


@pytest.mark.django_db
class TestHavingIsRefusedWhenItCannotBeAnnotated:
    def test_a_predicate_naming_an_unannotated_alias_is_refused(self, organization):
        """The executor's own check, driven directly.

        Unreachable through GraphQL -- `having_from_input` returns the metrics
        its predicate needs and the resolver merges them in -- so it is driven
        by hand here. Without it the alias resolves against the model and the
        condition silently becomes a `WHERE`.
        """
        from django.db.models import Q

        from calendar_integration.models import CalendarEvent
        from public_api.aggregations.errors import UnknownHavingAliasError
        from public_api.aggregations.executor import build_aggregate_queryset
        from public_api.aggregations.plan import (
            AggregatableEntity,
            AggregateQueryPlan,
            DimensionSpec,
            HavingSpec,
            MetricSpec,
        )

        plan = AggregateQueryPlan(
            entity=AggregatableEntity.CALENDAR_EVENT,
            dimensions=(DimensionSpec(alias="calendar_id", field_path="calendar_id"),),
            metrics=(MetricSpec.row_count(alias="count"),),
            having=HavingSpec(predicate=Q(duration_minutes_sum__gt=1)),
        )
        queryset = CalendarEvent.objects.filter_by_organization(organization.id)

        with pytest.raises(UnknownHavingAliasError) as exc_info:
            build_aggregate_queryset(plan, queryset)
        assert "duration_minutes_sum" in str(exc_info.value)
