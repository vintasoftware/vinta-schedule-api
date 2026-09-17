"""Integration test that aggregate queries with audit hooks still work."""

import datetime

import pytest
from rest_framework.test import APIClient

from calendar_integration.models import CalendarEvent
from organizations.models import Organization
from public_api.services import PublicAPIAuthService


@pytest.mark.django_db
def test_aggregate_query_runs_successfully_with_audit_hook(
    organization: Organization,
):
    """Running a calendarEventAggregate query succeeds with audit recording enabled."""
    # Create system user and token.
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name="test-aggregate-audit", organization=organization, bypass_limits=True
    )

    # Create test events.
    calendar = organization.calendar_set.create(name="Test Calendar")
    now = datetime.datetime.now(datetime.UTC)
    CalendarEvent.objects.create(
        calendar=calendar,
        start_time_tz_unaware=now,
        end_time_tz_unaware=now + datetime.timedelta(hours=1),
        timezone="UTC",
        title="Event 1",
    )
    CalendarEvent.objects.create(
        calendar=calendar,
        start_time_tz_unaware=now + datetime.timedelta(days=1),
        end_time_tz_unaware=now + datetime.timedelta(days=1, hours=1),
        timezone="UTC",
        title="Event 2",
    )

    # Run the aggregate query.
    client = APIClient()
    start_dt = now.isoformat()
    end_dt = (now + datetime.timedelta(days=7)).isoformat()
    query = f"""
        query {{
            calendarEventAggregate(
                filter: {{
                    startDatetime: "{start_dt}"
                    endDatetime: "{end_dt}"
                }}
                groupBy: []
                timezone: "UTC"
                limit: 10
                offset: 0
            ) {{
                count
            }}
        }}
    """

    response = client.post(
        "/graphql/",
        data={"query": query},
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}",
    )

    # Verify the query succeeded and returned the expected data.
    assert response.status_code == 200
    data = response.json()
    assert "errors" not in data or not data.get("errors")
    assert data["data"]["calendarEventAggregate"][0]["count"] == 2


@pytest.mark.django_db
def test_aggregate_query_with_zero_rows_succeeds_with_audit(
    organization: Organization,
):
    """A query returning zero rows succeeds with audit recording."""
    # Create system user and token.
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name="test-aggregate-zero", organization=organization, bypass_limits=True
    )

    now = datetime.datetime.now(datetime.UTC)

    # Run a query that will return zero rows.
    client = APIClient()
    start_dt = now.isoformat()
    end_dt = (now + datetime.timedelta(days=1)).isoformat()
    query = f"""
        query {{
            calendarEventAggregate(
                filter: {{
                    startDatetime: "{start_dt}"
                    endDatetime: "{end_dt}"
                }}
                groupBy: []
                timezone: "UTC"
                limit: 10
                offset: 0
            ) {{
                count
            }}
        }}
    """

    response = client.post(
        "/graphql/",
        data={"query": query},
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}",
    )

    assert response.status_code == 200
    data = response.json()
    assert "errors" not in data or not data.get("errors")
    assert data["data"]["calendarEventAggregate"][0]["count"] == 0
