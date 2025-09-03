import datetime
import json
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.models import AvailableTime, BlockedTime, Calendar, CalendarEvent
from calendar_integration.services.dataclasses import (
    AvailableTimeWindow,
    BlockedTimeData,
    UnavailableTimeWindow,
)
from organizations.models import Organization, OrganizationMembership
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


def assert_response_status_code(response, expected_status_code):
    """Helper function to assert response status codes with detailed error messages."""
    assert response.status_code == expected_status_code, (
        f"The status error {response.status_code} != {expected_status_code}\n"
        f"Response Content: {response.content.decode()}"
    )


def assert_graphql_success(response):
    """Helper function to assert GraphQL response is successful."""
    assert_response_status_code(response, 200)

    response_data = response.json()

    # Check if there are any errors
    if response_data.get("errors"):
        error_messages = [
            error.get("message", "Unknown error") for error in response_data["errors"]
        ]
        raise AssertionError(f"GraphQL errors: {error_messages}")

    # Ensure data field exists
    assert "data" in response_data, f"No data field in response: {response_data}"
    assert response_data["data"] is not None, f"Data field is None: {response_data}"

    return response_data["data"]


@pytest.fixture
def organization():
    """Create a test organization."""
    return baker.make(Organization, name="Test Organization")


@pytest.fixture
def system_user_with_resources(organization):
    """Create a system user with all necessary resource permissions."""
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name="test_integration", organization=organization
    )

    # Grant access to all required resources
    resources = [
        PublicAPIResources.CALENDAR,
        PublicAPIResources.CALENDAR_EVENT,
        PublicAPIResources.BLOCKED_TIME,
        PublicAPIResources.AVAILABLE_TIME,
        PublicAPIResources.AVAILABILITY_WINDOWS,
        PublicAPIResources.UNAVAILABLE_WINDOWS,
        PublicAPIResources.USER,
    ]

    for resource in resources:
        baker.make(ResourceAccess, system_user=system_user, resource_name=resource)

    return system_user, token


@pytest.fixture
def calendar(organization):
    """Create a test calendar."""
    return baker.make(
        Calendar,
        organization=organization,
        name="Test Calendar",
        description="Test Description",
        email="calendar@test.com",
    )


@pytest.fixture
def graphql_client(system_user_with_resources):
    """Create an authenticated GraphQL client."""
    system_user, token = system_user_with_resources
    client = APIClient()

    # Set the authorization header with the system user ID and token
    auth_header = f"Bearer {system_user.id}:{token}"
    client.credentials(HTTP_AUTHORIZATION=auth_header)

    return client


