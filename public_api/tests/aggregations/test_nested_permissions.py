"""A nested aggregate is gated by what it aggregates, not by what carries it.

Nesting is the one place where a resource grant could quietly widen: a token
that may list calendars reaches `calendar.eventAggregate` through a field the
`CALENDAR` grant already opened, and `OrganizationResourceAccess` only runs on
the field it decorates. So the assertion that matters here is the negative one
-- a `CALENDAR`-only token reads the calendar and is refused its events -- and
it is asserted through the real endpoint, because the permission class is part
of that path and not of a resolver call.
"""

import datetime

from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest

from public_api.aggregations.nested import NESTED_RESOURCE_BY_FIELD_NAME
from public_api.constants import PublicAPIResources
from public_api.permissions import OrganizationResourceAccess
from public_api.tests.aggregations.conftest import (
    WINDOW_END,
    WINDOW_START,
    make_calendar,
    make_event,
    org_wide_token,
    post_graphql,
)


NESTED_QUERY = """
query Nested($start: DateTime!, $end: DateTime!) {
  calendars(limit: 100) {
    id
    name
    eventAggregate(
      filter: {startDatetime: $start, endDatetime: $end}
      groupBy: [{temporal: {field: START_TIME, granularity: DAY}}]
      timezone: "UTC"
    ) { count }
  }
}
"""

CALENDAR_ONLY_QUERY = """
query Calendars {
  calendars(limit: 100) { id name }
}
"""

NESTED_BLOCKED_TIME_QUERY = """
query Nested($start: DateTime!, $end: DateTime!) {
  calendars(limit: 100) {
    id
    blockedTimeAggregate(
      filter: {startDatetime: $start, endDatetime: $end}
      groupBy: [{temporal: {field: START_TIME, granularity: DAY}}]
      timezone: "UTC"
    ) { count }
  }
}
"""


def _window() -> dict[str, str]:
    return {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()}


@pytest.fixture
def calendar_with_events(organization):
    calendar = make_calendar(organization, name="gated")
    for day in (1, 2, 3):
        make_event(
            organization,
            calendar,
            title=f"d{day}",
            start=datetime.datetime(2026, 3, day, 9, 0),
            minutes=30,
        )
    return calendar


@pytest.mark.django_db
class TestTheAggregatedEntitySResourceIsRequired:
    def test_a_calendar_only_token_is_refused_the_nested_event_aggregate(
        self, organization, calendar_with_events
    ):
        """The gate that stops nesting from being a back door into events."""
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR])

        response = post_graphql(NESTED_QUERY, system_user, token, auth, _window())

        body = response.json()
        messages = [error["message"] for error in body.get("errors", [])]
        assert messages == [OrganizationResourceAccess.message]

    def test_the_same_token_still_reads_the_calendar_itself(
        self, organization, calendar_with_events
    ):
        """Refusing the aggregate must not refuse the parent it hangs off."""
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR])

        response = post_graphql(CALENDAR_ONLY_QUERY, system_user, token, auth)

        body = response.json()
        assert body.get("errors", []) == []
        assert [row["name"] for row in body["data"]["calendars"]] == ["gated"]

    def test_the_refusal_nulls_the_whole_result_rather_than_the_one_field(
        self, organization, calendar_with_events
    ):
        """Documented, not aspired to: the refusal propagates up the non-null chain.

        A nested aggregate returns a non-null list, as the six root fields do,
        and `calendars` is a non-null list of non-null calendars -- so GraphQL's
        error propagation has no nullable field to stop at and `data` comes back
        null. That is what every other permission refusal on this API already
        does, and making this one field nullable to soften it would be a schema
        decision about partial responses that is not this phase's to take. A
        partner reading the calendars without the rollup asks for the calendars
        without the rollup, and gets them -- the test above.
        """
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR])

        body = post_graphql(NESTED_QUERY, system_user, token, auth, _window()).json()

        assert body["data"] is None
        assert [error["message"] for error in body["errors"]] == [
            OrganizationResourceAccess.message
        ]

    def test_both_grants_together_return_the_rollup(self, organization, calendar_with_events):
        """The positive control: nothing above is a blanket refusal."""
        system_user, token, auth = org_wide_token(
            organization, [PublicAPIResources.CALENDAR, PublicAPIResources.CALENDAR_EVENT]
        )

        body = post_graphql(NESTED_QUERY, system_user, token, auth, _window()).json()

        assert body.get("errors", []) == []
        rows = body["data"]["calendars"][0]["eventAggregate"]
        assert [row["count"] for row in rows] == [1, 1, 1]

    def test_the_event_grant_alone_does_not_open_the_parent_list(
        self, organization, calendar_with_events
    ):
        """Inheritance runs one way only: the parent keeps its own gate."""
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])

        body = post_graphql(NESTED_QUERY, system_user, token, auth, _window()).json()

        messages = [error["message"] for error in body.get("errors", [])]
        assert messages == [OrganizationResourceAccess.message]

    def test_the_nested_blocked_time_aggregate_needs_the_blocked_time_grant(
        self, organization, calendar_with_events
    ):
        """`blockedTimeAggregate` shares the root field's name and its resource."""
        system_user, token, auth = org_wide_token(
            organization, [PublicAPIResources.CALENDAR, PublicAPIResources.CALENDAR_EVENT]
        )

        body = post_graphql(NESTED_BLOCKED_TIME_QUERY, system_user, token, auth, _window()).json()

        messages = [error["message"] for error in body.get("errors", [])]
        assert messages == [OrganizationResourceAccess.message]


class TestTheMappingAgreesWithTheRegistrations:
    def test_every_nested_field_has_the_entry_its_registration_expects(self):
        """`FIELD_TO_RESOURCE_MAPPING` is hand-written; this is what pins it.

        Written out there rather than imported because `nested.py` imports the
        permission classes, so the reverse import would be a cycle -- which
        leaves this assertion as the thing that keeps the two in step.
        """
        for field_name, resource in NESTED_RESOURCE_BY_FIELD_NAME.items():
            assert OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING[field_name] == resource

    def test_no_nested_field_falls_through_to_its_own_name(self):
        """An unmapped field is checked against a resource no token can hold.

        That fails closed, which is the right direction, but it fails closed for
        everyone -- so it would look like a broken field rather than a missing
        mapping entry.
        """
        for field_name in NESTED_RESOURCE_BY_FIELD_NAME:
            assert field_name in OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING


@pytest.mark.django_db
class TestTheGateIsAskedOnceRatherThanOncePerParent:
    def test_the_resource_check_does_not_issue_a_query_per_calendar(self, organization):
        """A per-field `EXISTS` is a constant at the root and an N+1 under a list.

        Twenty calendars would have asked the same question twenty times, which
        would have made the batched aggregate beneath it pointless.
        """
        system_user, token, auth = org_wide_token(
            organization, [PublicAPIResources.CALENDAR, PublicAPIResources.CALENDAR_EVENT]
        )
        for index in range(20):
            calendar = make_calendar(organization, name=f"perm-{index}")
            make_event(
                organization,
                calendar,
                title=f"e{index}",
                start=datetime.datetime(2026, 3, 1, 9, 0),
                minutes=30,
            )

        with CaptureQueriesContext(connection) as captured:
            body = post_graphql(NESTED_QUERY, system_user, token, auth, _window()).json()

        assert body.get("errors", []) == []
        resource_queries = [
            query["sql"]
            for query in captured.captured_queries
            if "public_api_resourceaccess" in query["sql"]
        ]
        assert len(resource_queries) <= 2, resource_queries
