"""Integration test that aggregate queries with audit hooks still work."""

import datetime

import pytest

from organizations.models import Organization
from public_api.tests.aggregations.conftest import (
    WINDOW_END,
    WINDOW_START,
    make_calendar,
    make_event,
    org_wide_token,
    post_graphql,
)


@pytest.mark.django_db
def test_aggregate_query_runs_with_audit_hook(
    organization: Organization,
):
    """Running a calendarEventAggregate query with audit hook enabled succeeds."""
    # Create a calendar and two events.
    calendar = make_calendar(organization)
    make_event(
        organization,
        calendar,
        title="Event 1",
        start=WINDOW_START,
        minutes=60,
    )
    make_event(
        organization,
        calendar,
        title="Event 2",
        start=WINDOW_START + datetime.timedelta(days=1),
        minutes=60,
    )

    # Create a token with the CALENDAR_EVENT resource.
    system_user, token, auth_service = org_wide_token(
        organization, ["calendar_event"]
    )

    # Run the aggregate query with at least one group-by dimension.
    query = f"""
        query {{
            calendarEventAggregate(
                filter: {{
                    startDatetime: "{WINDOW_START.isoformat()}"
                    endDatetime: "{WINDOW_END.isoformat()}"
                }}
                groupBy: [{{temporal: {{field: START_TIME, granularity: DAY}}}}]
                timezone: "UTC"
                limit: 10
                offset: 0
            ) {{
                count
            }}
        }}
    """

    response = post_graphql(query, system_user, token, auth_service)

    # Verify the query succeeded and returned the expected data.
    assert response.status_code == 200
    data = response.json()
    assert "errors" not in data or not data.get("errors")
    # Two events, grouped by day: should be 2 groups.
    assert len(data["data"]["calendarEventAggregate"]) == 2


@pytest.mark.django_db
def test_aggregate_query_with_zero_rows_runs_with_audit(
    organization: Organization,
):
    """A query returning zero rows still runs successfully with audit hook."""
    # Create a calendar but no events.
    make_calendar(organization)

    # Create a token with the CALENDAR_EVENT resource.
    system_user, token, auth_service = org_wide_token(
        organization, ["calendar_event"]
    )

    # Run a query that will return zero rows (outside the window).
    far_future = WINDOW_END + datetime.timedelta(days=30)
    far_future_end = far_future + datetime.timedelta(days=1)
    query = f"""
        query {{
            calendarEventAggregate(
                filter: {{
                    startDatetime: "{far_future.isoformat()}"
                    endDatetime: "{far_future_end.isoformat()}"
                }}
                groupBy: [{{temporal: {{field: START_TIME, granularity: DAY}}}}]
                timezone: "UTC"
                limit: 10
                offset: 0
            ) {{
                count
            }}
        }}
    """

    response = post_graphql(query, system_user, token, auth_service)

    assert response.status_code == 200
    data = response.json()
    assert "errors" not in data or not data.get("errors")
