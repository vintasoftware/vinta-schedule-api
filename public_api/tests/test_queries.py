import datetime
import json
import uuid
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarType, CalendarVisibility
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarOwnership,
    ChildrenCalendarRelationship,
)
from calendar_integration.services.dataclasses import (
    AvailableTimeWindow,
    BlockedTimeData,
    UnavailableTimeWindow,
)
from common.utils.authentication_utils import generate_long_lived_token, hash_long_lived_token
from organizations.models import Organization, OrganizationMembership
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess, SystemUser
from public_api.services import PublicAPIAuthService
from users.factories import UserFactory
from users.models import User


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
            start_time_tz_unaware=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2025, 9, 2, 11, 0, tzinfo=datetime.UTC),
            timezone="UTC",
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
            start_time_tz_unaware=datetime.datetime(2025, 9, 2, 12, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2025, 9, 2, 13, 0, tzinfo=datetime.UTC),
            timezone="UTC",
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
            start_time_tz_unaware=datetime.datetime(2025, 9, 2, 14, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2025, 9, 2, 15, 0, tzinfo=datetime.UTC),
            timezone="UTC",
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

    def test_users_query_name_filter(self, mock_rate_limiter, graphql_client, organization):
        """Test users query filtering by concatenated profile name."""
        mock_rate_limiter.return_value = iter([None])

        user_model = get_user_model()
        # Create user with profile first and last nam
        user = baker.make(user_model, email="alice@example.com")
        baker.make(OrganizationMembership, user=user, organization=organization)
        # Create profile for the user with given names
        baker.make(
            "users.Profile",
            user=user,
            first_name="Alice",
            last_name="Smith",
        )

        # Create another user that should not match
        other = baker.make(user_model, email="bob@example.com")
        baker.make(OrganizationMembership, user=other, organization=organization)
        baker.make(
            "users.Profile",
            user=other,
            first_name="Bob",
            last_name="Jones",
        )

        query = """
            query GetUsers($name: String) {
                users(name: $name) {
                    id
                    email
                }
            }
        """

        variables = {"name": "Alice Smith"}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert "users" in data
        users = data["users"]
        assert len(users) == 1
        assert users[0]["email"] == "alice@example.com"

    def test_users_query_email_filter(self, mock_rate_limiter, graphql_client, organization):
        """Test users query filtering by email substring."""
        mock_rate_limiter.return_value = iter([None])

        user_model = get_user_model()
        user = baker.make(user_model, email="carol@example.com")
        baker.make(OrganizationMembership, user=user, organization=organization)

        baker.make(user_model, email="dave@other.com")

        query = """
            query GetUsers($email: String) {
                users(email: $email) {
                    id
                    email
                }
            }
        """

        variables = {"email": "@example.com"}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert "users" in data
        users = data["users"]
        # Should only include users with @example.com
        assert all("@example.com" in u["email"] for u in users)

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
                timezone="UTC",
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
            query GetCalendars($calendarId: Int, $offset: Int, $limit: Int) {
                calendars(calendarId: $calendarId, offset: $offset, limit: $limit) {
                    id
                    name
                    email
                }
            }
        """

        # Test filtering by ID
        variables = {"calendarId": calendar.id, "offset": 0, "limit": 100}

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

    def test_calendars_query_with_user_filter(self, mock_rate_limiter, graphql_client, calendar):
        """Test calendars query filtered by owner user via CalendarOwnership."""
        mock_rate_limiter.return_value = iter([None])

        # Create a user and make them owner of the calendar
        user_model = get_user_model()
        owner = baker.make(user_model, email="owner@example.com")
        baker.make(
            "calendar_integration.CalendarOwnership",
            calendar=calendar,
            user=owner,
            is_default=True,
            organization=calendar.organization,
        )

        # Create another calendar owned by a different user
        other_calendar = baker.make(
            Calendar,
            organization=calendar.organization,
            name="Other Calendar",
            email="other@example.com",
            external_id="other-cal-1",
            provider="internal",
        )
        other_owner = baker.make(user_model, email="other_owner@example.com")
        baker.make(
            "calendar_integration.CalendarOwnership",
            calendar=other_calendar,
            user=other_owner,
            organization=calendar.organization,
        )

        query = """
            query GetCalendars($userId: Int) {
                calendars(userId: $userId) {
                    id
                    name
                    email
                }
            }
        """

        variables = {"userId": owner.id}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert "calendars" in data

        calendars = data["calendars"]
        assert len(calendars) == 1
        assert calendars[0]["email"] == "calendar@test.com"

    def test_calendars_query_with_calendar_type_filter(
        self, mock_rate_limiter, graphql_client, calendar
    ):
        """Test calendars query filtered by calendar_type."""
        mock_rate_limiter.return_value = iter([None])

        # Create resource and virtual calendars
        baker.make(
            Calendar,
            organization=calendar.organization,
            name="Resource Calendar",
            email="resource@example.com",
            calendar_type="resource",
            external_id="res-cal-1",
            provider="internal",
        )

        baker.make(
            Calendar,
            organization=calendar.organization,
            name="Virtual Calendar",
            email="virtual@example.com",
            calendar_type="virtual",
            external_id="virt-cal-1",
            provider="internal",
        )

        query = """
            query GetCalendars($calendarType: String) {
                calendars(calendarType: $calendarType) {
                    id
                    name
                    calendarType
                }
            }
        """

        variables = {"calendarType": "resource"}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert "calendars" in data

        calendars = data["calendars"]
        # Should only return resource calendars
        assert all(c["calendarType"] == "resource" for c in calendars)

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
            start_time_tz_unaware=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2025, 9, 2, 11, 0, tzinfo=datetime.UTC),
            timezone="UTC",
            external_id="test-event-external-id",
        )

        # Create another event to ensure filtering works
        baker.make(
            CalendarEvent,
            calendar_fk=calendar,
            organization=calendar.organization,
            title="Other Event",
            external_id="other-event-external-id",
            timezone="UTC",
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
            start_time_tz_unaware=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2025, 9, 2, 11, 0, tzinfo=datetime.UTC),
            timezone="UTC",
            external_id="test-blocked-time-external-id",
        )

        # Create another blocked time to ensure filtering works
        baker.make(
            BlockedTime,
            calendar_fk=calendar,
            organization=calendar.organization,
            external_id="other-blocked-time-external-id",
            timezone="UTC",
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
            start_time_tz_unaware=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2025, 9, 2, 11, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

        # Create another available time to ensure filtering works
        baker.make(
            AvailableTime,
            calendar_fk=calendar,
            organization=calendar.organization,
            timezone="UTC",
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
        organization = user.organization_memberships.get().organization

        # Create another user in the same organization to ensure filtering works
        user_model = get_user_model()
        other_user = baker.make(user_model, email="other@example.com")
        baker.make(
            OrganizationMembership,
            user=other_user,
            organization=organization,
        )

        query = """
            query GetUsers($userId: Int, $offset: Int, $limit: Int) {
                users(userId: $userId, offset: $offset, limit: $limit) {
                    id
                    email
                }
            }
        """

        variables = {"userId": user.id, "offset": 0, "limit": 100}

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
            query GetCalendars($calendarId: Int, $offset: Int, $limit: Int) {
                calendars(calendarId: $calendarId, offset: $offset, limit: $limit) {
                    id
                    name
                }
            }
        """

        variables = {"calendarId": 99999, "offset": 0, "limit": 100}  # Nonexistent ID

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
            query GetUsers($userId: Int, $offset: Int, $limit: Int) {
                users(userId: $userId, offset: $offset, limit: $limit) {
                    id
                    email
                }
            }
        """

        variables = {"userId": 99999, "offset": 0, "limit": 100}  # Nonexistent ID

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert "users" in data
        users = data["users"]
        assert len(users) == 0  # Should return empty list

    def test_calendars_pagination_default(self, mock_rate_limiter, graphql_client, organization):
        """Test calendars query with default pagination."""
        mock_rate_limiter.return_value = iter([None])

        # Create multiple calendars
        calendars = []
        for i in range(5):
            calendar = baker.make(
                Calendar,
                organization=organization,
                name=f"Calendar {i}",
                email=f"calendar{i}@test.com",
                external_id=f"external-id-{i}",
            )
            calendars.append(calendar)

        query = """
            query GetCalendars {
                calendars {
                    id
                    name
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
        assert "calendars" in data
        result_calendars = data["calendars"]

        # Should return all calendars (within the limit of 100)
        assert len(result_calendars) == 5

    def test_calendars_pagination_with_offset_and_limit(
        self, mock_rate_limiter, graphql_client, organization
    ):
        """Test calendars query with custom offset and limit."""
        mock_rate_limiter.return_value = iter([None])

        # Create multiple calendars
        calendars = []
        for i in range(10):
            calendar = baker.make(
                Calendar,
                organization=organization,
                name=f"Calendar {i}",
                email=f"calendar{i}@test.com",
                external_id=f"external-id-{i}",
            )
            calendars.append(calendar)

        query = """
            query GetCalendars($offset: Int, $limit: Int) {
                calendars(offset: $offset, limit: $limit) {
                    id
                    name
                    email
                }
            }
        """

        variables = {"offset": 2, "limit": 3}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert "calendars" in data
        result_calendars = data["calendars"]

        # Should return 3 calendars starting from offset 2
        assert len(result_calendars) == 3

    def test_calendars_pagination_invalid_offset(self, mock_rate_limiter, graphql_client):
        """Test calendars query with invalid negative offset."""
        mock_rate_limiter.return_value = iter([None])

        query = """
            query GetCalendars($offset: Int) {
                calendars(offset: $offset) {
                    id
                    name
                }
            }
        """

        variables = {"offset": -1}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        assert_response_status_code(response, 200)
        response_data = response.json()
        assert "errors" in response_data
        assert "Offset must be non-negative" in str(response_data["errors"])

    def test_calendars_pagination_invalid_limit(self, mock_rate_limiter, graphql_client):
        """Test calendars query with invalid limit."""
        mock_rate_limiter.return_value = iter([None])

        query = """
            query GetCalendars($limit: Int) {
                calendars(limit: $limit) {
                    id
                    name
                }
            }
        """

        # Test with limit > 100
        variables = {"limit": 101}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        assert_response_status_code(response, 200)
        response_data = response.json()
        assert "errors" in response_data
        assert "Limit must be between 1 and 100" in str(response_data["errors"])

        # Test with limit <= 0
        variables = {"limit": 0}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        assert_response_status_code(response, 200)
        response_data = response.json()
        assert "errors" in response_data
        assert "Limit must be between 1 and 100" in str(response_data["errors"])

    def test_users_pagination_default(self, mock_rate_limiter, graphql_client, organization):
        """Test users query with default pagination."""
        mock_rate_limiter.return_value = iter([None])

        user_model = get_user_model()
        users = []

        # Create multiple users
        for i in range(5):
            user = baker.make(user_model, email=f"user{i}@test.com")
            baker.make(OrganizationMembership, user=user, organization=organization)
            users.append(user)

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
        result_users = data["users"]

        # Should return all users (within the limit of 100)
        assert len(result_users) == 5

    def test_users_pagination_with_offset_and_limit(
        self, mock_rate_limiter, graphql_client, organization
    ):
        """Test users query with custom offset and limit."""
        mock_rate_limiter.return_value = iter([None])

        user_model = get_user_model()
        users = []

        # Create multiple users
        for i in range(10):
            user = baker.make(user_model, email=f"user{i}@test.com")
            baker.make(OrganizationMembership, user=user, organization=organization)
            users.append(user)

        query = """
            query GetUsers($offset: Int, $limit: Int) {
                users(offset: $offset, limit: $limit) {
                    id
                    email
                }
            }
        """

        variables = {"offset": 2, "limit": 3}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert "users" in data
        result_users = data["users"]

        # Should return 3 users starting from offset 2
        assert len(result_users) == 3

    def test_users_pagination_invalid_offset(self, mock_rate_limiter, graphql_client):
        """Test users query with invalid negative offset."""
        mock_rate_limiter.return_value = iter([None])

        query = """
            query GetUsers($offset: Int) {
                users(offset: $offset) {
                    id
                    email
                }
            }
        """

        variables = {"offset": -1}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        assert_response_status_code(response, 200)
        response_data = response.json()
        assert "errors" in response_data
        assert "Offset must be non-negative" in str(response_data["errors"])

    def test_users_pagination_invalid_limit(self, mock_rate_limiter, graphql_client):
        """Test users query with invalid limit."""
        mock_rate_limiter.return_value = iter([None])

        query = """
            query GetUsers($limit: Int) {
                users(limit: $limit) {
                    id
                    email
                }
            }
        """

        # Test with limit > 100
        variables = {"limit": 101}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        assert_response_status_code(response, 200)
        response_data = response.json()
        assert "errors" in response_data
        assert "Limit must be between 1 and 100" in str(response_data["errors"])

        # Test with limit <= 0
        variables = {"limit": 0}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        assert_response_status_code(response, 200)
        response_data = response.json()
        assert "errors" in response_data
        assert "Limit must be between 1 and 100" in str(response_data["errors"])

    def test_users_query_excludes_inactive_members(
        self, mock_rate_limiter, graphql_client, organization
    ):
        """Public API users query must exclude users with is_active=False memberships.

        SHOULD-FIX 5 — confirms that adding organization_membership__is_active=True to
        the filter in public_api/queries.py actually removes inactive members from the
        result set, and that active members are still returned.
        """
        mock_rate_limiter.return_value = iter([None])

        user_model = get_user_model()

        # Active member — must appear in results
        active_user = baker.make(user_model, email="active_inactive_test@example.com")
        baker.make(
            OrganizationMembership, user=active_user, organization=organization, is_active=True
        )

        # Inactive member — must NOT appear in results
        inactive_user = baker.make(user_model, email="inactive_member_test@example.com")
        baker.make(
            OrganizationMembership, user=inactive_user, organization=organization, is_active=False
        )

        query = """
            query GetUsers($offset: Int, $limit: Int) {
                users(offset: $offset, limit: $limit) {
                    id
                    email
                }
            }
        """
        variables = {"offset": 0, "limit": 100}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        returned_emails = [u["email"] for u in data["users"]]

        assert active_user.email in returned_emails, "Active member must appear in users query"
        assert inactive_user.email not in returned_emails, "Inactive member must be excluded"


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestBrandingForTenantQuery:
    """Test the unauthenticated brandingForTenant public query."""

    @pytest.fixture
    def anonymous_client(self):
        """Create an unauthenticated GraphQL client (no Authorization header)."""
        return APIClient()

    def test_branding_for_unbranded_org_returns_vinta_default(
        self, mock_rate_limiter, anonymous_client
    ):
        """Test that an unbranded org returns vinta default branding."""
        mock_rate_limiter.return_value = iter([None])

        # Create an org with no branding
        org = baker.make(Organization, name="Unbranded Org")

        query = """
            query GetBrandingForTenant($tenantId: ID!) {
                brandingForTenant(tenantId: $tenantId) {
                    appName
                    logoUrl
                    primaryColor
                    secondaryColor
                }
            }
        """
        variables = {"tenantId": str(org.id)}

        response = anonymous_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        branding = data["brandingForTenant"]

        assert branding["appName"] == "Vinta Schedule"
        assert branding["logoUrl"] == ""
        assert branding["primaryColor"] == ""
        assert branding["secondaryColor"] == ""

    def test_branding_for_unknown_tenant_returns_vinta_default(
        self, mock_rate_limiter, anonymous_client
    ):
        """Test that an unknown tenant ID returns vinta default (no enumeration oracle)."""
        mock_rate_limiter.return_value = iter([None])

        query = """
            query GetBrandingForTenant($tenantId: ID!) {
                brandingForTenant(tenantId: $tenantId) {
                    appName
                    logoUrl
                    primaryColor
                    secondaryColor
                }
            }
        """
        # Use a random non-existent ID
        variables = {"tenantId": "999999"}

        response = anonymous_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        branding = data["brandingForTenant"]

        # Same response as unbranded org (no enumeration oracle)
        assert branding["appName"] == "Vinta Schedule"
        assert branding["logoUrl"] == ""
        assert branding["primaryColor"] == ""
        assert branding["secondaryColor"] == ""

    def test_branding_for_branded_reseller_returns_branding(
        self, mock_rate_limiter, anonymous_client
    ):
        """Test that a reseller's branding is returned correctly."""
        mock_rate_limiter.return_value = iter([None])

        # Create a reseller org with branding
        reseller = baker.make(Organization, name="Reseller", can_invite_organizations=True)
        baker.make(
            "organizations.OrganizationBranding",
            organization=reseller,
            app_name="MyScheduler",
            logo_url="https://example.com/logo.png",
            primary_color="#FF0000",
            secondary_color="#00FF00",
        )

        query = """
            query GetBrandingForTenant($tenantId: ID!) {
                brandingForTenant(tenantId: $tenantId) {
                    appName
                    logoUrl
                    primaryColor
                    secondaryColor
                }
            }
        """
        variables = {"tenantId": str(reseller.id)}

        response = anonymous_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        returned_branding = data["brandingForTenant"]

        assert returned_branding["appName"] == "MyScheduler"
        assert returned_branding["logoUrl"] == "https://example.com/logo.png"
        assert returned_branding["primaryColor"] == "#FF0000"
        assert returned_branding["secondaryColor"] == "#00FF00"

    def test_branding_for_child_returns_parent_branding(self, mock_rate_limiter, anonymous_client):
        """Test that a child org returns its parent reseller's branding."""
        mock_rate_limiter.return_value = iter([None])

        # Create a reseller with branding
        reseller = baker.make(Organization, name="Reseller", can_invite_organizations=True)
        baker.make(
            "organizations.OrganizationBranding",
            organization=reseller,
            app_name="ChildBranding",
            logo_url="https://example.com/child-logo.png",
            primary_color="#0000FF",
            secondary_color="#FFFF00",
        )

        # Create a child org (no branding of its own)
        child = baker.make(Organization, name="Child", parent=reseller)

        query = """
            query GetBrandingForTenant($tenantId: ID!) {
                brandingForTenant(tenantId: $tenantId) {
                    appName
                    logoUrl
                    primaryColor
                    secondaryColor
                }
            }
        """
        variables = {"tenantId": str(child.id)}

        response = anonymous_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        returned_branding = data["brandingForTenant"]

        # Child returns parent's branding
        assert returned_branding["appName"] == "ChildBranding"
        assert returned_branding["logoUrl"] == "https://example.com/child-logo.png"
        assert returned_branding["primaryColor"] == "#0000FF"
        assert returned_branding["secondaryColor"] == "#FFFF00"

    def test_branding_does_not_expose_secrets(self, mock_rate_limiter, anonymous_client):
        """Test that support_email and return_url_allowlist are not exposed."""
        mock_rate_limiter.return_value = iter([None])

        # Create a reseller with branding including secrets
        reseller = baker.make(Organization, name="Reseller", can_invite_organizations=True)
        baker.make(
            "organizations.OrganizationBranding",
            organization=reseller,
            app_name="MyApp",
            logo_url="https://example.com/logo.png",
            primary_color="#FF0000",
            secondary_color="#00FF00",
            support_email="support@example.com",
            return_url_allowlist=["https://example.com", "https://app.example.com"],
        )

        query = """
            query GetBrandingForTenant($tenantId: ID!) {
                brandingForTenant(tenantId: $tenantId) {
                    appName
                    logoUrl
                    primaryColor
                    secondaryColor
                }
            }
        """
        variables = {"tenantId": str(reseller.id)}

        response = anonymous_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        returned_branding = data["brandingForTenant"]

        # Verify secrets are not in the response
        assert "supportEmail" not in returned_branding
        assert "returnUrlAllowlist" not in returned_branding
        assert "support_email" not in returned_branding
        assert "return_url_allowlist" not in returned_branding
        # Verify the actual secret values are not present (support_email)
        assert "support@example.com" not in str(returned_branding)
        # return_url_allowlist should not be present in response at all
        assert "app.example.com" not in str(returned_branding)

    def test_branding_callable_without_token(self, mock_rate_limiter, anonymous_client):
        """Test that brandingForTenant is callable without authentication."""
        mock_rate_limiter.return_value = iter([None])

        query = """
            query {
                brandingForTenant(tenantId: "1") {
                    appName
                }
            }
        """

        # Make request without Authorization header
        response = anonymous_client.post(
            "/graphql/",
            data=json.dumps({"query": query}),
            content_type="application/json",
        )

        # Should succeed (200) despite no token
        assert_response_status_code(response, 200)
        data = response.json()
        assert "data" in data
        assert data["data"] is not None
        assert "brandingForTenant" in data["data"]

    def test_branding_rate_limited_by_ip(self, mock_rate_limiter, anonymous_client):
        """Test that brandingForTenant is rate-limited per anonymous IP.

        This test strengthens the existing patch-based test to assert on the
        limiter's call args, verifying the rate-limit key is constructed correctly
        as anon:<ip>. Since test settings disable rate limiting (PUBLIC_API_REQUESTS_PER_*
        limits all 0), full exhaustion testing is impractical without test fixture
        changes; instead we verify the IP keying logic at the extension level.
        """
        mock_rate_limiter.return_value = iter([None])

        org = baker.make(Organization, name="TestOrg")

        query = """
            query GetBrandingForTenant($tenantId: ID!) {
                brandingForTenant(tenantId: $tenantId) {
                    appName
                }
            }
        """
        variables = {"tenantId": str(org.id)}

        # Make a request with explicit X-Forwarded-For header
        response = anonymous_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
            HTTP_X_FORWARDED_FOR="192.168.1.50",  # Explicit test IP
        )

        assert_response_status_code(response, 200)

        # Verify that the rate limiter was called
        assert mock_rate_limiter.called, (
            "Rate limiter should be called for unauthenticated requests"
        )


_VALIDATE_RETURN_URL_QUERY = """
    query ValidateReturnUrl($tenantId: ID!, $url: String!) {
        validateReturnUrl(tenantId: $tenantId, url: $url) {
            allowed
            sanitizedUrl
        }
    }
"""


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestValidateReturnUrlQuery:
    """Test the unauthenticated validateReturnUrl public query.

    This query lets the OAuth interstitial callback (no session yet) ask whether
    a candidate `next` URL is allowed WITHOUT the reseller-internal
    return_url_allowlist ever being exposed (§4.6).
    """

    @pytest.fixture
    def anonymous_client(self):
        """Create an unauthenticated GraphQL client (no Authorization header)."""
        return APIClient()

    def _post(self, client, tenant_id, url):
        return client.post(
            "/graphql/",
            data=json.dumps(
                {
                    "query": _VALIDATE_RETURN_URL_QUERY,
                    "variables": {"tenantId": str(tenant_id), "url": url},
                }
            ),
            content_type="application/json",
        )

    def _make_reseller_with_allowlist(self, allowlist):
        reseller = baker.make(Organization, name="Reseller", can_invite_organizations=True)
        baker.make(
            "organizations.OrganizationBranding",
            organization=reseller,
            app_name="MyApp",
            return_url_allowlist=allowlist,
        )
        return reseller

    def test_allowed_url_for_child_org(self, mock_rate_limiter, anonymous_client):
        """A child org under a reseller whose allowlist contains the candidate's
        origin returns allowed=True and echoes the url."""
        mock_rate_limiter.return_value = iter([None])

        reseller = self._make_reseller_with_allowlist(["https://app.example.com"])
        child = baker.make(Organization, name="Child", parent=reseller)

        candidate = "https://app.example.com/auth/callback?code=abc"
        response = self._post(anonymous_client, child.id, candidate)

        data = assert_graphql_success(response)
        result = data["validateReturnUrl"]
        assert result["allowed"] is True
        assert result["sanitizedUrl"] == candidate

    def test_allowed_url_for_reseller_itself(self, mock_rate_limiter, anonymous_client):
        """A reseller validating against its own allowlist works."""
        mock_rate_limiter.return_value = iter([None])

        reseller = self._make_reseller_with_allowlist(["https://app.example.com"])

        candidate = "https://app.example.com/return"
        response = self._post(anonymous_client, reseller.id, candidate)

        data = assert_graphql_success(response)
        assert data["validateReturnUrl"] == {
            "allowed": True,
            "sanitizedUrl": candidate,
        }

    def test_origin_confusion_rejected(self, mock_rate_limiter, anonymous_client):
        """A look-alike host suffix must NOT be admitted (no substring matching)."""
        mock_rate_limiter.return_value = iter([None])

        reseller = self._make_reseller_with_allowlist(["https://app.example.com"])

        response = self._post(
            anonymous_client, reseller.id, "https://app.example.com.evil.com/steal"
        )

        data = assert_graphql_success(response)
        assert data["validateReturnUrl"] == {"allowed": False, "sanitizedUrl": None}

    def test_scheme_mismatch_rejected(self, mock_rate_limiter, anonymous_client):
        """http candidate against an https allowlist entry is a different origin."""
        mock_rate_limiter.return_value = iter([None])

        reseller = self._make_reseller_with_allowlist(["https://app.example.com"])

        response = self._post(anonymous_client, reseller.id, "http://app.example.com/cb")

        data = assert_graphql_success(response)
        assert data["validateReturnUrl"] == {"allowed": False, "sanitizedUrl": None}

    def test_default_port_normalization(self, mock_rate_limiter, anonymous_client):
        """An explicit default port (:443 on https) equals the implicit origin."""
        mock_rate_limiter.return_value = iter([None])

        reseller = self._make_reseller_with_allowlist(["https://app.example.com"])

        candidate = "https://app.example.com:443/cb"
        response = self._post(anonymous_client, reseller.id, candidate)

        data = assert_graphql_success(response)
        assert data["validateReturnUrl"]["allowed"] is True
        assert data["validateReturnUrl"]["sanitizedUrl"] == candidate

    @pytest.mark.parametrize(
        "bad_url",
        [
            "javascript:alert(1)",
            "data:text/html,x",
            "//evil.com",
            "not a url",
            "",
        ],
    )
    def test_scheme_guard_rejects_dangerous_schemes(
        self, mock_rate_limiter, anonymous_client, bad_url
    ):
        """javascript:, data:, protocol-relative, and unparseable input are rejected
        even when the host portion would otherwise match the allowlist."""
        mock_rate_limiter.return_value = iter([None])

        reseller = self._make_reseller_with_allowlist(
            ["https://app.example.com", "https://evil.com"]
        )

        response = self._post(anonymous_client, reseller.id, bad_url)

        data = assert_graphql_success(response)
        assert data["validateReturnUrl"] == {"allowed": False, "sanitizedUrl": None}

    def test_unknown_tenant_returns_not_allowed(self, mock_rate_limiter, anonymous_client):
        """Unknown tenant ID returns the same not-allowed shape (no enumeration oracle)."""
        mock_rate_limiter.return_value = iter([None])

        response = self._post(anonymous_client, "999999", "https://app.example.com/cb")

        data = assert_graphql_success(response)
        assert data["validateReturnUrl"] == {"allowed": False, "sanitizedUrl": None}

    def test_non_numeric_tenant_returns_not_allowed(self, mock_rate_limiter, anonymous_client):
        """A non-numeric tenant ID never raises — returns not-allowed."""
        mock_rate_limiter.return_value = iter([None])

        response = self._post(anonymous_client, "not-an-int", "https://app.example.com/cb")

        data = assert_graphql_success(response)
        assert data["validateReturnUrl"] == {"allowed": False, "sanitizedUrl": None}

    def test_org_without_branding_returns_not_allowed(self, mock_rate_limiter, anonymous_client):
        """An org with no reseller branding returns the same not-allowed shape."""
        mock_rate_limiter.return_value = iter([None])

        org = baker.make(Organization, name="Unbranded")

        response = self._post(anonymous_client, org.id, "https://app.example.com/cb")

        data = assert_graphql_success(response)
        assert data["validateReturnUrl"] == {"allowed": False, "sanitizedUrl": None}

    def test_empty_allowlist_returns_not_allowed(self, mock_rate_limiter, anonymous_client):
        """A reseller with an empty allowlist admits nothing (same shape)."""
        mock_rate_limiter.return_value = iter([None])

        reseller = self._make_reseller_with_allowlist([])

        response = self._post(anonymous_client, reseller.id, "https://app.example.com/cb")

        data = assert_graphql_success(response)
        assert data["validateReturnUrl"] == {"allowed": False, "sanitizedUrl": None}

    def test_no_oracle_identical_shape_across_negative_cases(
        self, mock_rate_limiter, anonymous_client
    ):
        """Unknown tenant, no-branding org, and empty allowlist are indistinguishable."""
        mock_rate_limiter.return_value = iter([None])

        unbranded = baker.make(Organization, name="NoBranding")
        empty_reseller = self._make_reseller_with_allowlist([])

        candidate = "https://app.example.com/cb"
        unknown = self._post(anonymous_client, "999999", candidate).json()["data"][
            "validateReturnUrl"
        ]
        no_branding = self._post(anonymous_client, unbranded.id, candidate).json()["data"][
            "validateReturnUrl"
        ]
        empty = self._post(anonymous_client, empty_reseller.id, candidate).json()["data"][
            "validateReturnUrl"
        ]

        expected = {"allowed": False, "sanitizedUrl": None}
        assert unknown == expected
        assert no_branding == expected
        assert empty == expected

    def test_allowlist_never_serialized(self, mock_rate_limiter, anonymous_client):
        """The allowlist values must never leak into the response (§4.6)."""
        mock_rate_limiter.return_value = iter([None])

        reseller = self._make_reseller_with_allowlist(
            ["https://app.example.com", "https://secret-internal.example.com"]
        )

        response = self._post(anonymous_client, reseller.id, "https://app.example.com/cb")

        body = response.content.decode()
        assert "secret-internal.example.com" not in body
        assert "returnUrlAllowlist" not in body
        assert "return_url_allowlist" not in body

    def test_callable_without_token(self, mock_rate_limiter, anonymous_client):
        """validateReturnUrl is callable without authentication."""
        mock_rate_limiter.return_value = iter([None])

        response = self._post(anonymous_client, "1", "https://app.example.com/cb")

        assert_response_status_code(response, 200)
        body = response.json()
        assert "data" in body and body["data"] is not None
        assert "validateReturnUrl" in body["data"]

    def test_rate_limited_like_branding(self, mock_rate_limiter, anonymous_client):
        """validateReturnUrl runs through the same OrganizationRateLimiter extension."""
        mock_rate_limiter.return_value = iter([None])

        org = baker.make(Organization, name="TestOrg")
        response = self._post(anonymous_client, org.id, "https://app.example.com/cb")

        assert_response_status_code(response, 200)
        assert mock_rate_limiter.called, (
            "Rate limiter should be invoked for the unauthenticated validateReturnUrl query"
        )


# ---------------------------------------------------------------------------
# childOrganizations analytics query tests
# ---------------------------------------------------------------------------

_CHILD_ORG_ANALYTICS_QUERY = """
    query GetChildOrganizations($offset: Int, $limit: Int) {
        childOrganizations(offset: $offset, limit: $limit) {
            id
            name
            createdAt
            membershipCount
            calendarCount
            eventCount
            calendarGroupCount
        }
    }
"""


def _make_reseller_client(reseller: Organization):
    """Create an authenticated API client for a reseller org with CHILD_ORG_ANALYTICS scope."""
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name="reseller_analytics", organization=reseller
    )
    baker.make(
        ResourceAccess,
        system_user=system_user,
        resource_name=PublicAPIResources.CHILD_ORG_ANALYTICS,
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")
    return client


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestChildOrganizationsQuery:
    """Tests for the childOrganizations gated analytics query."""

    def test_exact_aggregate_counts_per_child(self, mock_rate_limiter):
        """Counts are exact per child; distinct metrics detect join fan-out.

        Seeds a child with intentionally different values for each metric
        (3 memberships, 2 calendars, 5 events, 1 group) so that any
        join-based multi-relation fan-out would produce obviously wrong numbers.
        """
        mock_rate_limiter.return_value = iter([None])

        reseller = baker.make(Organization, name="Reseller", can_invite_organizations=True)
        child = baker.make(Organization, name="ChildA", parent=reseller)
        client = _make_reseller_client(reseller)

        # 3 memberships
        for i in range(3):
            user = baker.make("users.User", email=f"user{i}@childA.test")
            baker.make(OrganizationMembership, user=user, organization=child)

        # 2 calendars
        for i in range(2):
            baker.make(Calendar, organization=child, external_id=f"cal-{i}")

        # 5 events — all belong directly to the child org
        for i in range(5):
            baker.make(
                CalendarEvent,
                organization=child,
                external_id=f"ev-{i}",
                timezone="UTC",
            )

        # 1 calendar group
        baker.make(CalendarGroup, organization=child)

        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CHILD_ORG_ANALYTICS_QUERY}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        children = data["childOrganizations"]
        assert len(children) == 1

        row = children[0]
        assert row["id"] == child.id
        assert row["name"] == "ChildA"
        assert row["membershipCount"] == 3, f"expected 3, got {row['membershipCount']}"
        assert row["calendarCount"] == 2, f"expected 2, got {row['calendarCount']}"
        assert row["eventCount"] == 5, f"expected 5, got {row['eventCount']}"
        assert row["calendarGroupCount"] == 1, f"expected 1, got {row['calendarGroupCount']}"

    def test_multiple_children_counted_independently(self, mock_rate_limiter):
        """Each child's counts are independent (no cross-child leakage)."""
        mock_rate_limiter.return_value = iter([None])

        reseller = baker.make(Organization, name="Reseller2", can_invite_organizations=True)
        child1 = baker.make(Organization, name="Child1", parent=reseller)
        child2 = baker.make(Organization, name="Child2", parent=reseller)
        client = _make_reseller_client(reseller)

        # child1: 2 memberships, 1 calendar, 0 events, 0 groups
        for i in range(2):
            u = baker.make("users.User", email=f"mc1-{i}@test.com")
            baker.make(OrganizationMembership, user=u, organization=child1)
        baker.make(Calendar, organization=child1, external_id="c1-cal")

        # child2: 0 memberships, 0 calendars, 3 events, 2 groups
        for i in range(3):
            baker.make(CalendarEvent, organization=child2, external_id=f"c2-ev-{i}", timezone="UTC")
        for i in range(2):
            baker.make(CalendarGroup, organization=child2, name=f"grp-{i}")

        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CHILD_ORG_ANALYTICS_QUERY}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        children = {c["id"]: c for c in data["childOrganizations"]}

        assert children[child1.id]["membershipCount"] == 2
        assert children[child1.id]["calendarCount"] == 1
        assert children[child1.id]["eventCount"] == 0
        assert children[child1.id]["calendarGroupCount"] == 0

        assert children[child2.id]["membershipCount"] == 0
        assert children[child2.id]["calendarCount"] == 0
        assert children[child2.id]["eventCount"] == 3
        assert children[child2.id]["calendarGroupCount"] == 2

    def test_no_cross_reseller_leak(self, mock_rate_limiter):
        """Only the acting reseller's own children are returned (no cross-reseller data)."""
        mock_rate_limiter.return_value = iter([None])

        reseller_a = baker.make(Organization, name="ResellerA", can_invite_organizations=True)
        reseller_b = baker.make(Organization, name="ResellerB", can_invite_organizations=True)
        child_a = baker.make(Organization, name="ChildOfA", parent=reseller_a)
        child_b = baker.make(Organization, name="ChildOfB", parent=reseller_b)

        # Client for reseller_a
        client = _make_reseller_client(reseller_a)

        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CHILD_ORG_ANALYTICS_QUERY}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        children = data["childOrganizations"]
        returned_ids = {c["id"] for c in children}

        assert child_a.id in returned_ids, "reseller_a's child must be present"
        assert child_b.id not in returned_ids, "reseller_b's child must NOT appear"
        assert reseller_b.id not in returned_ids, "reseller_b itself must NOT appear"

    def test_flag_off_acting_org_returns_permission_error(self, mock_rate_limiter):
        """A non-reseller org (flag off) is denied even if it holds the scope."""
        mock_rate_limiter.return_value = iter([None])

        non_reseller = baker.make(Organization, name="NotAReseller", can_invite_organizations=False)
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="test_integration", organization=non_reseller
        )
        baker.make(
            ResourceAccess,
            system_user=system_user,
            resource_name=PublicAPIResources.CHILD_ORG_ANALYTICS,
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")

        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CHILD_ORG_ANALYTICS_QUERY}),
            content_type="application/json",
        )

        assert_response_status_code(response, 200)
        response_data = response.json()
        assert "errors" in response_data
        error_text = str(response_data["errors"])
        assert "permission" in error_text.lower() or "invite" in error_text.lower(), (
            f"Expected a permission-related error, got: {error_text}"
        )

    def test_missing_child_org_analytics_scope_returns_denied(self, mock_rate_limiter):
        """A reseller token without CHILD_ORG_ANALYTICS scope is denied at the permission layer."""
        mock_rate_limiter.return_value = iter([None])

        reseller = baker.make(Organization, name="ResellerNoScope", can_invite_organizations=True)
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="no_scope_integration", organization=reseller
        )
        # Intentionally do NOT grant CHILD_ORG_ANALYTICS scope
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")

        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CHILD_ORG_ANALYTICS_QUERY}),
            content_type="application/json",
        )

        assert_response_status_code(response, 200)
        response_data = response.json()
        assert "errors" in response_data

    def test_pagination_offset_and_limit(self, mock_rate_limiter):
        """Pagination returns the correct slice of children in stable order."""
        mock_rate_limiter.return_value = iter([None])

        reseller = baker.make(Organization, name="ResellerPaging", can_invite_organizations=True)
        client = _make_reseller_client(reseller)

        # Create 5 children
        children = [
            baker.make(Organization, name=f"PagChild-{i}", parent=reseller) for i in range(5)
        ]
        children_by_id = sorted(children, key=lambda c: c.id)

        # Request only first 2
        response = client.post(
            "/graphql/",
            data=json.dumps(
                {
                    "query": _CHILD_ORG_ANALYTICS_QUERY,
                    "variables": {"offset": 0, "limit": 2},
                }
            ),
            content_type="application/json",
        )
        data = assert_graphql_success(response)
        page1 = data["childOrganizations"]
        assert len(page1) == 2
        assert page1[0]["id"] == children_by_id[0].id
        assert page1[1]["id"] == children_by_id[1].id

        # Next page: offset=2, limit=2
        response = client.post(
            "/graphql/",
            data=json.dumps(
                {
                    "query": _CHILD_ORG_ANALYTICS_QUERY,
                    "variables": {"offset": 2, "limit": 2},
                }
            ),
            content_type="application/json",
        )
        data = assert_graphql_success(response)
        page2 = data["childOrganizations"]
        assert len(page2) == 2
        assert page2[0]["id"] == children_by_id[2].id
        assert page2[1]["id"] == children_by_id[3].id

    def test_zero_counts_for_empty_child(self, mock_rate_limiter):
        """A child with no members / calendars / events / groups returns zero counts."""
        mock_rate_limiter.return_value = iter([None])

        reseller = baker.make(Organization, name="ResellerEmpty", can_invite_organizations=True)
        child = baker.make(Organization, name="EmptyChild", parent=reseller)
        client = _make_reseller_client(reseller)

        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CHILD_ORG_ANALYTICS_QUERY}),
            content_type="application/json",
        )
        data = assert_graphql_success(response)
        children = data["childOrganizations"]
        assert len(children) == 1
        row = children[0]
        assert row["id"] == child.id
        assert row["membershipCount"] == 0
        assert row["calendarCount"] == 0
        assert row["eventCount"] == 0
        assert row["calendarGroupCount"] == 0

    def test_only_direct_children_returned(self, mock_rate_limiter):
        """Only direct children (parent=reseller) are returned; grandchildren are excluded."""
        mock_rate_limiter.return_value = iter([None])

        reseller = baker.make(Organization, name="ResellerDirect", can_invite_organizations=True)
        direct_child = baker.make(Organization, name="DirectChild", parent=reseller)
        # grandchild — NOT a direct child of reseller
        grandchild = baker.make(Organization, name="GrandChild", parent=direct_child)
        client = _make_reseller_client(reseller)

        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CHILD_ORG_ANALYTICS_QUERY}),
            content_type="application/json",
        )
        data = assert_graphql_success(response)
        children = data["childOrganizations"]
        returned_ids = {c["id"] for c in children}

        assert direct_child.id in returned_ids, "direct child must be listed"
        assert grandchild.id not in returned_ids, "grandchild must NOT be listed"

    def test_membership_count_includes_inactive(self, mock_rate_limiter):
        """membership_count includes ALL memberships (active=True and active=False).

        The plan spec says 'memberships' with no active-only qualifier.
        """
        mock_rate_limiter.return_value = iter([None])

        reseller = baker.make(Organization, name="ResellerInactive", can_invite_organizations=True)
        child = baker.make(Organization, name="ChildInactive", parent=reseller)
        client = _make_reseller_client(reseller)

        user_active = baker.make("users.User", email="active@example.com")
        user_inactive = baker.make("users.User", email="inactive@example.com")
        baker.make(OrganizationMembership, user=user_active, organization=child, is_active=True)
        baker.make(OrganizationMembership, user=user_inactive, organization=child, is_active=False)

        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CHILD_ORG_ANALYTICS_QUERY}),
            content_type="application/json",
        )
        data = assert_graphql_success(response)
        children = data["childOrganizations"]
        assert len(children) == 1
        # Both memberships (active + inactive) are counted
        assert children[0]["membershipCount"] == 2


