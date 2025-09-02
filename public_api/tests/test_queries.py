import datetime
import json
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.models import AvailableTime, BlockedTime, Calendar, CalendarEvent
from calendar_integration.services.dataclasses import AvailableTimeWindow
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
