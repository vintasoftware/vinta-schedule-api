"""Who may read an aggregate, and how much of one.

The case worth reading twice is the cross-tenant one. A ``GROUP BY`` over an
insufficiently scoped queryset does not raise — it returns plausible numbers
that quietly include another organization's rows. So the assertions here are
about what is *absent* from the result, not only about what is in it.
"""

import pytest

from public_api.aggregations.fields import aggregate_field_name
from public_api.aggregations.plan import AggregatableEntity
from public_api.constants import PublicAPIResources
from public_api.tests.aggregations.conftest import (
    assert_ok,
    event_window_variables,
    graphql_errors,
    make_calendar,
    make_event,
    make_membership_for,
    org_wide_token,
    own,
    post_graphql,
    scoped_token,
)


PERMISSION_DENIED = "You don't have access to query this resource."
NOT_AUTHENTICATED = "You must be authenticated to access this resource."

EVENTS_BY_CALENDAR = """
query EventAggregate($filter: CalendarEventAggregateFilterInput!) {
    calendarEventAggregate(
        filter: $filter
        groupBy: [{scalar: CALENDAR_ID}]
        timezone: "UTC"
    ) {
        key { calendarId }
        count
        title { concat }
    }
}
"""


#: The three entities whose filter input demands a bounded range at the type level.
TEMPORAL_ENTITIES = (
    AggregatableEntity.CALENDAR_EVENT,
    AggregatableEntity.AVAILABLE_TIME,
    AggregatableEntity.BLOCKED_TIME,
)


def _probe_query(entity: AggregatableEntity) -> str:
    """The smallest document that reaches one entity's aggregate field.

    ``groupBy`` is empty and the resolver would refuse it — which is the point:
    the permission check has to run first, so the error is the permission error
    rather than the plan one.
    """
    bounds = (
        'startDatetime: "2026-03-01T00:00:00+00:00", endDatetime: "2026-04-01T00:00:00+00:00"'
        if entity in TEMPORAL_ENTITIES
        else ""
    )
    return f"""
    query Probe {{
        {aggregate_field_name(entity)}(
            filter: {{{bounds}}}
            groupBy: []
            timezone: "UTC"
        ) {{ count }}
    }}
    """


@pytest.mark.django_db
class TestResourceScope:
    def test_a_token_without_the_event_resource_is_refused(self, api_client, organization):
        calendar = make_calendar(organization, "A")
        make_event(organization, calendar, title="Alpha", day=2, minutes=30)
        # The token holds a different resource, so it is authenticated but
        # ungranted for this field.
        credentials = org_wide_token(organization, [PublicAPIResources.CALENDAR])

        response = post_graphql(
            api_client, EVENTS_BY_CALENDAR, credentials, event_window_variables()
        )

        assert response.status_code == 200
        assert PERMISSION_DENIED in graphql_errors(response)
        # The field is non-null, so the refusal nulls the whole selection rather
        # than handing back an empty list a caller might read as "no events".
        assert response.json()["data"] is None

    def test_a_token_with_the_event_resource_succeeds(self, api_client, organization):
        calendar = make_calendar(organization, "A")
        make_event(organization, calendar, title="Alpha", day=2, minutes=30)
        credentials = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])

        response = post_graphql(
            api_client, EVENTS_BY_CALENDAR, credentials, event_window_variables()
        )

        rows = assert_ok(response)["calendarEventAggregate"]
        assert rows == [
            {
                "key": {"calendarId": calendar.id},
                "count": 1,
                "title": {"concat": "Alpha"},
            }
        ]

    def test_an_unauthenticated_request_is_refused(self, api_client, organization):
        response = api_client.post(
            "/graphql/",
            data={"query": EVENTS_BY_CALENDAR, "variables": event_window_variables()},
            format="json",
        )

        assert response.status_code == 200
        assert NOT_AUTHENTICATED in graphql_errors(response)

    def test_the_calendar_resource_does_not_unlock_the_event_aggregate(
        self, api_client, organization
    ):
        """Each aggregate needs its own entity's resource, not a neighbour's."""
        calendar = make_calendar(organization, "A")
        make_event(organization, calendar, title="Alpha", day=2, minutes=30)
        credentials = org_wide_token(organization, [PublicAPIResources.CALENDAR])

        calendars = post_graphql(
            api_client,
            """
            query Calendars($filter: CalendarAggregateFilterInput!) {
                calendarAggregate(
                    filter: $filter
                    groupBy: [{scalar: PROVIDER}]
                    timezone: "UTC"
                ) { count }
            }
            """,
            credentials,
            {"filter": {}},
        )
        events = post_graphql(api_client, EVENTS_BY_CALENDAR, credentials, event_window_variables())

        assert assert_ok(calendars)["calendarAggregate"] == [{"count": 1}]
        assert PERMISSION_DENIED in graphql_errors(events)

    @pytest.mark.parametrize("entity", tuple(AggregatableEntity))
    def test_every_field_refuses_a_token_holding_no_resource_at_all(
        self, api_client, organization, entity
    ):
        credentials = org_wide_token(organization, [])

        response = post_graphql(api_client, _probe_query(entity), credentials)

        assert PERMISSION_DENIED in graphql_errors(response)