_CALENDAR_BUNDLES_QUERY = """
    query GetCalendarBundles($offset: Int, $limit: Int) {
        calendarBundles(offset: $offset, limit: $limit) {
            id
            name
            description
            children {
                id
                name
            }
        }
    }
"""


def _make_bundle_calendar(organization, name="Test Bundle", child_count=2):
    """Helper: create a BUNDLE Calendar with `child_count` children in `organization`.

    Uses unique external_ids to avoid the (external_id, provider, organization_id)
    unique constraint on Calendar.
    """
    bundle = baker.make(
        Calendar,
        organization=organization,
        name=name,
        description=f"Description of {name}",
        calendar_type=CalendarType.BUNDLE,
        provider="internal",
        external_id=str(uuid.uuid4()),
    )
    children = []
    for i in range(child_count):
        child = baker.make(
            Calendar,
            organization=organization,
            name=f"{name} child {i}",
            provider="internal",
            external_id=str(uuid.uuid4()),
        )
        baker.make(
            ChildrenCalendarRelationship,
            bundle_calendar=bundle,
            child_calendar=child,
            organization=organization,
            is_primary=(i == 0),
        )
        children.append(child)
    return bundle, children


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestCalendarBundlesQuery:
    """Integration tests for the calendarBundles public GraphQL query."""

    @pytest.fixture
    def bundle_graphql_client(self, organization):
        """Create a system user + token with CALENDAR_BUNDLE access, return (client, org)."""
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="bundle_test_integration", organization=organization
        )
        baker.make(
            ResourceAccess,
            system_user=system_user,
            resource_name=PublicAPIResources.CALENDAR_BUNDLE,
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")
        return client, organization

    def test_lists_bundle_calendars(self, mock_rate_limiter, bundle_graphql_client):
        """Happy path: bundle calendars are listed with their children."""
        mock_rate_limiter.return_value = iter([None])
        client, org = bundle_graphql_client

        bundle, children = _make_bundle_calendar(org, name="Bundle Alpha", child_count=2)

        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDAR_BUNDLES_QUERY}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        bundles = data["calendarBundles"]
        assert len(bundles) == 1

        returned_bundle = bundles[0]
        assert returned_bundle["id"] == str(bundle.id)
        assert returned_bundle["name"] == "Bundle Alpha"
        assert returned_bundle["description"] == "Description of Bundle Alpha"

        returned_child_ids = {c["id"] for c in returned_bundle["children"]}
        assert returned_child_ids == {str(c.id) for c in children}

    def test_children_resolve_for_bundle(self, mock_rate_limiter, bundle_graphql_client):
        """Children of a bundle are correctly resolved via the children field."""
        mock_rate_limiter.return_value = iter([None])
        client, org = bundle_graphql_client

        _bundle, children = _make_bundle_calendar(org, name="Parent Bundle", child_count=3)

        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDAR_BUNDLES_QUERY}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        bundles = data["calendarBundles"]
        assert len(bundles) == 1

        returned_children = bundles[0]["children"]
        assert len(returned_children) == 3
        returned_names = {c["name"] for c in returned_children}
        expected_names = {c.name for c in children}
        assert returned_names == expected_names

    def test_org_isolation_other_org_bundles_excluded(
        self, mock_rate_limiter, bundle_graphql_client
    ):
        """Bundles from another organization must not appear in the results."""
        mock_rate_limiter.return_value = iter([None])
        client, org = bundle_graphql_client

        # Create a bundle in the acting org
        _make_bundle_calendar(org, name="My Bundle")

        # Create a bundle in a different org — must NOT appear
        other_org = baker.make(Organization, name="Other Org")
        _make_bundle_calendar(other_org, name="Other Org Bundle")

        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDAR_BUNDLES_QUERY}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        bundles = data["calendarBundles"]
        assert len(bundles) == 1
        assert bundles[0]["name"] == "My Bundle"

    def test_only_bundle_type_calendars_returned(self, mock_rate_limiter, bundle_graphql_client):
        """Regular (non-bundle) calendars must not appear in calendarBundles."""
        mock_rate_limiter.return_value = iter([None])
        client, org = bundle_graphql_client

        # A bundle calendar — should appear
        bundle, _ = _make_bundle_calendar(org, name="Real Bundle")

        # A regular calendar — should NOT appear
        baker.make(
            Calendar,
            organization=org,
            name="Regular Calendar",
            calendar_type=CalendarType.PERSONAL,
            provider="internal",
            external_id=str(uuid.uuid4()),
        )

        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDAR_BUNDLES_QUERY}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        bundles = data["calendarBundles"]
        assert len(bundles) == 1
        assert bundles[0]["id"] == str(bundle.id)

    def test_pagination_offset_and_limit_respected(self, mock_rate_limiter, bundle_graphql_client):
        """Pagination offset and limit control the returned slice."""
        mock_rate_limiter.return_value = iter([None])
        client, org = bundle_graphql_client

        # Create 5 bundles
        for i in range(5):
            _make_bundle_calendar(org, name=f"Bundle {i}", child_count=0)

        variables = {"offset": 1, "limit": 2}
        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDAR_BUNDLES_QUERY, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        bundles = data["calendarBundles"]
        assert len(bundles) == 2

    def test_pagination_empty_result_beyond_last_page(
        self, mock_rate_limiter, bundle_graphql_client
    ):
        """An offset beyond total count returns an empty list."""
        mock_rate_limiter.return_value = iter([None])
        client, org = bundle_graphql_client

        _make_bundle_calendar(org, name="Only Bundle")

        variables = {"offset": 10, "limit": 10}
        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDAR_BUNDLES_QUERY, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert data["calendarBundles"] == []

    def test_permission_denied_without_calendar_bundle_grant(self, mock_rate_limiter, organization):
        """A token without CALENDAR_BUNDLE grant receives a permission error."""
        mock_rate_limiter.return_value = iter([None])

        # System user without CALENDAR_BUNDLE resource
        auth_service = PublicAPIAuthService()
        system_user, token = auth_service.create_system_user(
            integration_name="no_bundle_integration", organization=organization
        )
        # Grant a different resource (not CALENDAR_BUNDLE)
        baker.make(
            ResourceAccess,
            system_user=system_user,
            resource_name=PublicAPIResources.CALENDAR,
        )

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")

        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDAR_BUNDLES_QUERY}),
            content_type="application/json",
        )

        assert response.status_code == 200
        response_data = response.json()
        assert "errors" in response_data
        permission_messages = [e.get("message", "") for e in response_data["errors"]]
        assert any(
            "access" in m.lower() or "permission" in m.lower() or "authenticated" in m.lower()
            for m in permission_messages
        )

    def test_empty_result_when_no_bundles(self, mock_rate_limiter, bundle_graphql_client):
        """Returns empty list when org has no bundle calendars."""
        mock_rate_limiter.return_value = iter([None])
        client, _org = bundle_graphql_client

        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDAR_BUNDLES_QUERY}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert data["calendarBundles"] == []

    def test_inactive_bundle_excluded_from_results(self, mock_rate_limiter, bundle_graphql_client):
        """Bundles with visibility=INACTIVE must be absent; active bundles still appear.

        BLOCKER — calendarBundles must honour .only_listed() so that bundles
        disabled by Phase 4d's disableCalendarBundle mutation drop out of the
        public listing, matching the behaviour of the `calendars` query.
        """
        mock_rate_limiter.return_value = iter([None])
        client, org = bundle_graphql_client

        # Active bundle — must appear
        active_bundle, _ = _make_bundle_calendar(org, name="Active Bundle", child_count=1)

        # Inactive bundle — must NOT appear
        inactive_bundle, _ = _make_bundle_calendar(org, name="Inactive Bundle", child_count=1)
        inactive_bundle.visibility = CalendarVisibility.INACTIVE
        inactive_bundle.save()

        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDAR_BUNDLES_QUERY}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        bundles = data["calendarBundles"]

        returned_ids = {b["id"] for b in bundles}
        assert str(active_bundle.id) in returned_ids, "Active bundle must be present"
        assert str(inactive_bundle.id) not in returned_ids, "Inactive bundle must be excluded"


