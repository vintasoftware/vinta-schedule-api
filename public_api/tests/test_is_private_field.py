"""Tests for the is_private field on Calendar, AppointmentType, and CalendarBundle."""

import json
from unittest.mock import patch

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarType
from calendar_integration.models import AppointmentType, Calendar
from organizations.models import Organization
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService


def assert_graphql_success(response):
    """Helper function to assert GraphQL response is successful."""
    assert response.status_code == 200, (
        f"The status error {response.status_code} != 200\n"
        f"Response Content: {response.content.decode()}"
    )

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
        PublicAPIResources.CALENDAR_BUNDLE,
        PublicAPIResources.APPOINTMENT_TYPE,
    ]

    for resource in resources:
        baker.make(ResourceAccess, system_user=system_user, resource_name=resource)

    return system_user, token


@pytest.fixture
def graphql_client(system_user_with_resources):
    """Create an authenticated GraphQL client."""
    system_user, token = system_user_with_resources
    client = APIClient()

    # Set the authorization header with the system user ID and token
    auth_header = f"Bearer {system_user.id}:{token}"
    client.credentials(HTTP_AUTHORIZATION=auth_header)

    return client


@pytest.mark.django_db
@patch("public_api.extensions.OrganizationRateLimiter.on_execute")
class TestIsPrivateField:
    """Test the is_private field on Calendar, AppointmentType, and CalendarBundle."""

    def test_calendar_is_private_when_not_accepts_public_scheduling(
        self, mock_rate_limiter, graphql_client, organization
    ):
        """Test that is_private is true when accepts_public_scheduling is false."""
        mock_rate_limiter.return_value = iter([None])

        # Create a private calendar (default)
        calendar = baker.make(
            Calendar,
            organization=organization,
            name="Private Calendar",
            accepts_public_scheduling=False,
        )

        query = """
            query GetCalendars($calendarId: Int!) {
                calendars(calendarId: $calendarId) {
                    id
                    name
                    isPrivate
                }
            }
        """

        variables = {"calendarId": calendar.id}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        calendars = data["calendars"]
        assert len(calendars) == 1
        assert calendars[0]["isPrivate"] is True

    def test_calendar_is_private_false_when_accepts_public_scheduling(
        self, mock_rate_limiter, graphql_client, organization
    ):
        """Test that is_private is false when accepts_public_scheduling is true."""
        mock_rate_limiter.return_value = iter([None])

        # Create a public calendar
        calendar = baker.make(
            Calendar,
            organization=organization,
            name="Public Calendar",
            accepts_public_scheduling=True,
        )

        query = """
            query GetCalendars($calendarId: Int!) {
                calendars(calendarId: $calendarId) {
                    id
                    name
                    isPrivate
                }
            }
        """

        variables = {"calendarId": calendar.id}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        calendars = data["calendars"]
        assert len(calendars) == 1
        assert calendars[0]["isPrivate"] is False

    def test_appointment_type_is_private_when_not_accepts_public_scheduling(
        self, mock_rate_limiter, graphql_client, organization
    ):
        """Test that is_private is true on AppointmentType when accepts_public_scheduling is false."""
        mock_rate_limiter.return_value = iter([None])

        # Create a private appointment type (default)
        appointment_type = baker.make(
            AppointmentType,
            organization=organization,
            name="Private AppointmentType",
            accepts_public_scheduling=False,
        )

        query = """
            query GetAppointmentType($appointmentTypeId: Int!) {
                appointmentType(appointmentTypeId: $appointmentTypeId) {
                    id
                    name
                    isPrivate
                }
            }
        """

        variables = {"appointmentTypeId": appointment_type.id}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert data["appointmentType"]["isPrivate"] is True

    def test_appointment_type_is_private_false_when_accepts_public_scheduling(
        self, mock_rate_limiter, graphql_client, organization
    ):
        """Test that is_private is false on AppointmentType when accepts_public_scheduling is true."""
        mock_rate_limiter.return_value = iter([None])

        # Create a public appointment type
        appointment_type = baker.make(
            AppointmentType,
            organization=organization,
            name="Public AppointmentType",
            accepts_public_scheduling=True,
        )

        query = """
            query GetAppointmentType($appointmentTypeId: Int!) {
                appointmentType(appointmentTypeId: $appointmentTypeId) {
                    id
                    name
                    isPrivate
                }
            }
        """

        variables = {"appointmentTypeId": appointment_type.id}

        response = graphql_client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
        )

        data = assert_graphql_success(response)
        assert data["appointmentType"]["isPrivate"] is False

    def test_bundle_is_private_when_not_accepts_public_scheduling(
        self, mock_rate_limiter, graphql_client, organization
    ):
        """Test that is_private is true on CalendarBundle when accepts_public_scheduling is false."""
        mock_rate_limiter.return_value = iter([None])

        # Create a private bundle calendar (default)
        baker.make(
            Calendar,
            organization=organization,
            calendar_type=CalendarType.BUNDLE,
            name="Private Bundle",
            accepts_public_scheduling=False,
        )

        query = """
            query GetBundles($offset: Int, $limit: Int) {
                calendarBundles(offset: $offset, limit: $limit) {
                    id
                    name
                    isPrivate
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
        bundles = data["calendarBundles"]
        assert len(bundles) == 1
        assert bundles[0]["isPrivate"] is True

    def test_bundle_is_private_false_when_accepts_public_scheduling(
        self, mock_rate_limiter, graphql_client, organization
    ):
        """Test that is_private is false on CalendarBundle when accepts_public_scheduling is true."""
        mock_rate_limiter.return_value = iter([None])

        # Create a public bundle calendar
        baker.make(
            Calendar,
            organization=organization,
            calendar_type=CalendarType.BUNDLE,
            name="Public Bundle",
            accepts_public_scheduling=True,
        )

        query = """
            query GetBundles($offset: Int, $limit: Int) {
                calendarBundles(offset: $offset, limit: $limit) {
                    id
                    name
                    isPrivate
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
        bundles = data["calendarBundles"]
        assert len(bundles) == 1
        assert bundles[0]["isPrivate"] is False
