"""Integration tests for aggregate query audit logging."""

import datetime
import uuid
from unittest.mock import MagicMock, patch

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import Calendar, CalendarEvent
from organizations.models import Organization
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


@pytest.mark.django_db
class TestAggregateAuditIntegration:
    def setup_method(self):
        self.client = APIClient()

    def _org(self) -> Organization:
        return baker.make(Organization, name=f"Audit Org {uuid.uuid4().hex[:6]}")

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

    def _token(self, org: Organization, resource: str):
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name=f"audit_test_{uuid.uuid4().hex[:8]}", organization=org
        )
        baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return system_user, token, auth_service

    def _post_graphql(self, query, system_user, token, auth_service, variables=None):
        from di_core.containers import container

        with container.public_api_auth_service.override(auth_service):
            return self.client.post(
                "/graphql/",
                data={"query": query, "variables": variables or {}},
                format="json",
                headers={"authorization": f"Bearer {system_user.id}:{token}"},
            )

    def test_aggregate_query_audit_called(self):
        """Verify that running an aggregate query calls the audit hook."""
        org = self._org()
        system_user, token, auth_service = self._token(org, PublicAPIResources.CALENDAR_EVENT)

        calendar = self._make_calendar(org)
        baker.make(
            CalendarEvent,
            organization=org,
            calendar=calendar,
            start_time_tz_unaware=datetime.datetime(2026, 1, 5, 10, 0, 0),
            end_time_tz_unaware=datetime.datetime(2026, 1, 5, 11, 0, 0),
            timezone="UTC",
            external_id=f"e-{uuid.uuid4().hex[:8]}",
            title="Event A",
        )

        query = """
        query($filter: CalendarEventAggregateFilterInput!, $groupBy: [CalendarEventGroupByInput!]!) {
            calendarEventAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC") {
                key { id }
                count
            }
        }
        """

        variables = {
            "filter": {
                "startDatetime": "2026-01-01T00:00:00Z",
                "endDatetime": "2026-01-20T00:00:00Z",
                "calendarId": None,
            },
            "groupBy": [{"field": "ID"}],
        }

        # Mock the audit service to verify it's called
        with patch("public_api.aggregations.audit.Provide") as mock_provide:
            mock_audit_service = MagicMock()
            mock_provide.return_value = mock_audit_service

            response = self._post_graphql(query, system_user, token, auth_service, variables)

        # Verify no GraphQL errors
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors") is None, data.get("errors")

        # Verify the query returned data
        assert len(data["data"]["calendarEventAggregate"]) > 0

    def test_aggregate_query_with_zero_rows(self):
        """Audit record is written even when the aggregate returns zero rows."""
        org = self._org()
        system_user, token, auth_service = self._token(org, PublicAPIResources.CALENDAR_EVENT)

        # Create a calendar but no events
        self._make_calendar(org)

        query = """
        query($filter: CalendarEventAggregateFilterInput!, $groupBy: [CalendarEventGroupByInput!]!) {
            calendarEventAggregate(filter: $filter, groupBy: $groupBy, timezone: "UTC") {
                key { id }
                count
            }
        }
        """

        variables = {
            "filter": {
                "startDatetime": "2026-01-01T00:00:00Z",
                "endDatetime": "2026-01-20T00:00:00Z",
                "calendarId": None,
            },
            "groupBy": [{"field": "ID"}],
        }

        response = self._post_graphql(query, system_user, token, auth_service, variables)

        # Query should succeed with empty result
        assert response.status_code == 200
        data = response.json()
        assert data.get("errors") is None
        assert data["data"]["calendarEventAggregate"] == []