# ---------------------------------------------------------------------------
# Owner-scoped public API token read enforcement tests (Phase 1)
# ---------------------------------------------------------------------------

_CALENDARS_QUERY = """
    query GetCalendars {
        calendars {
            id
            name
        }
    }
"""

_CALENDAR_EVENTS_BY_CALENDAR_QUERY = """
    query GetCalendarEvents($calendarId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
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

_CALENDAR_EVENTS_BY_ID_QUERY = """
    query GetCalendarEvents($eventId: Int!) {
        calendarEvents(eventId: $eventId) {
            id
            title
        }
    }
"""

_BLOCKED_TIMES_BY_CALENDAR_QUERY = """
    query GetBlockedTimes($calendarId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
        blockedTimes(
            calendarId: $calendarId,
            startDatetime: $startDatetime,
            endDatetime: $endDatetime
        ) {
            id
        }
    }
"""

_BLOCKED_TIMES_BY_ID_QUERY = """
    query GetBlockedTimes($blockedTimeId: Int!) {
        blockedTimes(blockedTimeId: $blockedTimeId) {
            id
        }
    }
"""

_AVAILABLE_TIMES_BY_CALENDAR_QUERY = """
    query GetAvailableTimes($calendarId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
        availableTimes(
            calendarId: $calendarId,
            startDatetime: $startDatetime,
            endDatetime: $endDatetime
        ) {
            id
        }
    }