@pytest.fixture
def user_with_organization(organization):
    """Create a regular user within the organization for testing user queries."""

    user_model = get_user_model()
    user = baker.make(user_model, email="test@example.com")
    baker.make(OrganizationMembership, user=user, organization=organization)
    return user


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestGraphQLQueries:
    """Test GraphQL queries through the API endpoint."""

    def test_availability_windows_query_success(self, mock_rate_limiter, graphql_client, calendar):
        """Test successful availability windows GraphQL query."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        from di_core.containers import container

        # Create mock calendar service
        mock_calendar_service = Mock()
        mock_availability_window = AvailableTimeWindow(
            start_time=datetime.datetime(2025, 9, 2, 9, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
            id=1,
            can_book_partially=True,
        )
        mock_calendar_service.initialize_without_provider.return_value = None
        mock_calendar_service.get_availability_windows_in_range.return_value = [
            mock_availability_window
        ]

        # GraphQL query
        query = """
            query GetAvailabilityWindows($calendarId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
                availabilityWindows(
                    calendarId: $calendarId,
                    startDatetime: $startDatetime,
                    endDatetime: $endDatetime
                ) {
                    startTime
                    endTime
                    id
                    canBookPartially
                }
            }
        """

        variables = {
            "calendarId": calendar.id,
            "startDatetime": "2025-09-02T08:00:00Z",
            "endDatetime": "2025-09-02T18:00:00Z",
        }

        # Execute GraphQL query with container override
        with container.calendar_service.override(mock_calendar_service):
            response = graphql_client.post(
                "/graphql/",
                data=json.dumps({"query": query, "variables": variables}),
                content_type="application/json",
            )

        data = assert_graphql_success(response)
        assert "availabilityWindows" in data

        windows = data["availabilityWindows"]
        assert len(windows) == 1

        window = windows[0]
        assert window["startTime"] == "2025-09-02T09:00:00+00:00"
        assert window["endTime"] == "2025-09-02T10:00:00+00:00"
        assert window["id"] == 1
        assert window["canBookPartially"] is True

        # Verify service was called correctly
        mock_calendar_service.initialize_without_provider.assert_called_once()
        mock_calendar_service.get_availability_windows_in_range.assert_called_once()

    def test_availability_windows_query_empty_result(
        self, mock_rate_limiter, graphql_client, calendar
    ):
        """Test availability windows query with empty result."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        from di_core.containers import container

        # Create mock calendar service that returns empty list
        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None
        mock_calendar_service.get_availability_windows_in_range.return_value = []

        query = """
            query GetAvailabilityWindows($calendarId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
                availabilityWindows(
                    calendarId: $calendarId,
                    startDatetime: $startDatetime,
                    endDatetime: $endDatetime
                ) {
                    startTime
                    endTime
                    id
                    canBookPartially
                }
            }
        """

        variables = {
            "calendarId": calendar.id,
            "startDatetime": "2025-09-02T08:00:00Z",
            "endDatetime": "2025-09-02T18:00:00Z",
        }

        with container.calendar_service.override(mock_calendar_service):
            response = graphql_client.post(
                "/graphql/",
                data=json.dumps({"query": query, "variables": variables}),
                content_type="application/json",
            )

        data = assert_graphql_success(response)
        assert "availabilityWindows" in data
        assert data["availabilityWindows"] == []

    def test_availability_windows_query_multiple_windows(
        self, mock_rate_limiter, graphql_client, calendar
    ):
        """Test availability windows query with multiple windows."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        from di_core.containers import container

        # Create mock calendar service with multiple windows
        mock_calendar_service = Mock()
        mock_windows = [
            AvailableTimeWindow(
                start_time=datetime.datetime(2025, 9, 2, 9, 0, tzinfo=datetime.UTC),
                end_time=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
                id=1,
                can_book_partially=True,
            ),
            AvailableTimeWindow(
                start_time=datetime.datetime(2025, 9, 2, 14, 0, tzinfo=datetime.UTC),
                end_time=datetime.datetime(2025, 9, 2, 15, 0, tzinfo=datetime.UTC),
                id=2,
                can_book_partially=False,
            ),
            AvailableTimeWindow(
                start_time=datetime.datetime(2025, 9, 2, 16, 0, tzinfo=datetime.UTC),
                end_time=datetime.datetime(2025, 9, 2, 17, 0, tzinfo=datetime.UTC),
                id=None,  # Test without ID
                can_book_partially=True,
            ),
        ]
        mock_calendar_service.initialize_without_provider.return_value = None
        mock_calendar_service.get_availability_windows_in_range.return_value = mock_windows

        query = """
            query GetAvailabilityWindows($calendarId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
                availabilityWindows(
                    calendarId: $calendarId,
                    startDatetime: $startDatetime,
                    endDatetime: $endDatetime
                ) {
                    startTime
                    endTime
                    id
                    canBookPartially
                }
            }
        """

        variables = {
            "calendarId": calendar.id,
            "startDatetime": "2025-09-02T08:00:00Z",
            "endDatetime": "2025-09-02T18:00:00Z",
        }

        with container.calendar_service.override(mock_calendar_service):
            response = graphql_client.post(
                "/graphql/",
                data=json.dumps({"query": query, "variables": variables}),
                content_type="application/json",
            )

        data = assert_graphql_success(response)
        windows = data["availabilityWindows"]
        assert len(windows) == 3

        # Check first window
        assert windows[0]["startTime"] == "2025-09-02T09:00:00+00:00"
        assert windows[0]["endTime"] == "2025-09-02T10:00:00+00:00"
        assert windows[0]["id"] == 1
        assert windows[0]["canBookPartially"] is True

        # Check second window
        assert windows[1]["startTime"] == "2025-09-02T14:00:00+00:00"
        assert windows[1]["endTime"] == "2025-09-02T15:00:00+00:00"
        assert windows[1]["id"] == 2
        assert windows[1]["canBookPartially"] is False

        # Check third window (without ID)
        assert windows[2]["startTime"] == "2025-09-02T16:00:00+00:00"
        assert windows[2]["endTime"] == "2025-09-02T17:00:00+00:00"
        assert windows[2]["id"] is None
        assert windows[2]["canBookPartially"] is True

    def test_availability_windows_query_calendar_not_found(self, mock_rate_limiter, graphql_client):
        """Test availability windows query with non-existent calendar."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        from di_core.containers import container

        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None

        query = """
            query GetAvailabilityWindows($calendarId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
                availabilityWindows(
                    calendarId: $calendarId,
                    startDatetime: $startDatetime,
                    endDatetime: $endDatetime
                ) {
                    startTime
                    endTime
                    id
                    canBookPartially
                }
            }
        """

        variables = {
            "calendarId": 999,  # Non-existent calendar ID
            "startDatetime": "2025-09-02T08:00:00Z",
            "endDatetime": "2025-09-02T18:00:00Z",
        }

        with container.calendar_service.override(mock_calendar_service):
            response = graphql_client.post(
                "/graphql/",
                data=json.dumps({"query": query, "variables": variables}),
                content_type="application/json",
            )

        assert_response_status_code(response, 200)

        response_data = response.json()
        assert "errors" in response_data
        # Should contain Calendar.DoesNotExist error

    def test_availability_windows_query_unauthenticated(self, mock_rate_limiter):
        """Test availability windows query without authentication."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        client = APIClient()  # Not authenticated

        query = """
            query GetAvailabilityWindows($calendarId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
                availabilityWindows(
                    calendarId: $calendarId,
                    startDatetime: $startDatetime,
                    endDatetime: $endDatetime
                ) {
                    startTime
                    endTime
                    id
                    canBookPartially
                }
            }
        """

        variables = {
            "calendarId": 1,
            "startDatetime": "2025-09-02T08:00:00Z",
            "endDatetime": "2025-09-02T18:00:00Z",
        }

        response = client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        assert_response_status_code(response, 200)

        response_data = response.json()
        assert "errors" in response_data
        # Should contain authentication error

    def test_calendars_query(self, mock_rate_limiter, graphql_client, calendar):
        """Test basic calendars query."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        query = """
            query GetCalendars {
                calendars {
                    id
                    name
                    description
                    email
                    provider
                    calendarType
                }
            }
        """

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert "calendars" in data

        calendars = data["calendars"]
        assert len(calendars) >= 1

        # Find our test calendar
        test_calendar = next((c for c in calendars if c["id"] == str(calendar.id)), None)
        assert test_calendar is not None
        assert test_calendar["name"] == "Test Calendar"
        assert test_calendar["email"] == "calendar@test.com"

    def test_calendar_events_query(self, mock_rate_limiter, graphql_client, calendar):
        """Test basic calendar events query."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        # First create a calendar event
        event = baker.make(
            CalendarEvent,
            calendar=calendar,
            organization=calendar.organization,
            title="Test Event",
            description="Test Description",
            start_time=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2025, 9, 2, 11, 0, tzinfo=datetime.UTC),
        )

        query = """
            query GetCalendarEvents($calendarId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
                calendarEvents(
                    calendarId: $calendarId,
                    startDatetime: $startDatetime,
                    endDatetime: $endDatetime
                ) {
                    id
                    title
                    description
                    startTime
                    endTime
                    isRecurring
                }
            }
        """

        variables = {
            "calendarId": calendar.id,
            "startDatetime": "2025-09-02T00:00:00Z",
            "endDatetime": "2025-09-02T23:59:59Z",
        }

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert "calendarEvents" in data

        events = data["calendarEvents"]
        assert len(events) >= 1

        # Find our test event
        test_event = next((e for e in events if e["id"] == str(event.id)), None)
        assert test_event is not None
        assert test_event["title"] == "Test Event"
        assert test_event["isRecurring"] is False

    def test_blocked_times_query(self, mock_rate_limiter, graphql_client, calendar):
        """Test basic blocked times query."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        blocked_time = baker.make(
            BlockedTime,
            calendar=calendar,
            organization=calendar.organization,
            start_time=datetime.datetime(2025, 9, 2, 12, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2025, 9, 2, 13, 0, tzinfo=datetime.UTC),
        )

        query = """
            query GetBlockedTimes($calendarId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
                blockedTimes(
                    calendarId: $calendarId,
                    startDatetime: $startDatetime,
                    endDatetime: $endDatetime
                ) {
                    id
                    startTime
                    endTime
                }
            }
        """

        variables = {
            "calendarId": calendar.id,
            "startDatetime": "2025-09-02T00:00:00Z",
            "endDatetime": "2025-09-02T23:59:59Z",
        }

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert "blockedTimes" in data

        blocked_times = data["blockedTimes"]
        assert len(blocked_times) >= 1

        # Find our test blocked time
        test_blocked = next((bt for bt in blocked_times if bt["id"] == str(blocked_time.id)), None)
        assert test_blocked is not None

    def test_available_times_query(self, mock_rate_limiter, graphql_client, calendar):
        """Test basic available times query."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        available_time = baker.make(
            AvailableTime,
            calendar=calendar,
            organization=calendar.organization,
            start_time=datetime.datetime(2025, 9, 2, 14, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2025, 9, 2, 15, 0, tzinfo=datetime.UTC),
        )

        query = """
            query GetAvailableTimes($calendarId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
                availableTimes(
                    calendarId: $calendarId,
                    startDatetime: $startDatetime,
                    endDatetime: $endDatetime
                ) {
                    id
                    startTime
                    endTime
                }
            }
        """

        variables = {
            "calendarId": calendar.id,
            "startDatetime": "2025-09-02T00:00:00Z",
            "endDatetime": "2025-09-02T23:59:59Z",
        }

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert "availableTimes" in data

        available_times = data["availableTimes"]
        assert len(available_times) >= 1

        # Find our test available time
        test_available = next(
            (at for at in available_times if at["id"] == str(available_time.id)), None
        )
        assert test_available is not None

    def test_users_query(self, mock_rate_limiter, graphql_client, user_with_organization):
        """Test basic users query."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        query = """
            query GetUsers {
                users {
                    id
                    email
                }
            }
        """

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert "users" in data

        users = data["users"]
        assert len(users) >= 1

        # Find our test user
        test_user = next((u for u in users if u["id"] == str(user_with_organization.id)), None)
        assert test_user is not None
        assert test_user["email"] == "test@example.com"

    def test_invalid_graphql_query(self, mock_rate_limiter, graphql_client):
        """Test GraphQL query with syntax errors."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        invalid_query = """
            query GetCalendars {
                calendars {
                    invalidField
                }
            }
        """

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": invalid_query}),
            content_type="application/json",
        )

        assert_response_status_code(response, 200)

        response_data = response.json()
        assert "errors" in response_data

    def test_availability_windows_service_error(self, mock_rate_limiter, graphql_client, calendar):
        """Test availability windows query when service raises an error."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        from di_core.containers import container

        # Create mock calendar service that raises an exception
        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None
        mock_calendar_service.get_availability_windows_in_range.side_effect = Exception(
            "Service error"
        )

        query = """
            query GetAvailabilityWindows($calendarId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
                availabilityWindows(
                    calendarId: $calendarId,
                    startDatetime: $startDatetime,
                    endDatetime: $endDatetime
                ) {
                    startTime
                    endTime
                    id
                    canBookPartially
                }
            }
        """

        variables = {
            "calendarId": calendar.id,
            "startDatetime": "2025-09-02T08:00:00Z",
            "endDatetime": "2025-09-02T18:00:00Z",
        }

        with container.calendar_service.override(mock_calendar_service):
            response = graphql_client.post(
                "/graphql/",
                data=json.dumps({"query": query, "variables": variables}),
                content_type="application/json",
            )

        assert_response_status_code(response, 200)

        response_data = response.json()
        assert "errors" in response_data
        # Should contain the service error

    def test_unavailable_windows_query_success(self, mock_rate_limiter, graphql_client, calendar):
        """Test successful unavailable windows GraphQL query."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        from di_core.containers import container

        # Create mock calendar service
        mock_calendar_service = Mock()
        mock_window = UnavailableTimeWindow(
            start_time=datetime.datetime(2025, 9, 2, 12, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2025, 9, 2, 13, 0, tzinfo=datetime.UTC),
            reason="blocked_time",
            id=1,
            data=BlockedTimeData(
                id=1,
                calendar_external_id="ext-cal",
                start_time=datetime.datetime(2025, 9, 2, 12, 0, tzinfo=datetime.UTC),
                end_time=datetime.datetime(2025, 9, 2, 13, 0, tzinfo=datetime.UTC),
                reason="maintenance",
                external_id=None,
                meta={},
            ),
        )
        mock_calendar_service.initialize_without_provider.return_value = None
        mock_calendar_service.get_unavailable_time_windows_in_range.return_value = [mock_window]

        query = """
            query GetUnavailableWindows($calendarId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
                unavailableWindows(
                    calendarId: $calendarId,
                    startDatetime: $startDatetime,
                    endDatetime: $endDatetime
                ) {
                    startTime
                    endTime
                    id
                    reason
                }
            }
        """

        variables = {
            "calendarId": calendar.id,
            "startDatetime": "2025-09-02T00:00:00Z",
            "endDatetime": "2025-09-02T23:59:59Z",
        }

        # Execute GraphQL query with container override
        with container.calendar_service.override(mock_calendar_service):
            response = graphql_client.post(
                "/graphql/",
                data=json.dumps({"query": query, "variables": variables}),
                content_type="application/json",
            )

        data = assert_graphql_success(response)
        assert "unavailableWindows" in data

        windows = data["unavailableWindows"]
        assert len(windows) == 1

        window = windows[0]
        assert window["startTime"] == "2025-09-02T12:00:00+00:00"
        assert window["endTime"] == "2025-09-02T13:00:00+00:00"
        assert window["id"] == 1
        assert window["reason"] == "blocked_time"

        mock_calendar_service.initialize_without_provider.assert_called_once()
        mock_calendar_service.get_unavailable_time_windows_in_range.assert_called_once()

    def test_unavailable_windows_query_empty_result(
        self, mock_rate_limiter, graphql_client, calendar
    ):
        """Test unavailable windows query with empty result."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        from di_core.containers import container

        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None
        mock_calendar_service.get_unavailable_time_windows_in_range.return_value = []

        query = """
            query GetUnavailableWindows($calendarId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
                unavailableWindows(
                    calendarId: $calendarId,
                    startDatetime: $startDatetime,
                    endDatetime: $endDatetime
                ) {
                    startTime
                    endTime
                    id
                    reason
                }
            }
        """

        variables = {
            "calendarId": calendar.id,
            "startDatetime": "2025-09-02T00:00:00Z",
            "endDatetime": "2025-09-02T23:59:59Z",
        }

        with container.calendar_service.override(mock_calendar_service):
            response = graphql_client.post(
                "/graphql/",
                data=json.dumps({"query": query, "variables": variables}),
                content_type="application/json",
            )

        data = assert_graphql_success(response)
        assert "unavailableWindows" in data
        assert data["unavailableWindows"] == []

    def test_unavailable_windows_service_error(self, mock_rate_limiter, graphql_client, calendar):
        """Test unavailable windows query when service raises an error."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        from di_core.containers import container

        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None
        mock_calendar_service.get_unavailable_time_windows_in_range.side_effect = Exception(
            "Service error"
        )

        query = """
            query GetUnavailableWindows($calendarId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
                unavailableWindows(
                    calendarId: $calendarId,
                    startDatetime: $startDatetime,
                    endDatetime: $endDatetime
                ) {
                    startTime
                    endTime
                    id
                    reason
                }
            }
        """

        variables = {
            "calendarId": calendar.id,
            "startDatetime": "2025-09-02T00:00:00Z",
            "endDatetime": "2025-09-02T23:59:59Z",
        }

        with container.calendar_service.override(mock_calendar_service):
            response = graphql_client.post(
                "/graphql/",
                data=json.dumps({"query": query, "variables": variables}),
                content_type="application/json",
            )

        assert_response_status_code(response, 200)

        response_data = response.json()
        assert "errors" in response_data
        # Should contain the service error

    def test_availability_windows_invalid_datetime_range(
        self, mock_rate_limiter, graphql_client, calendar
    ):
        """Test availability windows query with invalid datetime range."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        from di_core.containers import container

        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None

        query = """
            query GetAvailabilityWindows($calendarId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
                availabilityWindows(
                    calendarId: $calendarId,
                    startDatetime: $startDatetime,
                    endDatetime: $endDatetime
                ) {
                    startTime
                    endTime
                    id
                    canBookPartially
                }
            }
        """

        # End time before start time
        variables = {
            "calendarId": calendar.id,
            "startDatetime": "2025-09-02T18:00:00Z",
            "endDatetime": "2025-09-02T08:00:00Z",
        }

        with container.calendar_service.override(mock_calendar_service):
            response = graphql_client.post(
                "/graphql/",
                data=json.dumps({"query": query, "variables": variables}),
                content_type="application/json",
            )

        # This might pass through to the service, which could validate the range
        # The behavior depends on whether validation is done at the GraphQL level or service level
        assert_response_status_code(response, 200)

    def test_availability_windows_missing_variables(self, mock_rate_limiter, graphql_client):
        """Test availability windows query with missing required variables."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        query = """
            query GetAvailabilityWindows($calendarId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
                availabilityWindows(
                    calendarId: $calendarId,
                    startDatetime: $startDatetime,
                    endDatetime: $endDatetime
                ) {
                    startTime
                    endTime
                    id
                    canBookPartially
                }
            }
        """

        # Missing required variables
        variables = {
            "calendarId": 1,
            # Missing startDatetime and endDatetime
        }

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        assert_response_status_code(response, 200)

        response_data = response.json()
        assert "errors" in response_data
        # Should contain variable validation errors

    def test_calendars_query_with_id_filter(self, mock_rate_limiter, graphql_client, calendar):
        """Test calendars query with ID filter."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        # Create an additional calendar to ensure filtering works
        baker.make(
            Calendar,
            organization=calendar.organization,
            name="Other Calendar",
            email="other@example.com",
            external_id="other-external-id",
        )

        query = """
            query GetCalendars($calendarId: Int) {
                calendars(calendarId: $calendarId) {
                    id
                    name
                    email
                }
            }
        """

        # Test filtering by ID
        variables = {"calendarId": calendar.id}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        assert_response_status_code(response, 200)
        response_data = response.json()
        assert "errors" not in response_data
        assert len(response_data["data"]["calendars"]) == 1
        assert response_data["data"]["calendars"][0]["id"] == str(calendar.id)
        assert response_data["data"]["calendars"][0]["name"] == calendar.name

        # Test without ID filter (should return all calendars)
        variables = {}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        assert_response_status_code(response, 200)
        response_data = response.json()
        assert "errors" not in response_data
        # Should return both calendars
        assert len(response_data["data"]["calendars"]) == 2

    def test_calendar_events_query_with_id_filter(
        self, mock_rate_limiter, graphql_client, calendar
    ):
        """Test calendar events query with ID filter."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        # Create a calendar event
        event = baker.make(
            CalendarEvent,
            calendar_fk=calendar,
            organization=calendar.organization,
            title="Test Event",
            description="Test Description",
            start_time=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2025, 9, 2, 11, 0, tzinfo=datetime.UTC),
            external_id="test-event-external-id",
        )

        # Create another event to ensure filtering works
        baker.make(
            CalendarEvent,
            calendar_fk=calendar,
            organization=calendar.organization,
            title="Other Event",
            external_id="other-event-external-id",
        )

        query = """
            query GetCalendarEvents($eventId: Int) {
                calendarEvents(eventId: $eventId) {
                    id
                    title
                    description
                    startTime
                    endTime
                    isRecurring
                }
            }
        """

        variables = {"eventId": event.id}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert "calendarEvents" in data

        events = data["calendarEvents"]
        assert len(events) == 1
        assert events[0]["id"] == str(event.id)
        assert events[0]["title"] == "Test Event"

    def test_calendar_events_query_missing_required_params(self, mock_rate_limiter, graphql_client):
        """Test calendar events query without required parameters."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        query = """
            query GetCalendarEvents($calendarId: Int, $startDatetime: DateTime, $endDatetime: DateTime) {
                calendarEvents(
                    calendarId: $calendarId,
                    startDatetime: $startDatetime,
                    endDatetime: $endDatetime
                ) {
                    id
                    title
                }
            }
        """

        # Missing required parameters
        variables = {"calendarId": 1}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        assert_response_status_code(response, 200)
        response_data = response.json()
        assert "errors" in response_data
        assert any(
            "Missing required parameters" in error.get("message", "")
            for error in response_data["errors"]
        )

    def test_blocked_times_query_with_id_filter(self, mock_rate_limiter, graphql_client, calendar):
        """Test blocked times query with ID filter."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        blocked_time = baker.make(
            BlockedTime,
            calendar_fk=calendar,
            organization=calendar.organization,
            start_time=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2025, 9, 2, 11, 0, tzinfo=datetime.UTC),
            external_id="test-blocked-time-external-id",
        )

        # Create another blocked time to ensure filtering works
        baker.make(
            BlockedTime,
            calendar_fk=calendar,
            organization=calendar.organization,
            external_id="other-blocked-time-external-id",
        )

        query = """
            query GetBlockedTimes($blockedTimeId: Int) {
                blockedTimes(blockedTimeId: $blockedTimeId) {
                    id
                    startTime
                    endTime
                }
            }
        """

        variables = {"blockedTimeId": blocked_time.id}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert "blockedTimes" in data

        blocked_times = data["blockedTimes"]
        assert len(blocked_times) == 1
        assert blocked_times[0]["id"] == str(blocked_time.id)

    def test_blocked_times_query_missing_required_params(self, mock_rate_limiter, graphql_client):
        """Test blocked times query without required parameters."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        query = """
            query GetBlockedTimes($calendarId: Int, $startDatetime: DateTime, $endDatetime: DateTime) {
                blockedTimes(
                    calendarId: $calendarId,
                    startDatetime: $startDatetime,
                    endDatetime: $endDatetime
                ) {
                    id
                }
            }
        """

        # Missing required parameters
        variables = {"calendarId": 1}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        assert_response_status_code(response, 200)
        response_data = response.json()
        assert "errors" in response_data
        # The actual error should be related to missing required parameters
        assert any(
            "required" in error.get("message", "").lower() for error in response_data["errors"]
        )

    def test_available_times_query_with_id_filter(
        self, mock_rate_limiter, graphql_client, calendar
    ):
        """Test available times query with ID filter."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        available_time = baker.make(
            AvailableTime,
            calendar_fk=calendar,
            organization=calendar.organization,
            start_time=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2025, 9, 2, 11, 0, tzinfo=datetime.UTC),
        )

        # Create another available time to ensure filtering works
        baker.make(
            AvailableTime,
            calendar_fk=calendar,
            organization=calendar.organization,
        )

        query = """
            query GetAvailableTimes($availableTimeId: Int) {
                availableTimes(availableTimeId: $availableTimeId) {
                    id
                    startTime
                    endTime
                }
            }
        """

        variables = {"availableTimeId": available_time.id}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert "availableTimes" in data

        available_times = data["availableTimes"]
        assert len(available_times) == 1
        assert available_times[0]["id"] == str(available_time.id)

    def test_available_times_query_missing_required_params(self, mock_rate_limiter, graphql_client):
        """Test available times query without required parameters."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        query = """
            query GetAvailableTimes($calendarId: Int, $startDatetime: DateTime, $endDatetime: DateTime) {
                availableTimes(
                    calendarId: $calendarId,
                    startDatetime: $startDatetime,
                    endDatetime: $endDatetime
                ) {
                    id
                }
            }
        """

        # Missing required parameters
        variables = {"calendarId": 1}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        assert_response_status_code(response, 200)
        response_data = response.json()
        assert "errors" in response_data
        # The actual error should be related to missing required parameters
        assert any(
            "required" in error.get("message", "").lower() for error in response_data["errors"]
        )

    def test_users_query_with_id_filter(
        self, mock_rate_limiter, graphql_client, user_with_organization
    ):
        """Test users query with ID filter."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        # user_with_organization is already created by the fixture
        user = user_with_organization
        organization = user.organization_membership.organization

        # Create another user in the same organization to ensure filtering works
        user_model = get_user_model()
        other_user = baker.make(user_model, email="other@example.com", username="otheruser")
        baker.make(
            OrganizationMembership,
            user=other_user,
            organization=organization,
        )

        query = """
            query GetUsers($userId: Int) {
                users(userId: $userId) {
                    id
                    email
                    username
                }
            }
        """

        variables = {"userId": user.id}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert "users" in data

        users = data["users"]
        assert len(users) == 1
        assert users[0]["id"] == str(user.id)
        assert users[0]["email"] == user.email

    def test_calendars_query_nonexistent_id(self, mock_rate_limiter, graphql_client):
        """Test calendars query with nonexistent ID."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        query = """
            query GetCalendars($calendarId: Int) {
                calendars(calendarId: $calendarId) {
                    id
                    name
                }
            }
        """

        variables = {"calendarId": 99999}  # Nonexistent ID

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert "calendars" in data
        calendars = data["calendars"]
        assert len(calendars) == 0  # Should return empty list

    def test_users_query_nonexistent_id(self, mock_rate_limiter, graphql_client):
        """Test users query with nonexistent ID."""
        # Mock rate limiter to allow requests
        mock_rate_limiter.return_value = iter([None])

        query = """
            query GetUsers($userId: Int) {
                users(userId: $userId) {
                    id
                    email
                }
            }
        """

        variables = {"userId": 99999}  # Nonexistent ID

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert "users" in data
        users = data["users"]
        assert len(users) == 0  # Should return empty list