@pytest.mark.django_db
class TestOwnerScoping:
    def test_a_scoped_token_sees_no_contribution_from_another_owner(self, api_client, organization):
        """Calendar B's events must not reach a token scoped to calendar A."""
        owner, membership = make_membership_for(organization)
        calendar_a = make_calendar(organization, "A")
        calendar_b = make_calendar(organization, "B")
        own(organization, owner, calendar_a)

        make_event(organization, calendar_a, title="Mine", day=2, minutes=30)
        make_event(organization, calendar_b, title="Theirs", day=2, minutes=120)
        make_event(organization, calendar_b, title="AlsoTheirs", day=3, minutes=120)

        credentials = scoped_token(organization, membership, [PublicAPIResources.CALENDAR_EVENT])

        response = post_graphql(
            api_client, EVENTS_BY_CALENDAR, credentials, event_window_variables()
        )

        rows = assert_ok(response)["calendarEventAggregate"]
        assert rows == [
            {
                "key": {"calendarId": calendar_a.id},
                "count": 1,
                "title": {"concat": "Mine"},
            }
        ]
        # The other owner's titles are not merely un-grouped — they are absent
        # from the concatenated string too.
        assert "Theirs" not in rows[0]["title"]["concat"]

    def test_a_scoped_token_owning_nothing_gets_an_empty_result(self, api_client, organization):
        _owner, membership = make_membership_for(organization)
        calendar = make_calendar(organization, "Not mine")
        make_event(organization, calendar, title="Theirs", day=2, minutes=30)

        credentials = scoped_token(organization, membership, [PublicAPIResources.CALENDAR_EVENT])

        response = post_graphql(
            api_client, EVENTS_BY_CALENDAR, credentials, event_window_variables()
        )

        assert assert_ok(response)["calendarEventAggregate"] == []


@pytest.mark.django_db
class TestCrossTenant:
    def test_another_organizations_rows_contribute_nothing(
        self, api_client, organization, other_organization
    ):
        """An empty result, not the other tenant's numbers."""
        their_calendar = make_calendar(other_organization, "Theirs")
        make_event(other_organization, their_calendar, title="Theirs", day=2, minutes=300)
        make_event(other_organization, their_calendar, title="AlsoTheirs", day=3, minutes=300)

        credentials = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])

        response = post_graphql(
            api_client, EVENTS_BY_CALENDAR, credentials, event_window_variables()
        )

        assert assert_ok(response)["calendarEventAggregate"] == []

    def test_only_this_tenants_rows_are_aggregated_when_both_have_some(
        self, api_client, organization, other_organization
    ):
        mine = make_calendar(organization, "Mine")
        make_event(organization, mine, title="Mine", day=2, minutes=30)

        theirs = make_calendar(other_organization, "Theirs")
        make_event(other_organization, theirs, title="Theirs", day=2, minutes=300)

        credentials = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])

        response = post_graphql(
            api_client, EVENTS_BY_CALENDAR, credentials, event_window_variables()
        )

        rows = assert_ok(response)["calendarEventAggregate"]
        assert [row["key"]["calendarId"] for row in rows] == [mine.id]
        assert rows[0]["count"] == 1
        assert rows[0]["title"]["concat"] == "Mine"

    def test_naming_another_tenants_calendar_in_the_filter_returns_nothing(
        self, api_client, organization, other_organization
    ):
        """A filter is not a way out of the tenant's own rows."""
        theirs = make_calendar(other_organization, "Theirs")
        make_event(other_organization, theirs, title="Theirs", day=2, minutes=300)

        credentials = org_wide_token(organization, [PublicAPIResources.CALENDAR_EVENT])

        response = post_graphql(
            api_client,
            EVENTS_BY_CALENDAR,
            credentials,
            event_window_variables(calendarId=theirs.id),
        )

        assert assert_ok(response)["calendarEventAggregate"] == []