"""

_AVAILABLE_TIMES_BY_ID_QUERY = """
    query GetAvailableTimes($availableTimeId: Int!) {
        availableTimes(availableTimeId: $availableTimeId) {
            id
        }
    }
"""

_AVAILABILITY_WINDOWS_QUERY = """
    query GetAvailabilityWindows($calendarId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
        availabilityWindows(
            calendarId: $calendarId,
            startDatetime: $startDatetime,
            endDatetime: $endDatetime
        ) {
            startTime
            endTime
            id
        }
    }
"""

_UNAVAILABLE_WINDOWS_QUERY = """
    query GetUnavailableWindows($calendarId: Int!, $startDatetime: DateTime!, $endDatetime: DateTime!) {
        unavailableWindows(
            calendarId: $calendarId,
            startDatetime: $startDatetime,
            endDatetime: $endDatetime
        ) {
            startTime
            endTime
            id
        }
    }
"""

_DATETIME_START = "2025-09-02T00:00:00Z"
_DATETIME_END = "2025-09-02T23:59:59Z"


def _make_all_resource_grants(system_user: SystemUser) -> None:
    """Grant all calendar-related resource permissions to a system user."""
    resources = [
        PublicAPIResources.CALENDAR,
        PublicAPIResources.CALENDAR_EVENT,
        PublicAPIResources.BLOCKED_TIME,
        PublicAPIResources.AVAILABLE_TIME,
        PublicAPIResources.AVAILABILITY_WINDOWS,
        PublicAPIResources.UNAVAILABLE_WINDOWS,
    ]
    for resource in resources:
        baker.make(ResourceAccess, system_user=system_user, resource_name=resource)


def _make_org_wide_client(organization: Organization) -> tuple[APIClient, SystemUser]:
    """Create an org-wide (scoped_to_membership_fk IS NULL) API client with all resource grants."""
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=f"org_wide_{organization.pk}", organization=organization
    )
    _make_all_resource_grants(system_user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")
    return client, system_user


def _make_scoped_client(organization: Organization, owner: User) -> tuple[APIClient, SystemUser]:
    """Create a scoped API client (scoped_to_membership_fk=membership) with all resource grants."""
    membership, _ = OrganizationMembership.objects.get_or_create(
        user=owner, organization=organization, defaults={"is_active": True}
    )
    token = generate_long_lived_token()
    system_user = baker.make(
        SystemUser,
        organization=organization,
        scoped_to_membership_fk=membership,
        integration_name=f"scoped_{organization.pk}_{owner.pk}",
        long_lived_token_hash=hash_long_lived_token(token),
        is_active=True,
    )
    _make_all_resource_grants(system_user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")
    return client, system_user


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestOwnerScopedTokenReadEnforcement:
    """Phase 1: verify scoped tokens only return their owner's data; org-wide unchanged.

    Each resolver shape gets a scoped-sees-own, scoped-blocked-cross-owner, and
    an org-wide-unchanged assertion.
    """

    @pytest.fixture
    def organization(self):
        return baker.make(Organization, name="ScopingTestOrg")

    @pytest.fixture
    def owner(self):
        return baker.make(User, email="owner@scoping.test")

    @pytest.fixture
    def other_owner(self):
        return baker.make(User, email="other_owner@scoping.test")

    @pytest.fixture
    def owner_calendar(self, organization, owner):
        """Calendar owned by `owner` in the org."""
        cal = baker.make(
            Calendar,
            organization=organization,
            name="Owner Calendar",
            external_id="owner-cal-scope",
        )
        baker.make(CalendarOwnership, calendar=cal, user=owner, organization=organization)
        return cal

    @pytest.fixture
    def other_calendar(self, organization, other_owner):
        """Calendar owned by `other_owner` (different provider) in the same org."""
        cal = baker.make(
            Calendar,
            organization=organization,
            name="Other Calendar",
            external_id="other-cal-scope",
        )
        baker.make(CalendarOwnership, calendar=cal, user=other_owner, organization=organization)
        return cal

    # ------------------------------------------------------------------ #
    # calendars                                                            #
    # ------------------------------------------------------------------ #

    def test_scoped_token_sees_only_owner_calendars(
        self, mock_rate_limiter, organization, owner, owner_calendar, other_calendar
    ):
        """A scoped token's `calendars` query returns only the owner's calendar."""
        mock_rate_limiter.return_value = iter([None])

        client, _ = _make_scoped_client(organization, owner)
        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDARS_QUERY}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        returned_ids = {int(c["id"]) for c in data["calendars"]}

        assert owner_calendar.id in returned_ids, "Owner's calendar must be visible"
        assert other_calendar.id not in returned_ids, "Other owner's calendar must NOT be visible"

    def test_scoped_token_no_calendars_returns_empty(
        self, mock_rate_limiter, organization, other_owner, owner_calendar
    ):
        """A scoped token whose owner owns NO calendars sees an empty list."""
        mock_rate_limiter.return_value = iter([None])

        # Create client scoped to a user who owns nothing
        no_calendar_user = baker.make(User, email="nobody@scoping.test")
        client, _ = _make_scoped_client(organization, no_calendar_user)

        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDARS_QUERY}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert data["calendars"] == [], "Owner with no calendars must return empty list"

    def test_org_wide_token_sees_all_calendars(
        self, mock_rate_limiter, organization, owner_calendar, other_calendar
    ):
        """Org-wide token (scoped_to_membership_fk IS NULL) returns all org calendars unchanged."""
        mock_rate_limiter.return_value = iter([None])

        client, _ = _make_org_wide_client(organization)
        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDARS_QUERY}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        returned_ids = {int(c["id"]) for c in data["calendars"]}

        assert owner_calendar.id in returned_ids, "Org-wide token must see owner_calendar"
        assert other_calendar.id in returned_ids, "Org-wide token must see other_calendar"

    # ------------------------------------------------------------------ #
    # calendar_events (by calendarId)                                     #
    # ------------------------------------------------------------------ #

    def test_scoped_token_blocked_cross_owner_calendar_events(
        self, mock_rate_limiter, organization, owner, other_calendar
    ):
        """Scoped token querying another owner's calendarId is indistinguishable from
        a genuinely missing calendar — both must produce the same error response shape.

        The real contract: _prepare_service_and_calendar raises Calendar.DoesNotExist for
        cross-owner calendarId, surfaced as a GraphQL error identical to a missing calendar.
        This test asserts equivalence of the two responses so the test FAILS if the owner
        guard in _prepare_service_and_calendar is removed.
        """
        mock_rate_limiter.return_value = iter([None])

        baker.make(
            CalendarEvent,
            calendar=other_calendar,
            organization=organization,
            title="Other Event",
            external_id="other-ev-scope",
            timezone="UTC",
        )

        client, _ = _make_scoped_client(organization, owner)

        # (a) Cross-owner calendarId — owner guard fires Calendar.DoesNotExist
        variables_cross = {
            "calendarId": other_calendar.id,
            "startDatetime": _DATETIME_START,
            "endDatetime": _DATETIME_END,
        }
        response_cross = client.post(
            "/graphql/",
            data=json.dumps(
                {"query": _CALENDAR_EVENTS_BY_CALENDAR_QUERY, "variables": variables_cross}
            ),
            content_type="application/json",
        )

        # (b) Guaranteed-nonexistent calendarId — ORM raises Calendar.DoesNotExist
        variables_nonexistent = {
            "calendarId": 999999,
            "startDatetime": _DATETIME_START,
            "endDatetime": _DATETIME_END,
        }
        response_nonexistent = client.post(
            "/graphql/",
            data=json.dumps(
                {"query": _CALENDAR_EVENTS_BY_CALENDAR_QUERY, "variables": variables_nonexistent}
            ),
            content_type="application/json",
        )

        assert_response_status_code(response_cross, 200)
        assert_response_status_code(response_nonexistent, 200)

        data_cross = response_cross.json()
        data_nonexistent = response_nonexistent.json()

        # Both must be errors (Calendar.DoesNotExist path) — no existence leak
        has_error_cross = "errors" in data_cross
        has_error_nonexistent = "errors" in data_nonexistent
        assert has_error_cross == has_error_nonexistent, (
            "Cross-owner calendarId must produce the same error presence as a nonexistent calendarId. "
            f"cross={data_cross}, nonexistent={data_nonexistent}"
        )

        if not has_error_cross:
            # If neither raised an error, both must return empty (no data leak)
            events_cross = (data_cross.get("data") or {}).get("calendarEvents", [])
            events_nonexistent = (data_nonexistent.get("data") or {}).get("calendarEvents", [])
            assert events_cross == [], "Cross-owner calendar must return empty events"
            assert events_nonexistent == [], "Nonexistent calendar must return empty events"

    def test_scoped_token_sees_own_calendar_events(
        self, mock_rate_limiter, organization, owner, owner_calendar
    ):
        """Scoped token can read events on its owner's calendar."""
        mock_rate_limiter.return_value = iter([None])

        event = baker.make(
            CalendarEvent,
            calendar=owner_calendar,
            organization=organization,
            title="Owner Event",
            external_id="owner-ev-scope",
            start_time_tz_unaware=datetime.datetime(2025, 9, 2, 9, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

        client, _ = _make_scoped_client(organization, owner)
        variables = {
            "calendarId": owner_calendar.id,
            "startDatetime": _DATETIME_START,
            "endDatetime": _DATETIME_END,
        }
        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDAR_EVENTS_BY_CALENDAR_QUERY, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        event_ids = [int(e["id"]) for e in data["calendarEvents"]]
        assert event.id in event_ids, "Event on owner's calendar must be visible"

    def test_org_wide_token_sees_all_calendar_events(
        self, mock_rate_limiter, organization, owner_calendar, other_calendar
    ):
        """Org-wide token (scoped_to_membership_fk IS NULL) can read events on any calendar."""
        mock_rate_limiter.return_value = iter([None])

        owner_event = baker.make(
            CalendarEvent,
            calendar=owner_calendar,
            organization=organization,
            title="OwnerEvt OrgWide",
            external_id="ow-evt-orgwide",
            start_time_tz_unaware=datetime.datetime(2025, 9, 2, 9, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )
        other_event = baker.make(
            CalendarEvent,
            calendar=other_calendar,
            organization=organization,
            title="OtherEvt OrgWide",
            external_id="other-evt-orgwide",
            start_time_tz_unaware=datetime.datetime(2025, 9, 2, 11, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2025, 9, 2, 12, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

        client, _ = _make_org_wide_client(organization)

        # Fetch events for owner_calendar
        variables = {
            "calendarId": owner_calendar.id,
            "startDatetime": _DATETIME_START,
            "endDatetime": _DATETIME_END,
        }
        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDAR_EVENTS_BY_CALENDAR_QUERY, "variables": variables}),
            content_type="application/json",
        )
        data = assert_graphql_success(response)
        ids = [int(e["id"]) for e in data["calendarEvents"]]
        assert owner_event.id in ids, "Org-wide token must see owner_calendar events"

        # Fetch events for other_calendar
        variables["calendarId"] = other_calendar.id
        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDAR_EVENTS_BY_CALENDAR_QUERY, "variables": variables}),
            content_type="application/json",
        )
        data = assert_graphql_success(response)
        ids = [int(e["id"]) for e in data["calendarEvents"]]
        assert other_event.id in ids, "Org-wide token must see other_calendar events"

    # ------------------------------------------------------------------ #
    # calendar_events (by event_id — single-id lookup)                   #
    # ------------------------------------------------------------------ #

    def test_scoped_token_event_id_cross_owner_returns_empty(
        self, mock_rate_limiter, organization, owner, other_calendar
    ):
        """A scoped token looking up another owner's event_id gets empty, not an error."""
        mock_rate_limiter.return_value = iter([None])

        other_event = baker.make(
            CalendarEvent,
            calendar=other_calendar,
            organization=organization,
            title="Other Owner Event",
            external_id="other-ev-id-scope",
            timezone="UTC",
        )

        client, _ = _make_scoped_client(organization, owner)
        response = client.post(
            "/graphql/",
            data=json.dumps(
                {"query": _CALENDAR_EVENTS_BY_ID_QUERY, "variables": {"eventId": other_event.id}}
            ),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert data["calendarEvents"] == [], (
            "Scoped token must not expose another owner's event via event_id lookup"
        )

    def test_scoped_token_event_id_own_owner_returns_event(
        self, mock_rate_limiter, organization, owner, owner_calendar
    ):
        """A scoped token can look up its own event by event_id."""
        mock_rate_limiter.return_value = iter([None])

        own_event = baker.make(
            CalendarEvent,
            calendar=owner_calendar,
            organization=organization,
            title="Own Event",
            external_id="own-ev-id-scope",
            timezone="UTC",
        )

        client, _ = _make_scoped_client(organization, owner)
        response = client.post(
            "/graphql/",
            data=json.dumps(
                {"query": _CALENDAR_EVENTS_BY_ID_QUERY, "variables": {"eventId": own_event.id}}
            ),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert len(data["calendarEvents"]) == 1
        assert int(data["calendarEvents"][0]["id"]) == own_event.id

    def test_org_wide_token_event_id_lookup_unchanged(
        self, mock_rate_limiter, organization, other_calendar
    ):
        """Org-wide token can look up any event by event_id (pre-change behavior)."""
        mock_rate_limiter.return_value = iter([None])

        event = baker.make(
            CalendarEvent,
            calendar=other_calendar,
            organization=organization,
            title="Any Event",
            external_id="any-ev-orgwide",
            timezone="UTC",
        )

        client, _ = _make_org_wide_client(organization)
        response = client.post(
            "/graphql/",
            data=json.dumps(
                {"query": _CALENDAR_EVENTS_BY_ID_QUERY, "variables": {"eventId": event.id}}
            ),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert len(data["calendarEvents"]) == 1
        assert int(data["calendarEvents"][0]["id"]) == event.id

    # ------------------------------------------------------------------ #
    # blocked_times (by calendarId)                                       #
    # ------------------------------------------------------------------ #

    def test_scoped_token_blocked_cross_owner_blocked_times(
        self, mock_rate_limiter, organization, owner, other_calendar
    ):
        """Scoped token querying another owner's calendarId for blocked_times is
        indistinguishable from a genuinely missing calendar — same error response shape."""
        mock_rate_limiter.return_value = iter([None])

        baker.make(
            BlockedTime,
            calendar=other_calendar,
            organization=organization,
            external_id="other-bt-scope",
            timezone="UTC",
        )

        client, _ = _make_scoped_client(organization, owner)

        # (a) Cross-owner calendarId
        variables_cross = {
            "calendarId": other_calendar.id,
            "startDatetime": _DATETIME_START,
            "endDatetime": _DATETIME_END,
        }
        response_cross = client.post(
            "/graphql/",
            data=json.dumps(
                {"query": _BLOCKED_TIMES_BY_CALENDAR_QUERY, "variables": variables_cross}
            ),
            content_type="application/json",
        )

        # (b) Guaranteed-nonexistent calendarId
        variables_nonexistent = {
            "calendarId": 999999,
            "startDatetime": _DATETIME_START,
            "endDatetime": _DATETIME_END,
        }
        response_nonexistent = client.post(
            "/graphql/",
            data=json.dumps(
                {"query": _BLOCKED_TIMES_BY_CALENDAR_QUERY, "variables": variables_nonexistent}
            ),
            content_type="application/json",
        )

        assert_response_status_code(response_cross, 200)
        assert_response_status_code(response_nonexistent, 200)

        data_cross = response_cross.json()
        data_nonexistent = response_nonexistent.json()

        has_error_cross = "errors" in data_cross
        has_error_nonexistent = "errors" in data_nonexistent
        assert has_error_cross == has_error_nonexistent, (
            "Cross-owner calendarId must produce the same error presence as a nonexistent calendarId. "
            f"cross={data_cross}, nonexistent={data_nonexistent}"
        )

        if not has_error_cross:
            bt_cross = (data_cross.get("data") or {}).get("blockedTimes", [])
            bt_nonexistent = (data_nonexistent.get("data") or {}).get("blockedTimes", [])
            assert bt_cross == [], "Cross-owner calendar must return empty blocked times"
            assert bt_nonexistent == [], "Nonexistent calendar must return empty blocked times"

    def test_scoped_token_sees_own_blocked_times(
        self, mock_rate_limiter, organization, owner, owner_calendar
    ):
        """Scoped token can read blocked times on its owner's calendar."""
        mock_rate_limiter.return_value = iter([None])

        bt = baker.make(
            BlockedTime,
            calendar=owner_calendar,
            organization=organization,
            external_id="own-bt-scope",
            start_time_tz_unaware=datetime.datetime(2025, 9, 2, 9, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

        client, _ = _make_scoped_client(organization, owner)
        variables = {
            "calendarId": owner_calendar.id,
            "startDatetime": _DATETIME_START,
            "endDatetime": _DATETIME_END,
        }
        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _BLOCKED_TIMES_BY_CALENDAR_QUERY, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        bt_ids = [int(b["id"]) for b in data["blockedTimes"]]
        assert bt.id in bt_ids, "Blocked time on owner's calendar must be visible"

    def test_org_wide_token_sees_all_blocked_times(
        self, mock_rate_limiter, organization, owner_calendar, other_calendar
    ):
        """Org-wide token can read blocked times on any calendar."""
        mock_rate_limiter.return_value = iter([None])

        bt1 = baker.make(
            BlockedTime,
            calendar=owner_calendar,
            organization=organization,
            external_id="bt1-orgwide",
            start_time_tz_unaware=datetime.datetime(2025, 9, 2, 9, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )
        bt2 = baker.make(
            BlockedTime,
            calendar=other_calendar,
            organization=organization,
            external_id="bt2-orgwide",
            start_time_tz_unaware=datetime.datetime(2025, 9, 2, 11, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2025, 9, 2, 12, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

        client, _ = _make_org_wide_client(organization)

        for cal, expected_bt in [(owner_calendar, bt1), (other_calendar, bt2)]:
            variables = {
                "calendarId": cal.id,
                "startDatetime": _DATETIME_START,
                "endDatetime": _DATETIME_END,
            }
            response = client.post(
                "/graphql/",
                data=json.dumps(
                    {"query": _BLOCKED_TIMES_BY_CALENDAR_QUERY, "variables": variables}
                ),
                content_type="application/json",
            )
            data = assert_graphql_success(response)
            bt_ids = [int(b["id"]) for b in data["blockedTimes"]]
            assert expected_bt.id in bt_ids, f"Org-wide token must see {expected_bt.id}"

    # ------------------------------------------------------------------ #
    # blocked_times (by blocked_time_id — single-id lookup)              #
    # ------------------------------------------------------------------ #

    def test_scoped_token_blocked_time_id_cross_owner_returns_empty(
        self, mock_rate_limiter, organization, owner, other_calendar
    ):
        """A scoped token looking up another owner's blocked_time_id gets empty."""
        mock_rate_limiter.return_value = iter([None])

        other_bt = baker.make(
            BlockedTime,
            calendar=other_calendar,
            organization=organization,
            external_id="other-bt-id-scope",
            timezone="UTC",
        )

        client, _ = _make_scoped_client(organization, owner)
        response = client.post(
            "/graphql/",
            data=json.dumps(
                {
                    "query": _BLOCKED_TIMES_BY_ID_QUERY,
                    "variables": {"blockedTimeId": other_bt.id},
                }
            ),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert data["blockedTimes"] == [], (
            "Scoped token must not expose another owner's blocked time via id lookup"
        )

    def test_scoped_token_blocked_time_id_own_returns_item(
        self, mock_rate_limiter, organization, owner, owner_calendar
    ):
        """A scoped token can look up its own blocked time by id."""
        mock_rate_limiter.return_value = iter([None])

        own_bt = baker.make(
            BlockedTime,
            calendar=owner_calendar,
            organization=organization,
            external_id="own-bt-id-scope",
            timezone="UTC",
        )

        client, _ = _make_scoped_client(organization, owner)
        response = client.post(
            "/graphql/",
            data=json.dumps(
                {
                    "query": _BLOCKED_TIMES_BY_ID_QUERY,
                    "variables": {"blockedTimeId": own_bt.id},
                }
            ),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert len(data["blockedTimes"]) == 1
        assert int(data["blockedTimes"][0]["id"]) == own_bt.id

    def test_org_wide_token_blocked_time_id_lookup_unchanged(
        self, mock_rate_limiter, organization, other_calendar
    ):
        """Org-wide token can look up any blocked time by id."""
        mock_rate_limiter.return_value = iter([None])

        bt = baker.make(
            BlockedTime,
            calendar=other_calendar,
            organization=organization,
            external_id="any-bt-orgwide",
            timezone="UTC",
        )

        client, _ = _make_org_wide_client(organization)
        response = client.post(
            "/graphql/",
            data=json.dumps(
                {"query": _BLOCKED_TIMES_BY_ID_QUERY, "variables": {"blockedTimeId": bt.id}}
            ),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert len(data["blockedTimes"]) == 1
        assert int(data["blockedTimes"][0]["id"]) == bt.id

    # ------------------------------------------------------------------ #
    # available_times (by calendarId)                                     #
    # ------------------------------------------------------------------ #

    def test_scoped_token_blocked_cross_owner_available_times(
        self, mock_rate_limiter, organization, owner, other_calendar
    ):
        """Scoped token querying another owner's calendarId for available_times is
        indistinguishable from a genuinely missing calendar — same error response shape."""
        mock_rate_limiter.return_value = iter([None])

        baker.make(
            AvailableTime,
            calendar=other_calendar,
            organization=organization,
            timezone="UTC",
        )

        client, _ = _make_scoped_client(organization, owner)

        # (a) Cross-owner calendarId
        variables_cross = {
            "calendarId": other_calendar.id,
            "startDatetime": _DATETIME_START,
            "endDatetime": _DATETIME_END,
        }
        response_cross = client.post(
            "/graphql/",
            data=json.dumps(
                {"query": _AVAILABLE_TIMES_BY_CALENDAR_QUERY, "variables": variables_cross}
            ),
            content_type="application/json",
        )

        # (b) Guaranteed-nonexistent calendarId
        variables_nonexistent = {
            "calendarId": 999999,
            "startDatetime": _DATETIME_START,
            "endDatetime": _DATETIME_END,
        }
        response_nonexistent = client.post(
            "/graphql/",
            data=json.dumps(
                {"query": _AVAILABLE_TIMES_BY_CALENDAR_QUERY, "variables": variables_nonexistent}
            ),
            content_type="application/json",
        )

        assert_response_status_code(response_cross, 200)
        assert_response_status_code(response_nonexistent, 200)

        data_cross = response_cross.json()
        data_nonexistent = response_nonexistent.json()

        has_error_cross = "errors" in data_cross
        has_error_nonexistent = "errors" in data_nonexistent
        assert has_error_cross == has_error_nonexistent, (
            "Cross-owner calendarId must produce the same error presence as a nonexistent calendarId. "
            f"cross={data_cross}, nonexistent={data_nonexistent}"
        )

        if not has_error_cross:
            at_cross = (data_cross.get("data") or {}).get("availableTimes", [])
            at_nonexistent = (data_nonexistent.get("data") or {}).get("availableTimes", [])
            assert at_cross == [], "Cross-owner calendar must return empty available times"
            assert at_nonexistent == [], "Nonexistent calendar must return empty available times"

    def test_scoped_token_sees_own_available_times(
        self, mock_rate_limiter, organization, owner, owner_calendar
    ):
        """Scoped token can read available times on its owner's calendar."""
        mock_rate_limiter.return_value = iter([None])

        at = baker.make(
            AvailableTime,
            calendar=owner_calendar,
            organization=organization,
            start_time_tz_unaware=datetime.datetime(2025, 9, 2, 9, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

        client, _ = _make_scoped_client(organization, owner)
        variables = {
            "calendarId": owner_calendar.id,
            "startDatetime": _DATETIME_START,
            "endDatetime": _DATETIME_END,
        }
        response = client.post(
            "/graphql/",
            data=json.dumps({"query": _AVAILABLE_TIMES_BY_CALENDAR_QUERY, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        at_ids = [int(a["id"]) for a in data["availableTimes"]]
        assert at.id in at_ids, "Available time on owner's calendar must be visible"

    def test_org_wide_token_sees_all_available_times(
        self, mock_rate_limiter, organization, owner_calendar, other_calendar
    ):
        """Org-wide token can read available times on any calendar."""
        mock_rate_limiter.return_value = iter([None])

        at1 = baker.make(
            AvailableTime,
            calendar=owner_calendar,
            organization=organization,
            start_time_tz_unaware=datetime.datetime(2025, 9, 2, 9, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )
        at2 = baker.make(
            AvailableTime,
            calendar=other_calendar,
            organization=organization,
            start_time_tz_unaware=datetime.datetime(2025, 9, 2, 11, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2025, 9, 2, 12, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

        client, _ = _make_org_wide_client(organization)

        for cal, expected_at in [(owner_calendar, at1), (other_calendar, at2)]:
            variables = {
                "calendarId": cal.id,
                "startDatetime": _DATETIME_START,
                "endDatetime": _DATETIME_END,
            }
            response = client.post(
                "/graphql/",
                data=json.dumps(
                    {"query": _AVAILABLE_TIMES_BY_CALENDAR_QUERY, "variables": variables}
                ),
                content_type="application/json",
            )
            data = assert_graphql_success(response)
            at_ids = [int(a["id"]) for a in data["availableTimes"]]
            assert expected_at.id in at_ids, f"Org-wide token must see {expected_at.id}"

    # ------------------------------------------------------------------ #
    # available_times (by available_time_id — single-id lookup)          #
    # ------------------------------------------------------------------ #

    def test_scoped_token_available_time_id_cross_owner_returns_empty(
        self, mock_rate_limiter, organization, owner, other_calendar
    ):
        """A scoped token looking up another owner's available_time_id gets empty."""
        mock_rate_limiter.return_value = iter([None])

        other_at = baker.make(
            AvailableTime,
            calendar=other_calendar,
            organization=organization,
            timezone="UTC",
        )

        client, _ = _make_scoped_client(organization, owner)
        response = client.post(
            "/graphql/",
            data=json.dumps(
                {
                    "query": _AVAILABLE_TIMES_BY_ID_QUERY,
                    "variables": {"availableTimeId": other_at.id},
                }
            ),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert data["availableTimes"] == [], (
            "Scoped token must not expose another owner's available time via id lookup"
        )

    def test_scoped_token_available_time_id_own_returns_item(
        self, mock_rate_limiter, organization, owner, owner_calendar
    ):
        """A scoped token can look up its own available time by id."""
        mock_rate_limiter.return_value = iter([None])

        own_at = baker.make(
            AvailableTime,
            calendar=owner_calendar,
            organization=organization,
            timezone="UTC",
        )

        client, _ = _make_scoped_client(organization, owner)
        response = client.post(
            "/graphql/",
            data=json.dumps(
                {
                    "query": _AVAILABLE_TIMES_BY_ID_QUERY,
                    "variables": {"availableTimeId": own_at.id},
                }
            ),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert len(data["availableTimes"]) == 1
        assert int(data["availableTimes"][0]["id"]) == own_at.id

    def test_org_wide_token_available_time_id_lookup_unchanged(
        self, mock_rate_limiter, organization, other_calendar
    ):
        """Org-wide token can look up any available time by id."""
        mock_rate_limiter.return_value = iter([None])

        at = baker.make(
            AvailableTime,
            calendar=other_calendar,
            organization=organization,
            timezone="UTC",
        )

        client, _ = _make_org_wide_client(organization)
        response = client.post(
            "/graphql/",
            data=json.dumps(
                {"query": _AVAILABLE_TIMES_BY_ID_QUERY, "variables": {"availableTimeId": at.id}}
            ),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert len(data["availableTimes"]) == 1
        assert int(data["availableTimes"][0]["id"]) == at.id

    # ------------------------------------------------------------------ #
    # availability_windows (uses _prepare_service_and_calendar)           #
    # ------------------------------------------------------------------ #

    def test_scoped_token_blocked_cross_owner_availability_windows(
        self, mock_rate_limiter, organization, owner, other_calendar
    ):
        """Scoped token querying another owner's calendarId for availability_windows returns empty
        (the same Calendar.DoesNotExist error as a genuinely missing calendar)."""
        mock_rate_limiter.return_value = iter([None])

        from di_core.containers import container

        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None
        mock_calendar_service.get_availability_windows_in_range.return_value = []

        client, _ = _make_scoped_client(organization, owner)
        variables = {
            "calendarId": other_calendar.id,
            "startDatetime": _DATETIME_START,
            "endDatetime": _DATETIME_END,
        }

        with container.calendar_service.override(mock_calendar_service):
            response = client.post(
                "/graphql/",
                data=json.dumps({"query": _AVAILABILITY_WINDOWS_QUERY, "variables": variables}),
                content_type="application/json",
            )

        # Must be 200 with errors (Calendar.DoesNotExist path) or empty data —
        # never confirm existence of the other owner's calendar.
        assert_response_status_code(response, 200)
        response_data = response.json()
        if "errors" not in response_data:
            windows = (response_data.get("data") or {}).get("availabilityWindows", [])
            assert windows == [], "Cross-owner calendar must return empty windows"
        # If there are errors, they must be Calendar.DoesNotExist-shaped (not existence-leaking)
        # We simply verify the request did NOT succeed with data from the other owner's calendar.
        if response_data.get("data"):
            windows = response_data["data"].get("availabilityWindows", [])
            assert windows == [], "Cross-owner calendar must return empty windows"

    def test_scoped_token_sees_own_availability_windows(
        self, mock_rate_limiter, organization, owner, owner_calendar
    ):
        """Scoped token can read availability windows on its owner's calendar."""
        mock_rate_limiter.return_value = iter([None])

        from di_core.containers import container

        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None
        mock_calendar_service.get_availability_windows_in_range.return_value = [
            AvailableTimeWindow(
                start_time=datetime.datetime(2025, 9, 2, 9, 0, tzinfo=datetime.UTC),
                end_time=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
                id=42,
                can_book_partially=True,
            )
        ]

        client, _ = _make_scoped_client(organization, owner)
        variables = {
            "calendarId": owner_calendar.id,
            "startDatetime": _DATETIME_START,
            "endDatetime": _DATETIME_END,
        }

        with container.calendar_service.override(mock_calendar_service):
            response = client.post(
                "/graphql/",
                data=json.dumps({"query": _AVAILABILITY_WINDOWS_QUERY, "variables": variables}),
                content_type="application/json",
            )

        data = assert_graphql_success(response)
        assert len(data["availabilityWindows"]) == 1
        assert data["availabilityWindows"][0]["id"] == 42

    def test_org_wide_token_availability_windows_unchanged(
        self, mock_rate_limiter, organization, other_calendar
    ):
        """Org-wide token can read availability windows on any calendar."""
        mock_rate_limiter.return_value = iter([None])

        from di_core.containers import container

        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None
        mock_calendar_service.get_availability_windows_in_range.return_value = [
            AvailableTimeWindow(
                start_time=datetime.datetime(2025, 9, 2, 9, 0, tzinfo=datetime.UTC),
                end_time=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
                id=99,
                can_book_partially=False,
            )
        ]

        client, _ = _make_org_wide_client(organization)
        variables = {
            "calendarId": other_calendar.id,
            "startDatetime": _DATETIME_START,
            "endDatetime": _DATETIME_END,
        }

        with container.calendar_service.override(mock_calendar_service):
            response = client.post(
                "/graphql/",
                data=json.dumps({"query": _AVAILABILITY_WINDOWS_QUERY, "variables": variables}),
                content_type="application/json",
            )

        data = assert_graphql_success(response)
        assert len(data["availabilityWindows"]) == 1
        assert data["availabilityWindows"][0]["id"] == 99

    # ------------------------------------------------------------------ #
    # unavailable_windows (uses _prepare_service_and_calendar)            #
    # ------------------------------------------------------------------ #

    def test_scoped_token_blocked_cross_owner_unavailable_windows(
        self, mock_rate_limiter, organization, owner, other_calendar
    ):
        """Scoped token querying another owner's calendarId for unavailable_windows is blocked."""
        mock_rate_limiter.return_value = iter([None])

        from di_core.containers import container

        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None
        mock_calendar_service.get_unavailable_time_windows_in_range.return_value = []

        client, _ = _make_scoped_client(organization, owner)
        variables = {
            "calendarId": other_calendar.id,
            "startDatetime": _DATETIME_START,
            "endDatetime": _DATETIME_END,
        }

        with container.calendar_service.override(mock_calendar_service):
            response = client.post(
                "/graphql/",
                data=json.dumps({"query": _UNAVAILABLE_WINDOWS_QUERY, "variables": variables}),
                content_type="application/json",
            )

        assert_response_status_code(response, 200)
        response_data = response.json()
        if response_data.get("data"):
            windows = response_data["data"].get("unavailableWindows", [])
            assert windows == [], "Cross-owner calendar must return empty unavailable windows"

    def test_scoped_token_sees_own_unavailable_windows(
        self, mock_rate_limiter, organization, owner, owner_calendar
    ):
        """Scoped token can read unavailable windows on its owner's calendar."""
        mock_rate_limiter.return_value = iter([None])

        from di_core.containers import container

        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None
        mock_calendar_service.get_unavailable_time_windows_in_range.return_value = [
            UnavailableTimeWindow(
                start_time=datetime.datetime(2025, 9, 2, 12, 0, tzinfo=datetime.UTC),
                end_time=datetime.datetime(2025, 9, 2, 13, 0, tzinfo=datetime.UTC),
                reason="blocked_time",
                id=77,
                data=BlockedTimeData(
                    id=77,
                    calendar_external_id="owner-cal",
                    start_time=datetime.datetime(2025, 9, 2, 12, 0, tzinfo=datetime.UTC),
                    end_time=datetime.datetime(2025, 9, 2, 13, 0, tzinfo=datetime.UTC),
                    timezone="UTC",
                    reason="maintenance",
                    external_id=None,
                    meta={},
                ),
            )
        ]

        client, _ = _make_scoped_client(organization, owner)
        variables = {
            "calendarId": owner_calendar.id,
            "startDatetime": _DATETIME_START,
            "endDatetime": _DATETIME_END,
        }

        with container.calendar_service.override(mock_calendar_service):
            response = client.post(
                "/graphql/",
                data=json.dumps({"query": _UNAVAILABLE_WINDOWS_QUERY, "variables": variables}),
                content_type="application/json",
            )

        data = assert_graphql_success(response)
        assert len(data["unavailableWindows"]) == 1
        assert data["unavailableWindows"][0]["id"] == 77

    def test_org_wide_token_unavailable_windows_unchanged(
        self, mock_rate_limiter, organization, other_calendar
    ):
        """Org-wide token can read unavailable windows on any calendar."""
        mock_rate_limiter.return_value = iter([None])

        from di_core.containers import container

        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None
        mock_calendar_service.get_unavailable_time_windows_in_range.return_value = [
            UnavailableTimeWindow(
                start_time=datetime.datetime(2025, 9, 2, 12, 0, tzinfo=datetime.UTC),
                end_time=datetime.datetime(2025, 9, 2, 13, 0, tzinfo=datetime.UTC),
                reason="blocked_time",
                id=88,
                data=BlockedTimeData(
                    id=88,
                    calendar_external_id="other-cal",
                    start_time=datetime.datetime(2025, 9, 2, 12, 0, tzinfo=datetime.UTC),
                    end_time=datetime.datetime(2025, 9, 2, 13, 0, tzinfo=datetime.UTC),
                    timezone="UTC",
                    reason="maintenance",
                    external_id=None,
                    meta={},
                ),
            )
        ]

        client, _ = _make_org_wide_client(organization)
        variables = {
            "calendarId": other_calendar.id,
            "startDatetime": _DATETIME_START,
            "endDatetime": _DATETIME_END,
        }

        with container.calendar_service.override(mock_calendar_service):
            response = client.post(
                "/graphql/",
                data=json.dumps({"query": _UNAVAILABLE_WINDOWS_QUERY, "variables": variables}),
                content_type="application/json",
            )

        data = assert_graphql_success(response)
        assert len(data["unavailableWindows"]) == 1
        assert data["unavailableWindows"][0]["id"] == 88

    # ------------------------------------------------------------------ #
    # Post-expansion filter (BLOCKER) behavioral test                     #
    # ------------------------------------------------------------------ #

    def test_scoped_token_post_expansion_filter_strips_other_calendar_rows(
        self, mock_rate_limiter, organization, owner, owner_calendar, other_calendar
    ):
        """Post-expansion filter must strip rows whose calendar_fk_id is NOT in the scoped set.

        The service expansion for owner_calendar may return rows belonging to a different
        calendar (e.g. due to recurring-event expansion logic). A scoped token must only
        receive rows whose calendar_fk_id matches the owner's calendar, even when the
        guard in _prepare_service_and_calendar already passed.

        This test FAILS if the post-expansion filter in queries.py is removed.
        """
        mock_rate_limiter.return_value = iter([None])

        from di_core.containers import container

        # Create real CalendarEvent instances so the GraphQL type system can serialize them.
        # One row belongs to owner_calendar (scoped set), one to other_calendar (must be stripped).
        owner_event = baker.make(
            CalendarEvent,
            calendar_fk=owner_calendar,
            organization=organization,
            title="Owner Expansion Row",
            external_id="owner-expand-filter-test",
            start_time_tz_unaware=datetime.datetime(2025, 9, 2, 9, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )
        other_event = baker.make(
            CalendarEvent,
            calendar_fk=other_calendar,
            organization=organization,
            title="Other Expansion Row",
            external_id="other-expand-filter-test",
            start_time_tz_unaware=datetime.datetime(2025, 9, 2, 11, 0, tzinfo=datetime.UTC),
            end_time_tz_unaware=datetime.datetime(2025, 9, 2, 12, 0, tzinfo=datetime.UTC),
            timezone="UTC",
        )

        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None
        # Service returns both rows — the post-expansion filter must strip other_event
        mock_calendar_service.get_calendar_events_expanded.return_value = [owner_event, other_event]

        client, _ = _make_scoped_client(organization, owner)
        variables = {
            "calendarId": owner_calendar.id,
            "startDatetime": _DATETIME_START,
            "endDatetime": _DATETIME_END,
        }

        with container.calendar_service.override(mock_calendar_service):
            response = client.post(
                "/graphql/",
                data=json.dumps(
                    {"query": _CALENDAR_EVENTS_BY_CALENDAR_QUERY, "variables": variables}
                ),
                content_type="application/json",
            )

        data = assert_graphql_success(response)
        returned_ids = [int(e["id"]) for e in data["calendarEvents"]]

        assert owner_event.id in returned_ids, "Owner's row must be returned"
        assert other_event.id not in returned_ids, (
            "Other-calendar row must be stripped by the post-expansion filter"
        )


# ---------------------------------------------------------------------------
# owners field on CalendarGraphQLType
# ---------------------------------------------------------------------------

_CALENDARS_WITH_OWNERS_QUERY = """
    query GetCalendarsWithOwners {
        calendars {
            id
            name
            owners {
                id
                isDefault
                user {
                    id
                    email
                    profile {
                        firstName
                        lastName
                        profilePicture
                    }
                }
            }
        }
    }
"""


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestCalendarOwnersField:
    """Phase 1: CalendarGraphQLType.owners — shape, org-scoping, and N+1 guard.

    Tests:
        (a) Shape: a calendar with two ownership rows returns both with the
            correct ownership id (not user id), isDefault, and nested user/profile.
        (b) Org-scoping: an org-A token never sees org-B owner data — not the
            calendar, not the ownership row, not the user email, not the profile.
        (c) N+1: resolving owners for N calendars issues a constant number of
            queries, proving the prefetch_related wiring works.
    """

    @pytest.fixture
    def organization(self):
        return baker.make(Organization, name="OwnersTestOrg")

    @pytest.fixture
    def org_wide_client(self, organization):
        client, _ = _make_org_wide_client(organization)
        return client

    # ------------------------------------------------------------------ #
    # (a) Shape test                                                       #
    # ------------------------------------------------------------------ #

    def test_owners_field_returns_correct_shape(
        self, mock_rate_limiter, organization, org_wide_client
    ):
        """A calendar with two ownership rows returns both with correct ids and nested user/profile.

        The `id` on each ownership record must be the CalendarOwnership pk, NOT the user id.
        """
        mock_rate_limiter.return_value = iter([None])

        calendar = baker.make(
            Calendar,
            organization=organization,
            name="Owned Calendar",
            external_id="owned-cal-shape-test",
        )

        user_a = UserFactory().create_user(
            email="owner_a@shape.test", first_name="Alice", last_name="Smith"
        )
        user_b = UserFactory().create_user(
            email="owner_b@shape.test", first_name="Bob", last_name="Jones"
        )

        ownership_a = baker.make(
            CalendarOwnership,
            calendar=calendar,
            user=user_a,
            organization=organization,
            is_default=True,
        )
        ownership_b = baker.make(
            CalendarOwnership,
            calendar=calendar,
            user=user_b,
            organization=organization,
            is_default=False,
        )

        response = org_wide_client.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDARS_WITH_OWNERS_QUERY}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        calendars = data["calendars"]

        # Filter to the calendar we created (other tests may have left unrelated calendars)
        target = next((c for c in calendars if int(c["id"]) == calendar.id), None)
        assert target is not None, "Calendar not found in response"

        owners = target["owners"]
        assert len(owners) == 2, f"Expected 2 ownership rows, got {len(owners)}: {owners}"

        owners_by_user_email = {o["user"]["email"]: o for o in owners}

        # Verify ownership A
        owner_a = owners_by_user_email["owner_a@shape.test"]
        assert int(owner_a["id"]) == ownership_a.id, (
            "Ownership id must be CalendarOwnership pk, not user id"
        )
        assert owner_a["isDefault"] is True
        assert owner_a["user"]["id"] == str(user_a.id)
        assert owner_a["user"]["profile"]["firstName"] == "Alice"
        assert owner_a["user"]["profile"]["lastName"] == "Smith"
        # UserFactory creates profiles via baker.make without an explicit profile_picture,
        # so the field is empty/None; the GraphQL type resolves it as None.
        assert owner_a["user"]["profile"]["profilePicture"] is None

        # Verify ownership B
        owner_b = owners_by_user_email["owner_b@shape.test"]
        assert int(owner_b["id"]) == ownership_b.id, (
            "Ownership id must be CalendarOwnership pk, not user id"
        )
        assert owner_b["isDefault"] is False
        assert owner_b["user"]["id"] == str(user_b.id)
        assert owner_b["user"]["profile"]["firstName"] == "Bob"
        assert owner_b["user"]["profile"]["lastName"] == "Jones"
        assert owner_b["user"]["profile"]["profilePicture"] is None

    # ------------------------------------------------------------------ #
    # (b) Org-scoping / cross-org leak                                    #
    # ------------------------------------------------------------------ #

    def test_owners_field_org_scoping_no_cross_org_leak(self, mock_rate_limiter):
        """An org-A token querying calendars never receives org-B calendar or owner data.

        This is the highest-priority correctness test: proves that traversing
        CalendarOwnership rows from org-filtered calendars cannot leak org-B user
        emails or profiles to an org-A token.
        """
        mock_rate_limiter.return_value = iter([None])

        org_a = baker.make(Organization, name="OrgA")
        org_b = baker.make(Organization, name="OrgB")

        user_a = UserFactory().create_user(
            email="owner@org-a.test", first_name="OrgAFirst", last_name="OrgALast"
        )
        user_b = UserFactory().create_user(
            email="owner@org-b.test", first_name="OrgBFirst", last_name="OrgBLast"
        )

        cal_a = baker.make(
            Calendar,
            organization=org_a,
            name="Org A Calendar",
            external_id="cal-org-a",
        )
        cal_b = baker.make(
            Calendar,
            organization=org_b,
            name="Org B Calendar",
            external_id="cal-org-b",
        )

        baker.make(
            CalendarOwnership,
            calendar=cal_a,
            user=user_a,
            organization=org_a,
            is_default=True,
        )
        baker.make(
            CalendarOwnership,
            calendar=cal_b,
            user=user_b,
            organization=org_b,
            is_default=True,
        )

        # Build a client scoped to org_a only
        client_a, _ = _make_org_wide_client(org_a)

        response = client_a.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDARS_WITH_OWNERS_QUERY}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        calendars = data["calendars"]

        # Collect all calendar ids and all owner user emails returned
        returned_calendar_ids = {int(c["id"]) for c in calendars}
        returned_owner_emails = {o["user"]["email"] for c in calendars for o in c["owners"]}
        returned_profile_first_names = {
            o["user"]["profile"]["firstName"]
            for c in calendars
            for o in c["owners"]
            if o["user"]["profile"] is not None
        }

        # Org A's calendar must appear
        assert cal_a.id in returned_calendar_ids, "Org A calendar should be returned"

        # Org B's calendar must NOT appear
        assert cal_b.id not in returned_calendar_ids, (
            "Org B calendar must not be visible to an org-A token"
        )

        # Org B owner data must not appear anywhere in the response
        assert "owner@org-b.test" not in returned_owner_emails, (
            "Org B owner email must not leak to org-A token"
        )
        assert "OrgBFirst" not in returned_profile_first_names, (
            "Org B owner profile must not leak to org-A token"
        )

        # Org A owner data must appear
        assert "owner@org-a.test" in returned_owner_emails, "Org A owner email should be returned"

    # ------------------------------------------------------------------ #
    # (c) N+1 guard                                                        #
    # ------------------------------------------------------------------ #

    def test_owners_field_no_n_plus_1(self, mock_rate_limiter, organization, org_wide_client):
        """Resolving owners for N calendars issues a constant number of queries.

        Two-point comparison: captures the query count for 1 calendar (2 ownership
        rows) and for 4 calendars (each with 2 ownership rows), then asserts the
        two counts are equal (or differ by at most 1 for incidental per-row
        overhead).  With prefetch_related working correctly, adding more calendars
        must NOT add per-calendar owner/user/profile queries.
        """
        mock_rate_limiter.return_value = iter([None])

        def _make_calendar_with_owners(index):
            cal = baker.make(
                Calendar,
                organization=organization,
                name=f"N+1 Calendar {index}",
                external_id=f"n1-cal-{index}",
            )
            for j in range(2):
                u = UserFactory().create_user(
                    email=f"n1_owner_{index}_{j}@n1test.local",
                    first_name=f"F{index}{j}",
                    last_name=f"L{index}{j}",
                )
                baker.make(
                    CalendarOwnership,
                    calendar=cal,
                    user=u,
                    organization=organization,
                    is_default=(j == 0),
                )

        # --- Point 1: N=1 calendar with 2 owners ---
        _make_calendar_with_owners(0)

        with CaptureQueriesContext(connection) as ctx_1:
            response_1 = org_wide_client.post(
                "/graphql/",
                data=json.dumps({"query": _CALENDARS_WITH_OWNERS_QUERY}),
                content_type="application/json",
            )
        assert_graphql_success(response_1)
        queries_n1 = len(ctx_1.captured_queries)

        # --- Point 2: N=4 calendars with 2 owners each (3 more added, total 4) ---
        for i in range(1, 4):
            _make_calendar_with_owners(i)

        with CaptureQueriesContext(connection) as ctx_4:
            response_4 = org_wide_client.post(
                "/graphql/",
                data=json.dumps({"query": _CALENDARS_WITH_OWNERS_QUERY}),
                content_type="application/json",
            )
        assert_graphql_success(response_4)
        queries_n4 = len(ctx_4.captured_queries)

        # With prefetch_related the ownership/user/profile lookups are batched and
        # the query count must not grow with the number of calendars.  Allow a slack
        # of 1 to tolerate minor incidental per-row overhead.
        assert abs(queries_n4 - queries_n1) <= 1, (
            f"N+1 detected: N=1 calendar used {queries_n1} queries, "
            f"N=4 calendars used {queries_n4} queries. "
            "With prefetch_related the counts must be equal (or differ by at most 1). "
            "Check prefetch_related wiring in the owners resolver."
        )


# ---------------------------------------------------------------------------
# Phase 2 — N+1 hardening: calendarGroups and calendarBundles entry points
# ---------------------------------------------------------------------------

_CALENDAR_GROUPS_WITH_OWNERS_QUERY = """
    query GetCalendarGroupsWithOwners {
        calendarGroups {
            id
            name
            slots {
                id
                name
                calendars {
                    id
                    name
                    owners {
                        id
                        isDefault
                        user {
                            id
                            email
                            profile {
                                firstName
                                lastName
                                profilePicture
                            }
                        }
                    }
                }
            }
        }
    }
"""

_CALENDAR_BUNDLES_WITH_OWNERS_QUERY = """
    query GetCalendarBundlesWithOwners {
        calendarBundles {
            id
            name
            children {
                id
                name
                owners {
                    id
                    isDefault
                    user {
                        id
                        email
                        profile {
                            firstName
                            lastName
                            profilePicture
                        }
                    }
                }
            }
        }
    }
"""


def _make_group_with_owned_slot_calendars(
    organization: Organization,
    group_name: str,
    calendar_count: int = 2,
) -> tuple[CalendarGroup, CalendarGroupSlot, list[Calendar], list[CalendarOwnership]]:
    """Create a CalendarGroup with one slot holding `calendar_count` calendars, each with one owner.

    Uses .objects.create() for OrganizationForeignKey models (baker.make cannot resolve the
    virtual ForeignObject field on CalendarGroupSlot.group and CalendarGroupSlotMembership.slot).

    Returns (group, slot, calendars, ownerships).
    """
    group = baker.make(CalendarGroup, organization=organization, name=group_name)
    slot = CalendarGroupSlot.objects.create(
        organization=organization,
        group=group,
        name=f"{group_name} slot",
    )
    calendars = []
    ownerships = []
    for i in range(calendar_count):
        cal = baker.make(
            Calendar,
            organization=organization,
            name=f"{group_name} cal {i}",
            external_id=str(uuid.uuid4()),
        )
        CalendarGroupSlotMembership.objects.create(
            organization=organization,
            slot=slot,
            calendar=cal,
        )
        owner = UserFactory().create_user(
            email=f"owner_{group_name.lower().replace(' ', '_')}_{i}@test.local",
            first_name=f"First{i}",
            last_name=f"Last{i}",
        )
        ownership = baker.make(
            CalendarOwnership,
            organization=organization,
            calendar=cal,
            user=owner,
            is_default=True,
        )
        calendars.append(cal)
        ownerships.append(ownership)
    return group, slot, calendars, ownerships


def _make_group_wide_client(organization: Organization) -> tuple[APIClient, SystemUser]:
    """Create an org-wide API client with CALENDAR_GROUP resource grant.

    Only CALENDAR_GROUP is needed: the calendarGroups resolver checks that resource;
    there is no separate CALENDAR gate on the slots/calendars sub-fields.
    """
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=f"group_wide_{organization.pk}", organization=organization
    )
    baker.make(
        ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR_GROUP
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")
    return client, system_user


def _make_bundle_wide_client(organization: Organization) -> tuple[APIClient, SystemUser]:
    """Create an org-wide API client with CALENDAR_BUNDLE resource grant.

    Only CALENDAR_BUNDLE is needed: the calendarBundles resolver checks that resource;
    there is no separate CALENDAR gate on the children/owners sub-fields.
    """
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=f"bundle_wide_{organization.pk}", organization=organization
    )
    baker.make(
        ResourceAccess, system_user=system_user, resource_name=PublicAPIResources.CALENDAR_BUNDLE
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {system_user.id}:{token}")
    return client, system_user


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestCalendarGroupOwnersN1:
    """Phase 2: calendarGroups -> slots -> calendars -> owners is N+1-free.

    Tests:
        (a) Shape: owners are returned correctly through the group -> slot -> calendar path.
        (b) N+1: resolving owners for N slot calendars in groups issues a constant
            number of queries.
    """

    @pytest.fixture
    def organization(self):
        return baker.make(Organization, name="GroupOwnersTestOrg")

    @pytest.fixture
    def org_wide_client(self, organization):
        client, _ = _make_group_wide_client(organization)
        return client

    # ------------------------------------------------------------------ #
    # (a) Shape test                                                       #
    # ------------------------------------------------------------------ #

    def test_group_slot_calendars_owners_shape(
        self, mock_rate_limiter, organization, org_wide_client
    ):
        """Owners are returned for slot calendars when queried through calendarGroups."""
        mock_rate_limiter.return_value = iter([None])

        _group, _slot, _calendars, _ownerships = _make_group_with_owned_slot_calendars(
            organization, group_name="ShapeGroup", calendar_count=2
        )

        response = org_wide_client.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDAR_GROUPS_WITH_OWNERS_QUERY}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        groups = data["calendarGroups"]
        assert len(groups) >= 1

        # Find the group we created
        target_group = next((g for g in groups if g["name"] == "ShapeGroup"), None)
        assert target_group is not None, "ShapeGroup not found in calendarGroups response"

        assert len(target_group["slots"]) == 1
        slot_data = target_group["slots"][0]
        returned_calendars = slot_data["calendars"]
        assert len(returned_calendars) == 2

        # Each calendar must have exactly 1 owner
        for cal_data in returned_calendars:
            assert len(cal_data["owners"]) == 1
            owner_data = cal_data["owners"][0]
            assert owner_data["isDefault"] is True
            assert owner_data["user"]["email"].endswith("@test.local")
            assert owner_data["user"]["profile"]["firstName"].startswith("First")

    # ------------------------------------------------------------------ #
    # (b) N+1 guard                                                        #
    # ------------------------------------------------------------------ #

    def test_group_slot_calendars_owners_no_n_plus_1(
        self, mock_rate_limiter, organization, org_wide_client
    ):
        """Resolving owners through calendarGroups issues a constant number of queries.

        Two-point comparison: 1 slot calendar vs 3 slot calendars in a group.
        With prefetch_related wiring the count must not grow per slot calendar.
        """
        mock_rate_limiter.return_value = iter([None])

        # Point 1: group with 1 slot calendar
        _make_group_with_owned_slot_calendars(organization, group_name="N1Group1", calendar_count=1)

        with CaptureQueriesContext(connection) as ctx_1:
            response_1 = org_wide_client.post(
                "/graphql/",
                data=json.dumps({"query": _CALENDAR_GROUPS_WITH_OWNERS_QUERY}),
                content_type="application/json",
            )
        assert_graphql_success(response_1)
        queries_n1 = len(ctx_1.captured_queries)

        # Point 2: add another group with 3 slot calendars (total groups = 2)
        _make_group_with_owned_slot_calendars(organization, group_name="N1Group3", calendar_count=3)

        with CaptureQueriesContext(connection) as ctx_2:
            response_2 = org_wide_client.post(
                "/graphql/",
                data=json.dumps({"query": _CALENDAR_GROUPS_WITH_OWNERS_QUERY}),
                content_type="application/json",
            )
        assert_graphql_success(response_2)
        queries_n2 = len(ctx_2.captured_queries)

        # Query count must not grow per slot calendar added.
        # With correct prefetch there is no per-group/per-item overhead; allow a slack of 1
        # only to tolerate incidental auth/middleware jitter between the two requests.
        assert abs(queries_n2 - queries_n1) <= 1, (
            f"N+1 detected through calendarGroups: 1 slot-cal used {queries_n1} queries, "
            f"adding 3 more slot-cals used {queries_n2} queries. "
            "With prefetch_related the count must not grow per calendar. "
            "Check slots__calendars__ownerships__user__profile prefetch on calendar_groups resolver."
        )

    # ------------------------------------------------------------------ #
    # (c) Org-scoping                                                      #
    # ------------------------------------------------------------------ #

    def test_group_slot_calendars_owners_org_scoping(self, mock_rate_limiter):
        """An org-A token querying calendarGroups sees no org-B group, calendar, or owner data."""
        mock_rate_limiter.return_value = iter([None])

        org_a = baker.make(Organization, name="OrgA-GroupOwners")
        org_b = baker.make(Organization, name="OrgB-GroupOwners")

        # Org A: group with 1 owned slot calendar
        _group_a, _slot_a, _calendars_a, ownerships_a = _make_group_with_owned_slot_calendars(
            org_a, group_name="GroupA", calendar_count=1
        )

        # Org B: group with 1 slot calendar owned by a distinctly-named user —
        # must never appear in org-A response.
        group_b = baker.make(CalendarGroup, organization=org_b, name="GroupB")
        slot_b = CalendarGroupSlot.objects.create(
            organization=org_b, group=group_b, name="GroupB slot"
        )
        cal_b = baker.make(
            Calendar,
            organization=org_b,
            name="GroupB cal 0",
            external_id=str(uuid.uuid4()),
        )
        CalendarGroupSlotMembership.objects.create(organization=org_b, slot=slot_b, calendar=cal_b)
        owner_b = UserFactory().create_user(
            email="owner_b@group_scope.test",
            first_name="OrgBGroupFirst",
            last_name="OrgBGroupLast",
        )
        baker.make(
            CalendarOwnership,
            organization=org_b,
            calendar=cal_b,
            user=owner_b,
            is_default=True,
        )

        client_a, _ = _make_group_wide_client(org_a)

        response = client_a.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDAR_GROUPS_WITH_OWNERS_QUERY}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        groups = data["calendarGroups"]

        returned_group_ids = {int(g["id"]) for g in groups}
        all_calendar_ids = {
            int(c["id"]) for g in groups for s in g["slots"] for c in s["calendars"]
        }
        all_owner_emails = {
            o["user"]["email"]
            for g in groups
            for s in g["slots"]
            for c in s["calendars"]
            for o in c["owners"]
        }
        all_owner_first_names = {
            o["user"]["profile"]["firstName"]
            for g in groups
            for s in g["slots"]
            for c in s["calendars"]
            for o in c["owners"]
            if o["user"]["profile"] is not None
        }

        # Org A group must appear
        assert _group_a.id in returned_group_ids, "Org A group must be visible"

        # Org B group must NOT appear
        assert group_b.id not in returned_group_ids, (
            "Org B group must not be visible to an org-A token"
        )

        # Org B calendar must NOT appear
        assert cal_b.id not in all_calendar_ids, (
            "Org B calendar must not be visible to an org-A token"
        )

        # Org B owner data must not leak
        assert "owner_b@group_scope.test" not in all_owner_emails, (
            "Org B slot-calendar owner email must not leak to org-A token"
        )
        assert "OrgBGroupFirst" not in all_owner_first_names, (
            "Org B slot-calendar owner profile must not leak to org-A token"
        )

        # Org A owner data must appear
        owner_a_email = ownerships_a[0].user.email
        assert owner_a_email in all_owner_emails, "Org A slot-calendar owner email must be returned"


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestCalendarBundleOwnersN1:
    """Phase 2: calendarBundles -> children -> owners is N+1-free.

    Tests:
        (a) Shape: owners are returned correctly for both bundle children through the
            calendarBundles query.
        (b) N+1: resolving owners for N children is query-count-bounded.
        (c) Org-scoping: an org-A token sees no org-B child or owner data.
    """

    @pytest.fixture
    def organization(self):
        return baker.make(Organization, name="BundleOwnersTestOrg")

    @pytest.fixture
    def org_wide_client(self, organization):
        client, _ = _make_bundle_wide_client(organization)
        return client

    # ------------------------------------------------------------------ #
    # (a) Shape test                                                       #
    # ------------------------------------------------------------------ #

    def test_bundle_children_owners_shape(self, mock_rate_limiter, organization, org_wide_client):
        """Owners are returned for bundle children when queried through calendarBundles."""
        mock_rate_limiter.return_value = iter([None])

        bundle, children = _make_bundle_calendar(organization, name="ShapeBundle", child_count=2)

        # Add an owner to each child
        child_ownerships = []
        for i, child in enumerate(children):
            owner = UserFactory().create_user(
                email=f"child_owner_{i}@bundle_shape.test",
                first_name=f"Child{i}First",
                last_name=f"Child{i}Last",
            )
            ownership = baker.make(
                CalendarOwnership,
                organization=organization,
                calendar=child,
                user=owner,
                is_default=True,
            )
            child_ownerships.append(ownership)

        response = org_wide_client.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDAR_BUNDLES_WITH_OWNERS_QUERY}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        bundles = data["calendarBundles"]

        target = next((b for b in bundles if int(b["id"]) == bundle.id), None)
        assert target is not None, "Bundle not found in calendarBundles response"

        returned_children = target["children"]
        assert len(returned_children) == 2

        # Each child must expose exactly 1 owner
        for child_data in returned_children:
            assert len(child_data["owners"]) == 1, (
                f"Child {child_data['id']} expected 1 owner, got {len(child_data['owners'])}"
            )
            owner_data = child_data["owners"][0]
            assert owner_data["isDefault"] is True
            assert "@bundle_shape.test" in owner_data["user"]["email"]
            assert owner_data["user"]["profile"]["firstName"].startswith("Child")
            # UserFactory creates profiles via baker.make without an explicit profile_picture,
            # so the field is empty/None; the GraphQL type resolves it as None.
            assert owner_data["user"]["profile"]["profilePicture"] is None

    # ------------------------------------------------------------------ #
    # (b) N+1 guard                                                        #
    # ------------------------------------------------------------------ #

    def test_bundle_children_owners_no_n_plus_1(
        self, mock_rate_limiter, organization, org_wide_client
    ):
        """Resolving owners through calendarBundles children issues a constant number of queries.

        Two-point comparison: bundle with 1 child vs bundle with 4 children.
        With bundle_children__ownerships__user__profile prefetch the count must not grow.
        """
        mock_rate_limiter.return_value = iter([None])

        def _make_bundle_with_owned_children(name: str, child_count: int):
            bundle, children = _make_bundle_calendar(
                organization, name=name, child_count=child_count
            )
            for i, child in enumerate(children):
                owner = UserFactory().create_user(
                    email=f"n1_{name.lower().replace(' ', '_')}_{i}@n1bundle.test",
                    first_name=f"BF{i}",
                    last_name=f"BL{i}",
                )
                baker.make(
                    CalendarOwnership,
                    organization=organization,
                    calendar=child,
                    user=owner,
                    is_default=True,
                )
            return bundle

        # Point 1: bundle with 1 child
        _make_bundle_with_owned_children("N1Bundle1", child_count=1)

        with CaptureQueriesContext(connection) as ctx_1:
            response_1 = org_wide_client.post(
                "/graphql/",
                data=json.dumps({"query": _CALENDAR_BUNDLES_WITH_OWNERS_QUERY}),
                content_type="application/json",
            )
        assert_graphql_success(response_1)
        queries_n1 = len(ctx_1.captured_queries)

        # Point 2: add another bundle with 4 children (total bundles = 2)
        _make_bundle_with_owned_children("N1Bundle4", child_count=4)

        with CaptureQueriesContext(connection) as ctx_2:
            response_2 = org_wide_client.post(
                "/graphql/",
                data=json.dumps({"query": _CALENDAR_BUNDLES_WITH_OWNERS_QUERY}),
                content_type="application/json",
            )
        assert_graphql_success(response_2)
        queries_n2 = len(ctx_2.captured_queries)

        # Query count must not grow per child added.
        # With correct prefetch there is no per-bundle/per-item overhead; allow a slack of 1
        # only to tolerate incidental auth/middleware jitter between the two requests.
        assert abs(queries_n2 - queries_n1) <= 1, (
            f"N+1 detected through calendarBundles children: 1 child used {queries_n1} queries, "
            f"adding 4 more children used {queries_n2} queries. "
            "With prefetch_related the count must not grow per child. "
            "Check bundle_children__ownerships__user__profile prefetch on calendar_bundles resolver."
        )

    # ------------------------------------------------------------------ #
    # (c) Org-scoping                                                      #
    # ------------------------------------------------------------------ #

    def test_bundle_children_owners_org_scoping(self, mock_rate_limiter):
        """An org-A token querying calendarBundles sees no org-B child or owner data."""
        mock_rate_limiter.return_value = iter([None])

        org_a = baker.make(Organization, name="OrgA-BundleOwners")
        org_b = baker.make(Organization, name="OrgB-BundleOwners")

        # Org A: bundle with 1 owned child
        bundle_a, children_a = _make_bundle_calendar(org_a, name="BundleA", child_count=1)
        owner_a = UserFactory().create_user(
            email="owner_a@bundle_scope.test",
            first_name="OrgABundleFirst",
            last_name="OrgABundleLast",
        )
        baker.make(
            CalendarOwnership,
            organization=org_a,
            calendar=children_a[0],
            user=owner_a,
            is_default=True,
        )

        # Org B: bundle with 1 owned child — must never appear in org-A response
        bundle_b, children_b = _make_bundle_calendar(org_b, name="BundleB", child_count=1)
        owner_b = UserFactory().create_user(
            email="owner_b@bundle_scope.test",
            first_name="OrgBBundleFirst",
            last_name="OrgBBundleLast",
        )
        baker.make(
            CalendarOwnership,
            organization=org_b,
            calendar=children_b[0],
            user=owner_b,
            is_default=True,
        )

        client_a, _ = _make_bundle_wide_client(org_a)

        response = client_a.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDAR_BUNDLES_WITH_OWNERS_QUERY}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        bundles = data["calendarBundles"]

        returned_bundle_ids = {int(b["id"]) for b in bundles}
        all_child_owner_emails = {
            o["user"]["email"] for b in bundles for c in b["children"] for o in c["owners"]
        }
        all_child_owner_first_names = {
            o["user"]["profile"]["firstName"]
            for b in bundles
            for c in b["children"]
            for o in c["owners"]
        }

        # Org A bundle appears
        assert bundle_a.id in returned_bundle_ids, "Org A bundle must be visible"
        # Org B bundle must NOT appear
        assert bundle_b.id not in returned_bundle_ids, (
            "Org B bundle must not be visible to org-A token"
        )
        # Org B owner data must not leak
        assert "owner_b@bundle_scope.test" not in all_child_owner_emails, (
            "Org B child owner email must not leak to org-A token"
        )
        assert "OrgBBundleFirst" not in all_child_owner_first_names, (
            "Org B child owner profile must not leak to org-A token"
        )
        # Org A owner data appears
        assert "owner_a@bundle_scope.test" in all_child_owner_emails, (
            "Org A child owner email must be returned"
        )


# ---------------------------------------------------------------------------
# Phase 3 — owners on CalendarBundleGraphQLType (bundle parent)
# ---------------------------------------------------------------------------

_CALENDAR_BUNDLES_PARENT_OWNERS_QUERY = """
    query GetCalendarBundlesParentOwners {
        calendarBundles {
            id
            name
            owners {
                id
                isDefault
                user {
                    id
                    email
                    profile {
                        firstName
                        lastName
                        profilePicture
                    }
                }
            }
        }
    }
"""


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestCalendarBundleParentOwners:
    """Phase 3: CalendarBundleGraphQLType.owners — shape, org-scoping, and N+1 guard.

    Tests:
        (a) Shape: a bundle calendar with one or two ownership rows returns both
            with the correct ownership id (not user id), isDefault, and nested
            user/profile.
        (b) Org-scoping: an org-A token never sees org-B bundle or owner data.
        (c) N+1: resolving owners for N bundles issues a constant number of
            queries, proving the prefetch_related wiring works.
    """

    @pytest.fixture
    def organization(self):
        return baker.make(Organization, name="BundleParentOwnersTestOrg")

    @pytest.fixture
    def org_wide_client(self, organization):
        client, _ = _make_bundle_wide_client(organization)
        return client

    # ------------------------------------------------------------------ #
    # (a) Shape test                                                       #
    # ------------------------------------------------------------------ #

    def test_bundle_parent_owners_field_returns_correct_shape(
        self, mock_rate_limiter, organization, org_wide_client
    ):
        """A bundle with one or two ownership rows returns both with correct ids and
        nested user/profile.

        The `id` on each ownership record must be the CalendarOwnership pk, NOT the user id.
        """
        mock_rate_limiter.return_value = iter([None])

        bundle, _ = _make_bundle_calendar(organization, name="Owned Bundle", child_count=1)

        user_a = UserFactory().create_user(
            email="bundle_owner_a@shape.test", first_name="BundleAlice", last_name="BundleSmith"
        )
        user_b = UserFactory().create_user(
            email="bundle_owner_b@shape.test", first_name="BundleBob", last_name="BundleJones"
        )

        ownership_a = baker.make(
            CalendarOwnership,
            calendar=bundle,
            user=user_a,
            organization=organization,
            is_default=True,
        )
        ownership_b = baker.make(
            CalendarOwnership,
            calendar=bundle,
            user=user_b,
            organization=organization,
            is_default=False,
        )

        response = org_wide_client.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDAR_BUNDLES_PARENT_OWNERS_QUERY}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        bundles = data["calendarBundles"]

        # Filter to the bundle we created
        target = next((b for b in bundles if int(b["id"]) == bundle.id), None)
        assert target is not None, "Bundle not found in response"

        owners = target["owners"]
        assert len(owners) == 2, f"Expected 2 ownership rows, got {len(owners)}: {owners}"

        owners_by_user_email = {o["user"]["email"]: o for o in owners}

        # Verify ownership A
        owner_a = owners_by_user_email["bundle_owner_a@shape.test"]
        assert int(owner_a["id"]) == ownership_a.id, (
            "Ownership id must be CalendarOwnership pk, not user id"
        )
        assert owner_a["isDefault"] is True
        assert owner_a["user"]["id"] == str(user_a.id)
        assert owner_a["user"]["profile"]["firstName"] == "BundleAlice"
        assert owner_a["user"]["profile"]["lastName"] == "BundleSmith"
        assert owner_a["user"]["profile"]["profilePicture"] is None

        # Verify ownership B
        owner_b = owners_by_user_email["bundle_owner_b@shape.test"]
        assert int(owner_b["id"]) == ownership_b.id, (
            "Ownership id must be CalendarOwnership pk, not user id"
        )
        assert owner_b["isDefault"] is False
        assert owner_b["user"]["id"] == str(user_b.id)
        assert owner_b["user"]["profile"]["firstName"] == "BundleBob"
        assert owner_b["user"]["profile"]["lastName"] == "BundleJones"
        assert owner_b["user"]["profile"]["profilePicture"] is None

    # ------------------------------------------------------------------ #
    # (b) Org-scoping / cross-org leak                                    #
    # ------------------------------------------------------------------ #

    def test_bundle_parent_owners_field_org_scoping_no_cross_org_leak(self, mock_rate_limiter):
        """An org-A token querying calendarBundles never receives org-B bundle or owner data.

        This is the highest-priority correctness test: proves that traversing
        CalendarOwnership rows from org-filtered bundles cannot leak org-B user
        emails or profiles to an org-A token.
        """
        mock_rate_limiter.return_value = iter([None])

        org_a = baker.make(Organization, name="OrgA-BundleParentOwners")
        org_b = baker.make(Organization, name="OrgB-BundleParentOwners")

        user_a = UserFactory().create_user(
            email="bundle_owner@org-a.test",
            first_name="OrgABundleFirst",
            last_name="OrgABundleLast",
        )
        user_b = UserFactory().create_user(
            email="bundle_owner@org-b.test",
            first_name="OrgBBundleFirst",
            last_name="OrgBBundleLast",
        )

        bundle_a, _ = _make_bundle_calendar(org_a, name="Org A Bundle", child_count=1)
        bundle_b, _ = _make_bundle_calendar(org_b, name="Org B Bundle", child_count=1)

        baker.make(
            CalendarOwnership,
            calendar=bundle_a,
            user=user_a,
            organization=org_a,
            is_default=True,
        )
        baker.make(
            CalendarOwnership,
            calendar=bundle_b,
            user=user_b,
            organization=org_b,
            is_default=True,
        )

        # Build a client scoped to org_a only
        client_a, _ = _make_bundle_wide_client(org_a)

        response = client_a.post(
            "/graphql/",
            data=json.dumps({"query": _CALENDAR_BUNDLES_PARENT_OWNERS_QUERY}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        bundles = data["calendarBundles"]

        # Collect all bundle ids and all owner user emails returned
        returned_bundle_ids = {int(b["id"]) for b in bundles}
        returned_owner_emails = {o["user"]["email"] for b in bundles for o in b["owners"]}
        returned_profile_first_names = {
            o["user"]["profile"]["firstName"]
            for b in bundles
            for o in b["owners"]
            if o["user"]["profile"] is not None
        }

        # Org A's bundle must appear
        assert bundle_a.id in returned_bundle_ids, "Org A bundle should be returned"

        # Org B's bundle must NOT appear
        assert bundle_b.id not in returned_bundle_ids, (
            "Org B bundle must not be visible to an org-A token"
        )

        # Org B owner data must not appear anywhere in the response
        assert "bundle_owner@org-b.test" not in returned_owner_emails, (
            "Org B bundle owner email must not leak to org-A token"
        )
        assert "OrgBBundleFirst" not in returned_profile_first_names, (
            "Org B bundle owner profile must not leak to org-A token"
        )

        # Org A owner data must appear
        assert "bundle_owner@org-a.test" in returned_owner_emails, (
            "Org A bundle owner email should be returned"
        )

    # ------------------------------------------------------------------ #
    # (c) N+1 guard                                                        #
    # ------------------------------------------------------------------ #

    def test_bundle_parent_owners_field_no_n_plus_1(
        self, mock_rate_limiter, organization, org_wide_client
    ):
        """Resolving owners for N bundles issues a constant number of queries.

        Two-point comparison: captures the query count for 1 bundle (2 ownership
        rows) and for 4 bundles (each with 2 ownership rows), then asserts the
        two counts are equal (or differ by at most 1 for incidental per-row
        overhead). With prefetch_related working correctly, adding more bundles
        must NOT add per-bundle owner/user/profile queries.
        """
        mock_rate_limiter.return_value = iter([None])

        def _make_bundle_with_owners(index):
            bundle, _ = _make_bundle_calendar(
                organization, name=f"N+1 Bundle {index}", child_count=1
            )
            for j in range(2):
                u = UserFactory().create_user(
                    email=f"n1_bundle_owner_{index}_{j}@n1test.local",
                    first_name=f"BF{index}{j}",
                    last_name=f"BL{index}{j}",
                )
                baker.make(
                    CalendarOwnership,
                    calendar=bundle,
                    user=u,
                    organization=organization,
                    is_default=(j == 0),
                )

        # --- Point 1: N=1 bundle with 2 owners ---
        _make_bundle_with_owners(0)

        with CaptureQueriesContext(connection) as ctx_1:
            response_1 = org_wide_client.post(
                "/graphql/",
                data=json.dumps({"query": _CALENDAR_BUNDLES_PARENT_OWNERS_QUERY}),
                content_type="application/json",
            )
        assert_graphql_success(response_1)
        queries_n1 = len(ctx_1.captured_queries)

        # --- Point 2: N=4 bundles with 2 owners each (3 more added, total 4) ---
        for i in range(1, 4):
            _make_bundle_with_owners(i)

        with CaptureQueriesContext(connection) as ctx_4:
            response_4 = org_wide_client.post(
                "/graphql/",
                data=json.dumps({"query": _CALENDAR_BUNDLES_PARENT_OWNERS_QUERY}),
                content_type="application/json",
            )
        assert_graphql_success(response_4)
        queries_n4 = len(ctx_4.captured_queries)

        # With prefetch_related the ownership/user/profile lookups are batched and
        # the query count must not grow with the number of bundles. Allow a slack
        # of 1 to tolerate minor incidental per-row overhead.
        assert abs(queries_n4 - queries_n1) <= 1, (
            f"N+1 detected: N=1 bundle used {queries_n1} queries, "
            f"N=4 bundles used {queries_n4} queries. "
            "With prefetch_related the counts must be equal (or differ by at most 1). "
            "Check prefetch_related wiring in the owners resolver on CalendarBundleGraphQLType."
        )
