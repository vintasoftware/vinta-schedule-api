"""Who may run an aggregate, and over which rows.

Three questions, and the third is the one worth the integration cost: an
aggregate is a number computed *over* rows, so a scoping bug does not return
another tenant's row -- it folds that row into a count and returns a number
that looks exactly like a correct one. The cross-scope cases below therefore
assert an empty result or a number that excludes the out-of-scope rows, never
just "not equal to the other tenant's".
"""

import datetime

import pytest
from model_bakery import baker

from organizations.models import Organization
from public_api.constants import PublicAPIResources
from public_api.tests.aggregations.conftest import (
    WINDOW_END,
    WINDOW_START,
    make_calendar,
    make_event,
    make_member,
    org_wide_token,
    own_calendar,
    post_graphql,
    scoped_token,
)


EVENT_AGGREGATE_QUERY = """
query EventAggregate($start: DateTime!, $end: DateTime!) {
  calendarEventAggregate(
    filter: {startDatetime: $start, endDatetime: $end}
    groupBy: [{scalar: CALENDAR_ID}]
    timezone: "UTC"
  ) {
    key { calendarId }
    count
    durationMinutes { sum }
  }
}
"""


def _window() -> dict[str, str]:
    return {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()}


@pytest.mark.django_db
class TestAggregateResourceScope:
    def test_token_without_the_resource_is_refused(self, organization):
        """The standard `OrganizationResourceAccess` message, unchanged."""
        calendar = make_calendar(organization)
        make_event(
            organization,
            calendar,
            title="Hidden",
            start=datetime.datetime(2026, 3, 10, 9, 0),
            minutes=30,
        )
        # A real, active token -- just not one carrying CALENDAR_EVENT.
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR])

        response = post_graphql(EVENT_AGGREGATE_QUERY, system_user, token, auth, _window())

        assert response.status_code == 200
        payload = response.json()
        messages = [error["message"] for error in payload.get("errors", [])]
        assert messages == ["You don't have access to query this resource."]
        assert payload["data"] is None or payload["data"]["calendarEventAggregate"] is None

    def test_token_with_the_resource_succeeds(self, organization):
        calendar = make_calendar(organization)
        make_event(
            organization,
            calendar,
            title="Visible",
            start=datetime.datetime(2026, 3, 10, 9, 0),
            minutes=30,
        )
        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])

        response = post_graphql(EVENT_AGGREGATE_QUERY, system_user, token, auth, _window())

        assert response.status_code == 200
        payload = response.json()
        assert payload.get("errors", []) == []
        (row,) = payload["data"]["calendarEventAggregate"]
        assert row["key"]["calendarId"] == calendar.id
        assert row["count"] == 1

    def test_unauthenticated_request_is_refused(self, organization):
        """No token at all fails the first permission class, not the second."""
        from rest_framework.test import APIClient

        response = APIClient().post(
            "/graphql/",
            data={"query": EVENT_AGGREGATE_QUERY, "variables": _window()},
            format="json",
        )

        assert response.status_code == 200
        messages = [error["message"] for error in response.json().get("errors", [])]
        assert messages == ["You must be authenticated to access this resource."]


@pytest.mark.django_db
class TestAggregateOwnerScope:
    def test_scoped_token_sees_no_contribution_from_another_calendar(self, organization):
        """A token scoped to calendar A gets A's numbers, and only A's.

        The assertion that matters is the *absence* of calendar B's group and
        the exactness of A's rollup: a scoped token that leaked B would return
        two groups, or one group whose sum silently included B's 90 minutes.
        """
        member_user, membership = make_member(organization)
        calendar_a = make_calendar(organization, name="A")
        calendar_b = make_calendar(organization, name="B")
        own_calendar(organization, member_user, calendar_a)

        base = datetime.datetime(2026, 3, 10, 9, 0)
        make_event(organization, calendar_a, title="A1", start=base, minutes=30)
        make_event(organization, calendar_b, title="B1", start=base, minutes=90)

        system_user, token, auth = scoped_token(
            organization, membership, [PublicAPIResources.CALENDAR_EVENT]
        )
        response = post_graphql(EVENT_AGGREGATE_QUERY, system_user, token, auth, _window())

        assert response.status_code == 200
        payload = response.json()
        assert payload.get("errors", []) == []
        rows = payload["data"]["calendarEventAggregate"]
        assert [row["key"]["calendarId"] for row in rows] == [calendar_a.id]
        assert rows[0]["count"] == 1
        assert rows[0]["durationMinutes"]["sum"] == pytest.approx(30.0)

    def test_scoped_token_owning_nothing_gets_an_empty_result(self, organization):
        """Fail closed: an empty result, not the organization's numbers."""
        _member_user, membership = make_member(organization)
        calendar = make_calendar(organization)
        make_event(
            organization,
            calendar,
            title="Not mine",
            start=datetime.datetime(2026, 3, 10, 9, 0),
            minutes=30,
        )

        system_user, token, auth = scoped_token(
            organization, membership, [PublicAPIResources.CALENDAR_EVENT]
        )
        response = post_graphql(EVENT_AGGREGATE_QUERY, system_user, token, auth, _window())

        assert response.status_code == 200
        payload = response.json()
        assert payload.get("errors", []) == []
        assert payload["data"]["calendarEventAggregate"] == []


@pytest.mark.django_db
class TestAggregateTenantScope:
    def test_another_organizations_rows_are_absent_not_merged(self, organization):
        """An aggregate over two tenants would look like a correct answer."""
        other_org = baker.make(Organization, name="Other")
        mine = make_calendar(organization, name="Mine")
        theirs = make_calendar(other_org, name="Theirs")

        base = datetime.datetime(2026, 3, 10, 9, 0)
        make_event(organization, mine, title="Mine", start=base, minutes=30)
        make_event(other_org, theirs, title="Theirs 1", start=base, minutes=60)
        make_event(
            other_org,
            theirs,
            title="Theirs 2",
            start=base + datetime.timedelta(hours=2),
            minutes=60,
        )

        system_user, token, auth = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])
        response = post_graphql(EVENT_AGGREGATE_QUERY, system_user, token, auth, _window())

        assert response.status_code == 200
        payload = response.json()
        assert payload.get("errors", []) == []
        rows = payload["data"]["calendarEventAggregate"]
        assert [row["key"]["calendarId"] for row in rows] == [mine.id]
        assert rows[0]["count"] == 1
        assert rows[0]["durationMinutes"]["sum"] == pytest.approx(30.0)
