"""Integration test that aggregate queries write audit records."""

import datetime

import pytest

from audit_integration.constants import AuditAction
from audit_integration.repositories import OrganizationAuditRepository
from audit_integration.types import OrganizationAuditQuery
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
def test_aggregate_query_writes_audit_record(
    organization: Organization,
):
    """Running a calendarEventAggregate query writes an AGGREGATE_QUERY audit record."""
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
                groupBy: [{{temporal: {{field: "startTime", granularity: "DAY"}}}}]
                timezone: "UTC"
                limit: 10
                offset: 0
            ) {{
                count
            }}
        }}
    """

    response = post_graphql(query, system_user, token, auth_service)

    # Verify the query succeeded.
    assert response.status_code == 200
    data = response.json()
    assert "errors" not in data or not data.get("errors")
    # Two events, grouped by day: should be 2 groups.
    assert len(data["data"]["calendarEventAggregate"]) == 2

    # Verify exactly one AGGREGATE_QUERY audit record was written.
    repository = OrganizationAuditRepository()

    page = repository.query(
        OrganizationAuditQuery(
            actions=[AuditAction.AGGREGATE_QUERY],
            organization_ids=[organization.id],
        ),
        limit=50,
    )

    assert page.total == 1
    record = page.items[0]

    # Verify the record has the expected shape.
    assert record.action_key == AuditAction.AGGREGATE_QUERY
    assert record.scope.scope_key == str(organization.id)
    assert record.actor.identity_type == "system_user"

    # Verify the payload (diff) contains expected fields.
    diff = record.diff or {}
    assert diff.get("entity") == "calendar_event"
    assert diff.get("row_count") == 2
    assert "metrics" in diff
    assert "filter_bounds" in diff
    assert "dimensions" in diff


@pytest.mark.django_db
def test_aggregate_query_with_zero_rows_writes_audit(
    organization: Organization,
):
    """A query returning zero rows still writes an audit record."""
    # Create a calendar but no events (not used, but needed for scope).
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
                groupBy: [{{temporal: {{field: "startTime", granularity: "DAY"}}}}]
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

    # Verify the audit record was still written.
    repository = OrganizationAuditRepository()

    page = repository.query(
        OrganizationAuditQuery(
            actions=[AuditAction.AGGREGATE_QUERY],
            organization_ids=[organization.id],
        ),
        limit=50,
    )

    assert page.total == 1
    record = page.items[0]
    assert (record.diff or {}).get("row_count") == 0
