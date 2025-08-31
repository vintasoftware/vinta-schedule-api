import datetime
import json
import uuid
from unittest.mock import Mock

from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken
from model_bakery import baker
from rest_framework import status

from calendar_integration.constants import CalendarProvider, CalendarType, RecurrenceFrequency
from calendar_integration.models import (
    AvailableTime,
    BlockedTime,
    Calendar,
    CalendarEvent,
    CalendarOwnership,
    RecurrenceRule,
)
from calendar_integration.services.dataclasses import (
    AvailableTimeWindow,
    UnavailableTimeWindow,
)
from organizations.models import Organization, OrganizationMembership


User = get_user_model()


def assert_response_status_code(response, expected_status_code):
    assert response.status_code == expected_status_code, (
        f"The status error {response.status_code} != {expected_status_code}\n"
        f"Response Payload: {json.dumps(response.json())}"
    )


class CalendarIntegrationTestFactory:
    @staticmethod
    def create_organization(name="Test Organization"):
        return baker.make(Organization, name=name)

    @staticmethod
    def create_calendar(
        organization=None,
        name="Test Calendar",
        description="",
        email="test@calendar.com",
        external_id=None,
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=False,
    ):
        if organization is None:
            organization = CalendarIntegrationTestFactory.create_organization()

        if external_id is None:
            external_id = f"test_external_id_{uuid.uuid4().hex[:8]}"

        return baker.make(
            Calendar,
            organization=organization,
            name=name,
            description=description,
            email=email,
            external_id=external_id,
            provider=provider,
            calendar_type=calendar_type,
            manage_available_windows=manage_available_windows,
        )

    @staticmethod
    def create_calendar_event(
        calendar=None,
        title="Test Event",
        description="Test Description",
        start_time=None,
        end_time=None,
        external_id=None,
    ):
        if calendar is None:
            calendar = CalendarIntegrationTestFactory.create_calendar()

        if start_time is None:
            start_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        if end_time is None:
            end_time = start_time + datetime.timedelta(hours=1)

        if external_id is None:
            external_id = f"test_event_{uuid.uuid4().hex[:8]}"

        return baker.make(
            CalendarEvent,
            calendar=calendar,
            organization=calendar.organization,
            title=title,
            description=description,
            start_time=start_time,
            end_time=end_time,
            external_id=external_id,
        )

    @staticmethod
    def create_calendar_ownership(user, calendar, is_default=False):
        return baker.make(
            CalendarOwnership,
            user=user,
            calendar=calendar,
            organization=calendar.organization,
            is_default=is_default,
        )

    @staticmethod
    def create_recurrence_rule(
        organization,
        frequency=RecurrenceFrequency.WEEKLY,
        interval=1,
        count=None,
        until=None,
        by_weekday="",
    ):
        return baker.make(
            RecurrenceRule,
            organization=organization,
            frequency=frequency,
            interval=interval,
            count=count,
            until=until,
            by_weekday=by_weekday,
        )

    @staticmethod
    def create_recurring_event(
        calendar=None,
        title="Recurring Event",
        description="Recurring Description",
        start_time=None,
        end_time=None,
        external_id=None,
        recurrence_rule=None,
    ):
        if calendar is None:
            calendar = CalendarIntegrationTestFactory.create_calendar()

        if start_time is None:
            start_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        if end_time is None:
            end_time = start_time + datetime.timedelta(hours=1)

        if external_id is None:
            external_id = f"recurring_event_{uuid.uuid4().hex[:8]}"

        if recurrence_rule is None:
            recurrence_rule = CalendarIntegrationTestFactory.create_recurrence_rule(
                calendar.organization
            )

        return baker.make(
            CalendarEvent,
            calendar=calendar,
            organization=calendar.organization,
            title=title,
            description=description,
            start_time=start_time,
            end_time=end_time,
            external_id=external_id,
            recurrence_rule=recurrence_rule,
        )

    @staticmethod
    def create_blocked_time(
        calendar=None,
        reason="Test blocked time",
        start_time=None,
        end_time=None,
        external_id=None,
        recurrence_rule=None,
    ):
        if calendar is None:
            calendar = CalendarIntegrationTestFactory.create_calendar()

        if start_time is None:
            start_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        if end_time is None:
            end_time = start_time + datetime.timedelta(hours=1)

        if external_id is None:
            external_id = f"blocked_time_{uuid.uuid4().hex[:8]}"

        return baker.make(
            BlockedTime,
            calendar=calendar,
            organization=calendar.organization,
            reason=reason,
            start_time=start_time,
            end_time=end_time,
            external_id=external_id,
            recurrence_rule=recurrence_rule,
        )

    @staticmethod
    def create_available_time(
        calendar=None,
        start_time=None,
        end_time=None,
        recurrence_rule=None,
    ):
        if calendar is None:
            calendar = CalendarIntegrationTestFactory.create_calendar()

        if start_time is None:
            start_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        if end_time is None:
            end_time = start_time + datetime.timedelta(hours=1)

        return baker.make(
            AvailableTime,
            calendar=calendar,
            organization=calendar.organization,
            start_time=start_time,
            end_time=end_time,
            recurrence_rule=recurrence_rule,
        )

    @staticmethod
    def create_organization_membership(user, organization):
        return baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
        )


@pytest.fixture
def organization(user):
    organization = CalendarIntegrationTestFactory.create_organization()
    CalendarIntegrationTestFactory.create_organization_membership(user, organization)
    return organization


@pytest.fixture
def calendar(organization):
    return CalendarIntegrationTestFactory.create_calendar(organization=organization)


@pytest.fixture
def calendar_event(calendar):
    return CalendarIntegrationTestFactory.create_calendar_event(calendar=calendar)


@pytest.fixture
def calendar_ownership(user, calendar):
    return CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)


@pytest.fixture
def social_account(user, calendar):
    """Create a SocialAccount for the user matching the calendar provider"""

    account = baker.make(
        SocialAccount,
        user=user,
        provider=calendar.provider,
    )

    # Create a SocialToken for the account
    baker.make(
        SocialToken,
        account=account,
        token="fake_access_token",
        token_secret="fake_refresh_token",
        expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
    )

    return account


@pytest.mark.django_db
class TestCalendarEventViewSet:
    """Test suite for CalendarEventViewSet"""

    def test_list_calendar_events_authenticated(
        self, auth_client, calendar_event, social_account, user
    ):
        """Test listing calendar events as authenticated user"""
        # Create calendar ownership so user can access the event
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar_event.calendar)

        url = reverse("api:CalendarEvents-list")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        assert "results" in response.data
        assert len(response.data["results"]) == 1

    def test_list_calendar_events_unauthenticated(self, anonymous_client, calendar_event):
        """Test listing calendar events as unauthenticated user"""
        url = reverse("api:CalendarEvents-list")
        response = anonymous_client.get(url)

        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    def test_list_calendar_events_with_filters(self, auth_client, calendar, social_account, user):
        """Test listing calendar events with various filters"""
        # Create calendar ownership so user can access the events
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create events with different times and titles
        now = datetime.datetime.now(datetime.UTC)
        CalendarIntegrationTestFactory.create_calendar_event(
            calendar=calendar,
            title="Meeting with client",
            start_time=now + datetime.timedelta(hours=1),
            end_time=now + datetime.timedelta(hours=2),
            external_id=f"meeting_{uuid.uuid4().hex[:8]}",
        )
        CalendarIntegrationTestFactory.create_calendar_event(
            calendar=calendar,
            title="Team standup",
            start_time=now + datetime.timedelta(hours=3),
            end_time=now + datetime.timedelta(hours=4),
            external_id=f"standup_{uuid.uuid4().hex[:8]}",
        )

        url = reverse("api:CalendarEvents-list")

        # Test title filter
        response = auth_client.get(url, {"title": "meeting"})
        assert_response_status_code(response, status.HTTP_200_OK)
        assert len(response.data["results"]) == 1
        assert response.data["results"][0]["title"] == "Meeting with client"

        # Test time range filter
        start_filter = (now + datetime.timedelta(hours=2, minutes=30)).isoformat()
        response = auth_client.get(url, {"start_time": start_filter})
        assert_response_status_code(response, status.HTTP_200_OK)
        assert len(response.data["results"]) == 1
        assert response.data["results"][0]["title"] == "Team standup"

    def test_retrieve_calendar_event(self, auth_client, calendar_event, social_account, user):
        """Test retrieving a specific calendar event"""
        # Create calendar ownership so user can access the event
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar_event.calendar)

        url = reverse("api:CalendarEvents-detail", kwargs={"pk": calendar_event.id})
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        assert response.data["id"] == calendar_event.id
        assert response.data["title"] == calendar_event.title

    def test_retrieve_nonexistent_calendar_event(self, auth_client):
        """Test retrieving a non-existent calendar event"""
        url = reverse("api:CalendarEvents-detail", kwargs={"pk": 99999})
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_create_calendar_event(self, auth_client, calendar, user, social_account):
        """Test creating a calendar event"""
        from di_core.containers import container

        # Create a mock calendar service
        mock_calendar_service = Mock()
        mock_calendar_service.authenticate.return_value = None

        # Create a real CalendarEvent instance that will be saved to the database
        start_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        end_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)

        created_event = CalendarIntegrationTestFactory.create_calendar_event(
            calendar=calendar,
            title="New Event",
            description="Test Description",
            start_time=start_time,
            end_time=end_time,
            external_id="new_external_id",
        )

        mock_calendar_service.create_event.return_value = created_event

        # Create calendar ownership for the user
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar, is_default=True)

        url = reverse("api:CalendarEvents-list")
        data = {
            "organization": calendar.organization.id,
            "calendar": calendar.id,  # Add explicit calendar ID
            "title": "New Event",
            "description": "Test Description",
            "start_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
            ).isoformat(),
            "end_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)
            ).isoformat(),
            "resource_allocations": [],
            "attendances": [],
            "external_attendances": [],
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")
            if response.status_code != status.HTTP_201_CREATED:
                print(f"Response status: {response.status_code}")
                print(f"Response data: {response.data}")
            assert_response_status_code(response, status.HTTP_201_CREATED)
            assert response.data["title"] == "New Event"

        # Verify the mock was called
        mock_calendar_service.authenticate.assert_called_once()
        mock_calendar_service.create_event.assert_called_once()

    def test_create_calendar_event_validation_errors(self, auth_client, calendar):
        """Test creating calendar event with validation errors"""
        url = reverse("api:CalendarEvents-list")

        # Test missing required fields
        response = auth_client.post(url, {}, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

        # Test invalid time range (end before start)
        now = datetime.datetime.now(datetime.UTC)
        data = {
            "title": "Invalid Event",
            "start_time": (now + datetime.timedelta(hours=2)).isoformat(),
            "end_time": (now + datetime.timedelta(hours=1)).isoformat(),
            "organization": calendar.organization.id,
            "resource_allocations": [],
            "attendances": [],
            "external_attendances": [],
        }
        response = auth_client.post(url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_update_calendar_event(self, auth_client, calendar_event, user, social_account):
        """Test updating a calendar event"""
        from di_core.containers import container

        # Create a mock calendar service
        mock_calendar_service = Mock()
        mock_calendar_service.authenticate.return_value = None

        # Update the existing calendar event with new data
        calendar_event.title = "Updated Meeting"
        calendar_event.description = "Updated important meeting"
        calendar_event.save()

        mock_calendar_service.update_event.return_value = calendar_event

        # Create calendar ownership for the user
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar_event.calendar)

        url = reverse("api:CalendarEvents-detail", kwargs={"pk": calendar_event.id})
        updated_event_data = {
            "title": "Updated Meeting",
            "description": "Updated important meeting",
            "start_time": calendar_event.start_time.isoformat(),
            "end_time": calendar_event.end_time.isoformat(),
            "calendar": calendar_event.calendar.id,
            "resource_allocations": [],
            "attendances": [],
            "external_attendances": [],
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.put(url, updated_event_data, format="json")
            assert_response_status_code(response, status.HTTP_200_OK)
            assert response.data["title"] == "Updated Meeting"
            assert response.data["description"] == "Updated important meeting"

        # Verify the mock was called
        mock_calendar_service.authenticate.assert_called_once()
        mock_calendar_service.update_event.assert_called_once()

    def test_delete_calendar_event(self, auth_client, calendar_event, social_account, user):
        """Test deleting a calendar event"""
        from di_core.containers import container

        # Create calendar ownership so user can access the event
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar_event.calendar)

        # Create a mock calendar service
        mock_calendar_service = Mock()
        mock_calendar_service.authenticate.return_value = None
        mock_calendar_service.delete_event.return_value = None

        url = reverse("api:CalendarEvents-detail", kwargs={"pk": calendar_event.id})

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.delete(url)

            assert_response_status_code(response, status.HTTP_204_NO_CONTENT)

        # Verify the mock was called
        mock_calendar_service.authenticate.assert_called_once()
        mock_calendar_service.delete_event.assert_called_once()

    def test_delete_calendar_event_unauthenticated(self, anonymous_client, calendar_event):
        """Test deleting calendar event as unauthenticated user"""
        url = reverse("api:CalendarEvents-detail", kwargs={"pk": calendar_event.id})
        response = anonymous_client.delete(url)

        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)


@pytest.mark.django_db
class TestRecurringCalendarEventViewSet:
    """Test suite for recurring calendar events"""

    def test_create_recurring_event(self, auth_client, calendar, user, social_account):
        """Test creating a recurring calendar event"""
        from di_core.containers import container

        # Create a mock calendar service
        mock_calendar_service = Mock()
        mock_calendar_service.authenticate.return_value = None

        # Create a real recurring CalendarEvent instance that will be saved to the database
        start_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        end_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)

        created_event = CalendarIntegrationTestFactory.create_recurring_event(
            calendar=calendar,
            title="Weekly Meeting",
            description="Weekly team meeting",
            start_time=start_time,
            end_time=end_time,
            external_id="recurring_weekly_meeting",
        )

        mock_calendar_service.create_event.return_value = created_event

        # Create calendar ownership for the user
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar, is_default=True)

        url = reverse("api:CalendarEvents-list")
        data = {
            "organization": calendar.organization.id,
            "calendar": calendar.id,
            "title": "Weekly Meeting",
            "description": "Weekly team meeting",
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "resource_allocations": [],
            "attendances": [],
            "external_attendances": [],
            "rrule_string": "FREQ=WEEKLY;COUNT=10;BYDAY=MO",
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        assert response.data["title"] == "Weekly Meeting"
        assert response.data["is_recurring"] is True

        # Verify the mock was called
        mock_calendar_service.authenticate.assert_called_once()
        mock_calendar_service.create_event.assert_called_once()

    def test_create_recurring_event_with_recurrence_rule(
        self, auth_client, calendar, user, social_account
    ):
        """Test creating a recurring event with recurrence_rule"""
        from di_core.containers import container

        # Create a mock calendar service
        mock_calendar_service = Mock()
        mock_calendar_service.authenticate.return_value = None

        start_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        end_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)

        created_event = CalendarIntegrationTestFactory.create_recurring_event(
            calendar=calendar,
            title="Daily Standup",
            description="Daily team standup",
            start_time=start_time,
            end_time=end_time,
            external_id="daily_standup",
        )

        mock_calendar_service.create_event.return_value = created_event

        # Create calendar ownership for the user
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar, is_default=True)

        url = reverse("api:CalendarEvents-list")
        data = {
            "organization": calendar.organization.id,
            "calendar": calendar.id,
            "title": "Daily Standup",
            "description": "Daily team standup",
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "resource_allocations": [],
            "attendances": [],
            "external_attendances": [],
            "recurrence_rule": {
                "frequency": "DAILY",
                "interval": 1,
                "count": 30,
            },
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        assert response.data["title"] == "Daily Standup"

        # Verify the mock was called
        mock_calendar_service.authenticate.assert_called_once()
        mock_calendar_service.create_event.assert_called_once()

    def test_create_recurring_event_validation_errors(self, auth_client, calendar, user):
        """Test validation errors when creating recurring events"""
        url = reverse("api:CalendarEvents-list")

        # Test both rrule_string and recurrence_rule provided
        now = datetime.datetime.now(datetime.UTC)
        data = {
            "title": "Invalid Event",
            "start_time": (now + datetime.timedelta(hours=1)).isoformat(),
            "end_time": (now + datetime.timedelta(hours=2)).isoformat(),
            "organization": calendar.organization.id,
            "calendar": calendar.id,
            "resource_allocations": [],
            "attendances": [],
            "external_attendances": [],
            "rrule_string": "FREQ=WEEKLY;COUNT=10",
            "recurrence_rule": {"frequency": "DAILY", "interval": 1},
        }

        response = auth_client.post(url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "Cannot specify both recurrence_rule and rrule_string" in str(response.data)

    def _base_event_payload(self, calendar):
        now = datetime.datetime.now(datetime.UTC)
        return {
            "organization": calendar.organization.id,
            "calendar": calendar.id,
            "title": "Recurring Event",
            "description": "Test recurring",
            "start_time": (now + datetime.timedelta(hours=1)).isoformat(),
            "end_time": (now + datetime.timedelta(hours=2)).isoformat(),
            "resource_allocations": [],
            "attendances": [],
            "external_attendances": [],
        }

    def test_valid_recurrence_rule(self, auth_client, calendar, user, social_account):
        from di_core.containers import container

        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar, is_default=True)
        payload = self._base_event_payload(calendar)
        payload["recurrence_rule"] = {
            "frequency": "WEEKLY",
            "interval": 2,
            "by_weekday": "MO,WE,FR",
        }

        mock_calendar_service = Mock()
        mock_calendar_service.authenticate.return_value = None
        created_event = CalendarIntegrationTestFactory.create_recurring_event(calendar=calendar)
        mock_calendar_service.create_event.return_value = created_event

        url = reverse("api:CalendarEvents-list")
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, payload, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        assert response.data["is_recurring"] is True
        assert response.data["recurrence_rule"] is not None

    def test_invalid_weekday(self, auth_client, calendar, user, social_account):
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar, is_default=True)
        payload = self._base_event_payload(calendar)
        payload["recurrence_rule"] = {
            "frequency": "DAILY",
            "interval": 1,
            "by_weekday": "MO,XX",
        }
        url = reverse("api:CalendarEvents-list")
        response = auth_client.post(url, payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "Invalid weekdays" in str(response.data)

    def test_invalid_month_day(self, auth_client, calendar, user, social_account):
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar, is_default=True)
        payload = self._base_event_payload(calendar)
        payload["recurrence_rule"] = {
            "frequency": "MONTHLY",
            "interval": 1,
            "by_month_day": "1,32",
        }
        url = reverse("api:CalendarEvents-list")
        response = auth_client.post(url, payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "Invalid month days" in str(response.data)

    def test_invalid_month(self, auth_client, calendar, user, social_account):
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar, is_default=True)
        payload = self._base_event_payload(calendar)
        payload["recurrence_rule"] = {
            "frequency": "YEARLY",
            "interval": 1,
            "by_month": "12,13",
        }
        url = reverse("api:CalendarEvents-list")
        response = auth_client.post(url, payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "Invalid months" in str(response.data)

    def test_count_and_until_conflict(self, auth_client, calendar, user, social_account):
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar, is_default=True)
        payload = self._base_event_payload(calendar)
        payload["recurrence_rule"] = {
            "frequency": "DAILY",
            "interval": 1,
            "count": 5,
            "until": (datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=5)).isoformat(),
        }
        url = reverse("api:CalendarEvents-list")
        response = auth_client.post(url, payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        # Error is nested under recurrence_rule.non_field_errors as an ErrorDetail whose string
        # value itself contains a JSON-style list string. Inspect the first error directly.
        err = response.data["recurrence_rule"]["non_field_errors"][0]
        err_text = str(err)
        assert "Cannot specify both 'count' and 'until' in a recurrence rule." in err_text

    def test_interval_less_than_one(self, auth_client, calendar, user, social_account):
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar, is_default=True)
        payload = self._base_event_payload(calendar)
        payload["recurrence_rule"] = {
            "frequency": "DAILY",
            "interval": 0,
        }
        url = reverse("api:CalendarEvents-list")
        response = auth_client.post(url, payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "Interval must be at least 1" in str(response.data)

    def test_list_recurring_events_shows_recurrence_info(
        self, auth_client, calendar, user, social_account
    ):
        """Test that listing recurring events shows recurrence information"""
        # Create calendar ownership so user can access the events
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a recurring event
        CalendarIntegrationTestFactory.create_recurring_event(
            calendar=calendar,
            title="Weekly Meeting",
            description="Weekly team meeting",
        )

        url = reverse("api:CalendarEvents-list")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        assert len(response.data["results"]) == 1

        event_data = response.data["results"][0]
        assert event_data["title"] == "Weekly Meeting"
        assert event_data["is_recurring"] is True
        assert event_data["recurrence_rule"] is not None
        assert "rrule_string" in event_data["recurrence_rule"]

    def test_retrieve_recurring_event(self, auth_client, calendar, user, social_account):
        """Test retrieving a specific recurring event"""
        # Create calendar ownership so user can access the event
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a recurring event
        recurring_event = CalendarIntegrationTestFactory.create_recurring_event(
            calendar=calendar,
            title="Monthly Review",
            description="Monthly team review",
        )

        url = reverse("api:CalendarEvents-detail", kwargs={"pk": recurring_event.id})
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        assert response.data["id"] == recurring_event.id
        assert response.data["title"] == "Monthly Review"
        assert response.data["is_recurring"] is True
        assert response.data["recurrence_rule"] is not None

    def test_create_recurring_event_exception_cancelled(
        self, auth_client, calendar, user, social_account
    ):
        """Test creating a cancelled exception for a recurring event"""
        from di_core.containers import container

        # Create a recurring event
        start_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        end_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)

        recurring_event = CalendarIntegrationTestFactory.create_recurring_event(
            calendar=calendar,
            title="Weekly Meeting",
            description="Weekly team meeting",
            start_time=start_time,
            end_time=end_time,
            external_id="recurring_weekly_meeting",
        )

        # Create calendar ownership for the user
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a mock calendar service
        mock_calendar_service = Mock()
        mock_calendar_service.authenticate.return_value = None
        mock_calendar_service.create_recurring_event_exception.return_value = (
            None  # Cancelled event
        )

        url = reverse("api:CalendarEvents-create-exception", kwargs={"pk": recurring_event.id})
        exception_date = (start_time + datetime.timedelta(days=7)).date()
        data = {
            "exception_date": exception_date.isoformat(),
            "is_cancelled": True,
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_204_NO_CONTENT)

        # Verify the mock was called with correct parameters
        mock_calendar_service.create_recurring_event_exception.assert_called_once()
        call_args = mock_calendar_service.create_recurring_event_exception.call_args
        assert call_args[1]["parent_event"] == recurring_event
        assert call_args[1]["is_cancelled"] is True

    def test_create_recurring_event_exception_modified(
        self, auth_client, calendar, user, social_account
    ):
        """Test creating a modified exception for a recurring event"""
        from di_core.containers import container

        # Create a recurring event
        start_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        end_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)

        recurring_event = CalendarIntegrationTestFactory.create_recurring_event(
            calendar=calendar,
            title="Weekly Meeting",
            description="Weekly team meeting",
            start_time=start_time,
            end_time=end_time,
            external_id="recurring_weekly_meeting",
        )

        # Create calendar ownership for the user
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a mock modified event
        modified_start_time = start_time + datetime.timedelta(days=7, hours=1)
        modified_end_time = end_time + datetime.timedelta(days=7, hours=1)

        modified_event = CalendarIntegrationTestFactory.create_calendar_event(
            calendar=calendar,
            title="Modified Weekly Meeting",
            description="Modified weekly team meeting",
            start_time=modified_start_time,
            end_time=modified_end_time,
            external_id="modified_weekly_meeting",
        )

        # Create a mock calendar service
        mock_calendar_service = Mock()
        mock_calendar_service.authenticate.return_value = None
        mock_calendar_service.create_recurring_event_exception.return_value = modified_event

        url = reverse("api:CalendarEvents-create-exception", kwargs={"pk": recurring_event.id})
        exception_date = (start_time + datetime.timedelta(days=7)).date()
        data = {
            "exception_date": exception_date.isoformat(),
            "modified_title": "Modified Weekly Meeting",
            "modified_description": "Modified weekly team meeting",
            "modified_start_time": modified_start_time.isoformat(),
            "modified_end_time": modified_end_time.isoformat(),
            "is_cancelled": False,
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        assert response.data["title"] == "Modified Weekly Meeting"
        assert response.data["description"] == "Modified weekly team meeting"

        # Verify the mock was called with correct parameters
        mock_calendar_service.create_recurring_event_exception.assert_called_once()
        call_args = mock_calendar_service.create_recurring_event_exception.call_args
        assert call_args[1]["parent_event"] == recurring_event
        assert call_args[1]["is_cancelled"] is False
        assert call_args[1]["modified_title"] == "Modified Weekly Meeting"

    def test_create_recurring_event_exception_non_recurring_event(
        self, auth_client, calendar, user
    ):
        """Test creating an exception for a non-recurring event should fail"""
        # Create a non-recurring event
        start_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        end_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)

        non_recurring_event = CalendarIntegrationTestFactory.create_calendar_event(
            calendar=calendar,
            title="Single Meeting",
            start_time=start_time,
            end_time=end_time,
            external_id="single_meeting",
        )

        # Create calendar ownership for the user
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        url = reverse("api:CalendarEvents-create-exception", kwargs={"pk": non_recurring_event.id})
        exception_date = (start_time + datetime.timedelta(days=7)).date()
        data = {
            "exception_date": exception_date.isoformat(),
            "is_cancelled": True,
        }

        response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "not a recurring event" in str(response.data)

    def test_create_recurring_event_exception_validation_errors(self, auth_client, calendar, user):
        """Test validation errors when creating recurring exceptions"""
        # Create a recurring event
        start_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        end_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)

        recurring_event = CalendarIntegrationTestFactory.create_recurring_event(
            calendar=calendar,
            title="Weekly Meeting",
            start_time=start_time,
            end_time=end_time,
            external_id="recurring_weekly_meeting",
        )

        # Create calendar ownership for the user
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        url = reverse("api:CalendarEvents-create-exception", kwargs={"pk": recurring_event.id})

        # Test missing required fields
        response = auth_client.post(url, {}, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

        # Test non-cancelled exception without modifications
        exception_date = (start_time + datetime.timedelta(days=7)).date()
        data = {
            "exception_date": exception_date.isoformat(),
            "is_cancelled": False,
        }
        response = auth_client.post(url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "modification field must be provided" in str(response.data)

        # Test invalid datetime range
        data = {
            "exception_date": exception_date.isoformat(),
            "modified_start_time": (start_time + datetime.timedelta(hours=2)).isoformat(),
            "modified_end_time": (start_time + datetime.timedelta(hours=1)).isoformat(),
            "is_cancelled": False,
        }
        response = auth_client.post(url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "must be before" in str(response.data)


@pytest.mark.django_db
class TestCalendarViewSet:
    """Test suite for CalendarViewSet"""

    def test_get_available_windows(self, auth_client, calendar, user):
        """Test getting available time windows for a calendar"""
        from di_core.containers import container

        # Create calendar ownership for the user
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a mock calendar service
        mock_calendar_service = Mock()
        mock_calendar_service.authenticate.return_value = None

        now = datetime.datetime.now(datetime.UTC)
        mock_calendar_service.get_availability_windows_in_range.return_value = [
            AvailableTimeWindow(
                id=1,
                start_time=now + datetime.timedelta(hours=1),
                end_time=now + datetime.timedelta(hours=2),
                can_book_partially=True,
            ),
            AvailableTimeWindow(
                id=2,
                start_time=now + datetime.timedelta(hours=3),
                end_time=now + datetime.timedelta(hours=4),
                can_book_partially=False,
            ),
        ]

        url = reverse("api:Calendars-available-windows", kwargs={"pk": calendar.id})
        params = {
            "start_datetime": (now + datetime.timedelta(hours=1)).isoformat(),
            "end_datetime": (now + datetime.timedelta(hours=5)).isoformat(),
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.get(url, params)
            assert_response_status_code(response, status.HTTP_200_OK)
            assert len(response.data) == 2
            assert response.data[0]["can_book_partially"] is True
            assert response.data[1]["can_book_partially"] is False

        # Verify the mock was called
        mock_calendar_service.get_availability_windows_in_range.assert_called_once()

    def test_get_available_windows_missing_params(self, auth_client, calendar):
        """Test getting available windows without required parameters"""
        url = reverse("api:Calendars-available-windows", kwargs={"pk": calendar.id})

        # Missing both parameters
        response = auth_client.get(url)
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "start_datetime and end_datetime are required" in str(response.data)

        # Missing end_datetime
        response = auth_client.get(url, {"start_datetime": "2024-01-01T00:00:00Z"})
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_get_available_windows_invalid_datetime(self, auth_client, calendar):
        """Test getting available windows with invalid datetime format"""
        url = reverse("api:Calendars-available-windows", kwargs={"pk": calendar.id})
        params = {
            "start_datetime": "invalid-datetime",
            "end_datetime": "2024-01-01T00:00:00Z",
        }

        response = auth_client.get(url, params)
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "Invalid datetime format" in str(response.data)

    def test_get_unavailable_windows(self, auth_client, calendar, user):
        """Test getting unavailable time windows for a calendar"""
        from di_core.containers import container

        # Create calendar ownership for the user
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a mock calendar service
        mock_calendar_service = Mock()
        mock_calendar_service.authenticate.return_value = None

        now = datetime.datetime.now(datetime.UTC)
        mock_calendar_service.get_unavailable_time_windows_in_range.return_value = [
            UnavailableTimeWindow(
                id=1,
                start_time=now + datetime.timedelta(hours=1),
                end_time=now + datetime.timedelta(hours=2),
                reason="calendar_event",
                data=Mock(title="Meeting"),
            ),
            UnavailableTimeWindow(
                id=2,
                start_time=now + datetime.timedelta(hours=3),
                end_time=now + datetime.timedelta(hours=4),
                reason="blocked_time",
                data=Mock(reason="Lunch break"),
            ),
        ]

        url = reverse("api:Calendars-unavailable-windows", kwargs={"pk": calendar.id})
        params = {
            "start_datetime": (now + datetime.timedelta(hours=1)).isoformat(),
            "end_datetime": (now + datetime.timedelta(hours=5)).isoformat(),
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.get(url, params)
            assert_response_status_code(response, status.HTTP_200_OK)
            assert len(response.data) == 2
            assert response.data[0]["reason"] == "calendar_event"
            assert response.data[1]["reason"] == "blocked_time"

        # Verify the mock was called
        mock_calendar_service.get_unavailable_time_windows_in_range.assert_called_once()

    def test_get_unavailable_windows_unauthenticated(self, anonymous_client, calendar):
        """Test getting unavailable windows as unauthenticated user"""
        url = reverse("api:Calendars-unavailable-windows", kwargs={"pk": calendar.id})
        params = {
            "start_datetime": "2024-01-01T00:00:00Z",
            "end_datetime": "2024-01-01T23:59:59Z",
        }

        response = anonymous_client.get(url, params)
        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    def test_service_error_handling(self, auth_client, calendar, user):
        """Test error handling when calendar service raises exceptions"""
        from di_core.containers import container

        # Create calendar ownership for the user
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a mock calendar service that raises exception
        mock_calendar_service = Mock()
        mock_calendar_service.authenticate.side_effect = ValueError("Authentication failed")

        url = reverse("api:Calendars-available-windows", kwargs={"pk": calendar.id})
        params = {
            "start_datetime": "2024-01-01T00:00:00Z",
            "end_datetime": "2024-01-01T23:59:59Z",
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.get(url, params)
            assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
            assert "Authentication failed" in str(response.data)
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "Authentication failed" in str(response.data)

    def test_nonexistent_calendar(self, auth_client, user):
        """Test accessing availability for non-existent calendar"""
        # Create calendar organization membership for the user
        calendar_org = CalendarIntegrationTestFactory.create_organization()
        CalendarIntegrationTestFactory.create_organization_membership(user, calendar_org)

        url = reverse("api:Calendars-available-windows", kwargs={"pk": 99999})
        params = {
            "start_datetime": "2024-01-01T00:00:00Z",
            "end_datetime": "2024-01-01T23:59:59Z",
        }

        response = auth_client.get(url, params)
        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_list_calendars_authenticated(self, auth_client, calendar, user):
        """Test listing calendars as authenticated user"""
        # Create calendar ownership so user can access the calendar
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        url = reverse("api:Calendars-list")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        assert "results" in response.data
        assert len(response.data["results"]) == 1
        assert response.data["results"][0]["id"] == calendar.id

    def test_list_calendars_unauthenticated(self, anonymous_client, calendar):
        """Test listing calendars as unauthenticated user"""
        url = reverse("api:Calendars-list")
        response = anonymous_client.get(url)

        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    def test_retrieve_calendar(self, auth_client, calendar, user):
        """Test retrieving a specific calendar"""
        # Create calendar ownership so user can access the calendar
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        url = reverse("api:Calendars-detail", kwargs={"pk": calendar.id})
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        assert response.data["id"] == calendar.id
        assert response.data["name"] == calendar.name

    def test_retrieve_nonexistent_calendar(self, auth_client, user, organization):
        """Test retrieving a non-existent calendar"""
        url = reverse("api:Calendars-detail", kwargs={"pk": 99999})
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_create_virtual_calendar(self, auth_client, organization, user):
        """Test creating a virtual calendar"""
        from di_core.containers import container

        # Create a mock calendar service
        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None

        # Create a real Calendar instance that will be saved to the database
        created_calendar = CalendarIntegrationTestFactory.create_calendar(
            organization=organization,
            name="New Virtual Calendar",
            description="Test virtual calendar",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.PERSONAL,
        )

        mock_calendar_service.create_virtual_calendar.return_value = created_calendar

        url = reverse("api:Calendars-list")
        data = {
            "name": "New Virtual Calendar",
            "description": "Test virtual calendar",
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        assert response.data["name"] == "New Virtual Calendar"
        assert response.data["description"] == "Test virtual calendar"

        # Verify the mock was called
        mock_calendar_service.initialize_without_provider.assert_called_once()
        mock_calendar_service.create_virtual_calendar.assert_called_once()

    def test_create_calendar_validation_errors(self, auth_client):
        """Test creating calendar with validation errors"""
        url = reverse("api:Calendars-list")

        # Test missing required fields
        response = auth_client.post(url, {}, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

        response = auth_client.post(url, {"bundle_children": []}, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

        response = auth_client.post(url, {"primary_calendar": None}, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_update_calendar(self, auth_client, calendar, user):
        """Test updating a calendar"""
        # Create calendar ownership so user can access the calendar
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        url = reverse("api:Calendars-detail", kwargs={"pk": calendar.id})
        updated_data = {
            "name": "Updated Calendar Name",
            "description": "Updated description",
        }

        response = auth_client.patch(url, updated_data, format="json")

        assert_response_status_code(response, status.HTTP_200_OK)
        assert response.data["name"] == "Updated Calendar Name"
        assert response.data["description"] == "Updated description"

    def test_delete_calendar(self, auth_client, calendar, user):
        """Test deleting a calendar"""
        # Create calendar ownership so user can access the calendar
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        url = reverse("api:Calendars-detail", kwargs={"pk": calendar.id})
        response = auth_client.delete(url)

        assert_response_status_code(response, status.HTTP_204_NO_CONTENT)

    def test_create_bundle_calendar(self, auth_client, organization, user):
        """Test creating a bundle calendar"""
        from di_core.containers import container

        # Create mock calendar service
        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None

        # Create child calendars for the bundle
        child_calendar_1 = CalendarIntegrationTestFactory.create_calendar(
            organization=organization,
            name="Child Calendar 1",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.PERSONAL,
        )
        child_calendar_2 = CalendarIntegrationTestFactory.create_calendar(
            organization=organization,
            name="Child Calendar 2",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.PERSONAL,
        )

        # Create the bundle calendar that will be returned
        bundle_calendar = CalendarIntegrationTestFactory.create_calendar(
            organization=organization,
            name="Bundle Calendar",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.BUNDLE,
        )

        mock_calendar_service.create_bundle_calendar.return_value = bundle_calendar

        url = reverse("api:Calendars-bundle")
        data = {
            "name": "Bundle Calendar",
            "bundle_calendars": [child_calendar_1.id, child_calendar_2.id],
            "primary_calendar": child_calendar_2.id,
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        assert response.data["name"] == "Bundle Calendar"
        assert response.data["calendar_type"] == CalendarType.BUNDLE

        # Verify the mock was called
        mock_calendar_service.initialize_without_provider.assert_called_once()
        mock_calendar_service.create_bundle_calendar.assert_called_once()

    def test_create_bundle_calendar_validation_errors(self, auth_client, organization, user):
        """Test creating bundle calendar with validation errors"""
        url = reverse("api:Calendars-bundle")

        # Test missing required fields
        response = auth_client.post(url, {}, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

        # Test with only one calendar (should require at least 2)
        child_calendar = CalendarIntegrationTestFactory.create_calendar(
            organization=organization,
            provider=CalendarProvider.INTERNAL,
        )

        data = {
            "name": "Invalid Bundle",
            "bundle_calendars": [child_calendar.id],
            "primary_calendar": child_calendar.id,
        }

        response = auth_client.post(url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_create_bundle_calendar_with_integration_calendars(
        self, auth_client, organization, user
    ):
        """Test creating bundle calendar with integration calendars requires integration primary"""
        # Create child calendars - one internal, one integration
        internal_calendar = CalendarIntegrationTestFactory.create_calendar(
            organization=organization,
            provider=CalendarProvider.INTERNAL,
        )
        integration_calendar = CalendarIntegrationTestFactory.create_calendar(
            organization=organization,
            provider=CalendarProvider.GOOGLE,
        )

        url = reverse("api:Calendars-bundle")

        # Test with internal primary calendar when bundle has integration calendars (should fail)
        data = {
            "name": "Invalid Bundle",
            "bundle_calendars": [internal_calendar.id, integration_calendar.id],
            "primary_calendar": internal_calendar.id,
        }

        response = auth_client.post(url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "Primary calendar needs to be an integration calendar" in str(response.data)

    def test_create_bundle_calendar_unauthenticated(self, anonymous_client):
        """Test creating bundle calendar as unauthenticated user"""
        url = reverse("api:Calendars-bundle")
        data = {
            "name": "Bundle Calendar",
            "bundle_calendars": [1, 2],
            "primary_calendar": 2,
        }

        response = anonymous_client.post(url, data, format="json")
        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)


@pytest.mark.django_db
class TestCalendarIntegrationPermissions:
    """Test suite for calendar integration permissions"""

    def test_calendar_event_permission_authenticated(self, auth_client, calendar_event, user):
        """Test that authenticated users can access calendar events"""
        # Create calendar ownership so user can access the event
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar_event.calendar)

        url = reverse("api:CalendarEvents-list")
        response = auth_client.get(url)
        assert_response_status_code(response, status.HTTP_200_OK)
        assert_response_status_code(response, status.HTTP_200_OK)

    def test_calendar_event_permission_unauthenticated(self, anonymous_client, calendar_event):
        """Test that unauthenticated users cannot access calendar events"""
        url = reverse("api:CalendarEvents-list")
        response = anonymous_client.get(url)
        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    def test_calendar_availability_permission_authenticated(self, auth_client, calendar, user):
        """Test that authenticated users can check calendar availability"""
        from di_core.containers import container

        # User already has calendar organization membership from fixture
        # Create social account for the user
        account = baker.make(
            SocialAccount,
            user=user,
            provider=calendar.provider,
        )

        # Create a SocialToken for the account
        baker.make(
            SocialToken,
            account=account,
            token="fake_access_token",
            token_secret="fake_refresh_token",
            expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
        )

        # Mock the calendar service to avoid Google API calls
        mock_calendar_service = Mock()
        mock_calendar_service.authenticate.return_value = None
        mock_calendar_service.get_availability_windows_in_range.return_value = [
            AvailableTimeWindow(
                start_time=datetime.datetime(2024, 1, 1, 9, 0, tzinfo=datetime.UTC),
                end_time=datetime.datetime(2024, 1, 1, 17, 0, tzinfo=datetime.UTC),
            ),
        ]

        url = reverse("api:Calendars-available-windows", kwargs={"pk": calendar.id})
        params = {
            "start_datetime": "2024-01-01T00:00:00Z",
            "end_datetime": "2024-01-01T23:59:59Z",
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.get(url, params)
            # Should not be 401 (permission denied) - should return 200 with mocked data
            assert_response_status_code(response, status.HTTP_200_OK)

        # Verify the service methods were called
        mock_calendar_service.authenticate.assert_called_once()
        mock_calendar_service.get_availability_windows_in_range.assert_called_once()

    def test_calendar_availability_permission_unauthenticated(self, anonymous_client, calendar):
        """Test that unauthenticated users cannot check calendar availability"""
        url = reverse("api:Calendars-available-windows", kwargs={"pk": calendar.id})
        params = {
            "start_datetime": "2024-01-01T00:00:00Z",
            "end_datetime": "2024-01-01T23:59:59Z",
        }
        response = anonymous_client.get(url, params)
        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)


@pytest.mark.django_db
class TestCalendarEventFilters:
    """Test suite for calendar event filtering"""

    def test_start_time_filter(self, auth_client, calendar, social_account, user):
        """Test filtering events by start time"""
        # Create calendar ownership so user can access the events
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        now = datetime.datetime.now(datetime.UTC)

        # Create events at different times
        CalendarIntegrationTestFactory.create_calendar_event(
            calendar=calendar,
            title="Early Event",
            start_time=now + datetime.timedelta(hours=1),
            end_time=now + datetime.timedelta(hours=2),
            external_id=f"early_{uuid.uuid4().hex[:8]}",
        )
        CalendarIntegrationTestFactory.create_calendar_event(
            calendar=calendar,
            title="Late Event",
            start_time=now + datetime.timedelta(hours=3),
            end_time=now + datetime.timedelta(hours=4),
            external_id=f"late_{uuid.uuid4().hex[:8]}",
        )

        url = reverse("api:CalendarEvents-list")

        # Filter for events starting after 2 hours from now
        filter_time = (now + datetime.timedelta(hours=2, minutes=30)).isoformat()
        response = auth_client.get(url, {"start_time": filter_time})

        assert_response_status_code(response, status.HTTP_200_OK)
        assert len(response.data["results"]) == 1
        assert response.data["results"][0]["title"] == "Late Event"

    def test_end_time_filter(self, auth_client, calendar, social_account, user):
        """Test filtering events by end time"""
        # Create calendar ownership so user can access the events
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        now = datetime.datetime.now(datetime.UTC)

        # Create events with different end times
        CalendarIntegrationTestFactory.create_calendar_event(
            calendar=calendar,
            title="Short Event",
            start_time=now + datetime.timedelta(hours=1),
            end_time=now + datetime.timedelta(hours=2),
            external_id=f"short_{uuid.uuid4().hex[:8]}",
        )
        CalendarIntegrationTestFactory.create_calendar_event(
            calendar=calendar,
            title="Long Event",
            start_time=now + datetime.timedelta(hours=1),
            end_time=now + datetime.timedelta(hours=4),
            external_id=f"long_{uuid.uuid4().hex[:8]}",
        )

        url = reverse("api:CalendarEvents-list")

        # Filter for events ending before 3 hours from now
        filter_time = (now + datetime.timedelta(hours=3)).isoformat()
        response = auth_client.get(url, {"end_time": filter_time})

        assert_response_status_code(response, status.HTTP_200_OK)
        assert len(response.data["results"]) == 1
        assert response.data["results"][0]["title"] == "Short Event"

    def test_calendar_filter(self, auth_client, organization, user):
        """Test filtering events by calendar"""
        calendar1 = CalendarIntegrationTestFactory.create_calendar(
            organization=organization,
            name="Calendar 1",
            external_id="calendar_1_external_id",
        )
        calendar2 = CalendarIntegrationTestFactory.create_calendar(
            organization=organization,
            name="Calendar 2",
            external_id="calendar_2_external_id",
        )

        # Create social account for user with first calendar's provider
        baker.make(
            SocialAccount,
            user=user,
            provider=calendar1.provider,
        )

        # Create calendar ownership for both calendars
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar1)
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar2)

        CalendarIntegrationTestFactory.create_calendar_event(
            calendar=calendar1,
            title="Event in Calendar 1",
            external_id=f"cal1_{uuid.uuid4().hex[:8]}",
        )
        CalendarIntegrationTestFactory.create_calendar_event(
            calendar=calendar2,
            title="Event in Calendar 2",
            external_id=f"cal2_{uuid.uuid4().hex[:8]}",
        )

        url = reverse("api:CalendarEvents-list")

        # Filter by calendar1
        response = auth_client.get(url, {"calendar": calendar1.id})
        assert_response_status_code(response, status.HTTP_200_OK)
        assert len(response.data["results"]) == 1
        assert response.data["results"][0]["title"] == "Event in Calendar 1"

        # Filter by calendar2
        response = auth_client.get(url, {"calendar": calendar2.id})
        assert_response_status_code(response, status.HTTP_200_OK)
        assert len(response.data["results"]) == 1
        assert response.data["results"][0]["title"] == "Event in Calendar 2"


@pytest.mark.django_db
class TestBlockedTimeViewSet:
    """Test suite for BlockedTimeViewSet"""

    def test_list_blocked_times_authenticated(self, auth_client, calendar, user):
        """Test listing blocked times as authenticated user"""
        # Create calendar ownership so user can access the blocked times
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create blocked time
        CalendarIntegrationTestFactory.create_blocked_time(
            calendar=calendar,
            reason="Lunch break",
        )

        url = reverse("api:BlockedTimes-list")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        assert "results" in response.data
        assert len(response.data["results"]) == 1
        assert response.data["results"][0]["reason"] == "Lunch break"

    def test_list_blocked_times_unauthenticated(self, anonymous_client):
        """Test listing blocked times as unauthenticated user"""
        url = reverse("api:BlockedTimes-list")
        response = anonymous_client.get(url)

        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    def test_create_blocked_time(self, auth_client, calendar, user):
        """Test creating a blocked time"""
        from di_core.containers import container

        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a mock calendar service
        mock_calendar_service = Mock()

        created_blocked_time = CalendarIntegrationTestFactory.create_blocked_time(
            calendar=calendar,
            reason="Meeting preparation",
        )

        mock_calendar_service.create_blocked_time.return_value = created_blocked_time

        url = reverse("api:BlockedTimes-list")
        data = {
            "calendar": calendar.id,
            "reason": "Meeting preparation",
            "start_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
            ).isoformat(),
            "end_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)
            ).isoformat(),
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        assert response.data["reason"] == "Meeting preparation"

        # Verify the mock was called
        mock_calendar_service.create_blocked_time.assert_called_once()

    def test_create_recurring_blocked_time(self, auth_client, calendar, user):
        """Test creating a recurring blocked time"""
        from di_core.containers import container

        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a mock calendar service
        mock_calendar_service = Mock()

        created_blocked_time = CalendarIntegrationTestFactory.create_blocked_time(
            calendar=calendar,
            reason="Weekly team sync",
        )

        mock_calendar_service.create_blocked_time.return_value = created_blocked_time

        url = reverse("api:BlockedTimes-list")
        data = {
            "calendar": calendar.id,  # Fixed: use calendar_id instead of calendar
            "reason": "Weekly team sync",
            "start_time": (  # Fixed: use start_time (not start_datetime)
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
            ).isoformat(),
            "end_time": (  # Fixed: use end_time (not end_datetime)
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)
            ).isoformat(),
            "rrule_string": "FREQ=WEEKLY;COUNT=10;BYDAY=MO",  # Fixed: use recurrence_rule instead of rrule
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        assert response.data["reason"] == "Weekly team sync"

        # Verify the mock was called
        mock_calendar_service.create_blocked_time.assert_called_once()

    def test_bulk_create_blocked_times(self, auth_client, calendar, user):
        """Test bulk creating blocked times"""
        from di_core.containers import container

        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a mock calendar service
        mock_calendar_service = Mock()

        created_blocked_times = [
            CalendarIntegrationTestFactory.create_blocked_time(
                calendar=calendar,
                reason="Lunch break",
            ),
            CalendarIntegrationTestFactory.create_blocked_time(
                calendar=calendar,
                reason="Break time",
            ),
        ]

        mock_calendar_service.bulk_create_manual_blocked_times.return_value = created_blocked_times

        url = reverse("api:BlockedTimes-bulk-create")
        data = {
            "blocked_times": [
                {
                    "calendar": calendar.id,
                    "reason": "Lunch break",
                    "start_time": (
                        datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
                    ).isoformat(),
                    "end_time": (
                        datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)
                    ).isoformat(),
                },
                {
                    "calendar": calendar.id,
                    "reason": "Break time",
                    "start_time": (
                        datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=3)
                    ).isoformat(),
                    "end_time": (
                        datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=4)
                    ).isoformat(),
                },
            ],
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        assert len(response.data) == 2

        # Verify the mock was called
        mock_calendar_service.bulk_create_manual_blocked_times.assert_called_once()

    def test_get_blocked_times_expanded(self, auth_client, calendar, user):
        """Test getting expanded blocked times (including recurring occurrences)"""
        from di_core.containers import container

        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a mock calendar service
        mock_calendar_service = Mock()

        blocked_times = [
            CalendarIntegrationTestFactory.create_blocked_time(
                calendar=calendar,
                reason="Daily standup",
            ),
            CalendarIntegrationTestFactory.create_blocked_time(
                calendar=calendar,
                reason="Another meeting",
            ),
        ]

        mock_calendar_service.get_blocked_times_expanded.return_value = blocked_times

        url = reverse("api:BlockedTimes-expanded")
        params = {
            "calendar_id": calendar.id,
            "start_time": "2024-01-01T00:00:00Z",  # Fixed: use start_time (not start_datetime)
            "end_time": "2024-01-31T23:59:59Z",  # Fixed: use end_time (not end_datetime)
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.get(url, params)

        assert_response_status_code(response, status.HTTP_200_OK)
        assert len(response.data) == 2

        # Verify the mock was called
        mock_calendar_service.get_blocked_times_expanded.assert_called_once()

    def test_create_blocked_time_validation_errors(self, auth_client, calendar):
        """Test creating blocked time with validation errors"""
        url = reverse("api:BlockedTimes-list")

        # Test missing required fields
        response = auth_client.post(url, {}, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

        # Test invalid time range (end before start)
        now = datetime.datetime.now(datetime.UTC)
        data = {
            "calendar": calendar.id,
            "reason": "Invalid time range",
            "start_time": (now + datetime.timedelta(hours=2)).isoformat(),
            "end_time": (now + datetime.timedelta(hours=1)).isoformat(),
        }
        response = auth_client.post(url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_retrieve_blocked_time(self, auth_client, calendar, user):
        """Test retrieving a specific blocked time"""
        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create blocked time
        blocked_time = CalendarIntegrationTestFactory.create_blocked_time(
            calendar=calendar,
            reason="Important meeting",
        )

        url = reverse("api:BlockedTimes-detail", kwargs={"pk": blocked_time.id})
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        assert response.data["id"] == blocked_time.id
        assert response.data["reason"] == "Important meeting"

    def test_update_blocked_time(self, auth_client, calendar, user):
        """Test updating a blocked time"""
        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create blocked time
        blocked_time = CalendarIntegrationTestFactory.create_blocked_time(
            calendar=calendar,
            reason="Original reason",
        )

        url = reverse("api:BlockedTimes-detail", kwargs={"pk": blocked_time.id})
        updated_data = {
            "reason": "Updated reason",
            "start_time": blocked_time.start_time.isoformat(),
            "end_time": blocked_time.end_time.isoformat(),
            "calendar": calendar.id,
        }

        response = auth_client.put(url, updated_data, format="json")

        assert_response_status_code(response, status.HTTP_200_OK)
        assert response.data["reason"] == "Updated reason"

    def test_delete_blocked_time(self, auth_client, calendar, user):
        """Test deleting a blocked time"""
        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create blocked time
        blocked_time = CalendarIntegrationTestFactory.create_blocked_time(
            calendar=calendar,
            reason="To be deleted",
        )

        url = reverse("api:BlockedTimes-detail", kwargs={"pk": blocked_time.id})
        response = auth_client.delete(url)

        assert_response_status_code(response, status.HTTP_204_NO_CONTENT)

    def test_create_blocked_time_exception_cancelled(self, auth_client, calendar, user):
        """Test creating a cancelled exception for a recurring blocked time"""
        from di_core.containers import container

        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a recurring blocked time
        recurrence_rule = CalendarIntegrationTestFactory.create_recurrence_rule(
            organization=user.organization_membership.organization,
            frequency=RecurrenceFrequency.WEEKLY,
        )

        blocked_time = CalendarIntegrationTestFactory.create_blocked_time(
            calendar=calendar,
            reason="Weekly team meeting",
            recurrence_rule=recurrence_rule,
        )

        # Create a mock calendar service
        mock_calendar_service = Mock()
        mock_calendar_service.create_recurring_blocked_time_exception.return_value = (
            None  # Cancelled exception
        )

        url = reverse("api:BlockedTimes-create-exception", kwargs={"pk": blocked_time.id})
        data = {
            "exception_date": "2024-02-15",  # A specific date to cancel
            "is_cancelled": True,
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_204_NO_CONTENT)

        # Verify the mock was called with correct parameters
        mock_calendar_service.create_recurring_blocked_time_exception.assert_called_once()
        call_args = mock_calendar_service.create_recurring_blocked_time_exception.call_args
        assert call_args[1]["parent_blocked_time"] == blocked_time
        assert call_args[1]["is_cancelled"] is True

    def test_create_blocked_time_exception_modified(self, auth_client, calendar, user):
        """Test creating a modified exception for a recurring blocked time"""
        from di_core.containers import container

        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a recurring blocked time
        recurrence_rule = CalendarIntegrationTestFactory.create_recurrence_rule(
            organization=user.organization_membership.organization,
            frequency=RecurrenceFrequency.WEEKLY,
        )

        blocked_time = CalendarIntegrationTestFactory.create_blocked_time(
            calendar=calendar,
            reason="Weekly team meeting",
            recurrence_rule=recurrence_rule,
        )

        # Create a mock calendar service and mock exception instance
        mock_calendar_service = Mock()
        mock_exception_instance = CalendarIntegrationTestFactory.create_blocked_time(
            calendar=calendar,
            reason="Modified team meeting",
        )
        mock_calendar_service.create_recurring_blocked_time_exception.return_value = (
            mock_exception_instance
        )

        url = reverse("api:BlockedTimes-create-exception", kwargs={"pk": blocked_time.id})
        data = {
            "exception_date": "2024-02-15",  # A specific date to modify
            "modified_reason": "Modified team meeting",
            "modified_start_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=3)
            ).isoformat(),
            "modified_end_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=4)
            ).isoformat(),
            "is_cancelled": False,
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        assert "reason" in response.data  # Response should contain the modified event data

        # Verify the mock was called with correct parameters
        mock_calendar_service.create_recurring_blocked_time_exception.assert_called_once()
        call_args = mock_calendar_service.create_recurring_blocked_time_exception.call_args
        assert call_args[1]["parent_blocked_time"] == blocked_time
        assert call_args[1]["modified_reason"] == "Modified team meeting"
        assert call_args[1]["is_cancelled"] is False

    def test_create_blocked_time_exception_non_recurring(self, auth_client, calendar, user):
        """Test creating an exception for a non-recurring blocked time (should fail)"""
        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a non-recurring blocked time
        blocked_time = CalendarIntegrationTestFactory.create_blocked_time(
            calendar=calendar,
            reason="One-time meeting",
        )

        url = reverse("api:BlockedTimes-create-exception", kwargs={"pk": blocked_time.id})
        data = {
            "exception_date": "2024-02-15",
            "is_cancelled": True,
        }

        response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "Blocked time is not a recurring" in str(response.data)

    def test_create_blocked_time_exception_validation_errors(self, auth_client, calendar, user):
        """Test validation errors when creating blocked time exceptions"""
        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a recurring blocked time
        recurrence_rule = CalendarIntegrationTestFactory.create_recurrence_rule(
            organization=user.organization_membership.organization,
            frequency=RecurrenceFrequency.WEEKLY,
        )

        blocked_time = CalendarIntegrationTestFactory.create_blocked_time(
            calendar=calendar,
            reason="Weekly team meeting",
            recurrence_rule=recurrence_rule,
        )

        url = reverse("api:BlockedTimes-create-exception", kwargs={"pk": blocked_time.id})

        # Test missing exception_date
        response = auth_client.post(url, {}, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

        # Test invalid time range (modified_start_time after modified_end_time)
        data = {
            "exception_date": "2024-02-15",
            "modified_start_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=4)
            ).isoformat(),
            "modified_end_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=3)
            ).isoformat(),
            "is_cancelled": False,
        }
        response = auth_client.post(url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

        # Test non-cancelled exception without modifications
        data = {
            "exception_date": "2024-02-15",
            "is_cancelled": False,
        }
        response = auth_client.post(url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)


@pytest.mark.django_db
class TestAvailableTimeViewSet:
    """Test suite for AvailableTimeViewSet"""

    def test_list_available_times_authenticated(self, auth_client, calendar, user):
        """Test listing available times as authenticated user"""
        # Create calendar ownership so user can access the available times
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create available time
        CalendarIntegrationTestFactory.create_available_time(
            calendar=calendar,
        )

        url = reverse("api:AvailableTimes-list")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        assert "results" in response.data
        assert len(response.data["results"]) == 1
        # Note: AvailableTime model doesn't have can_book_partially field
        # This field exists only in AvailableTimeWindow dataclass

    def test_list_available_times_unauthenticated(self, anonymous_client):
        """Test listing available times as unauthenticated user"""
        url = reverse("api:AvailableTimes-list")
        response = anonymous_client.get(url)

        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    def test_create_available_time(self, auth_client, calendar, user):
        """Test creating an available time"""
        from di_core.containers import container

        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a mock calendar service
        mock_calendar_service = Mock()

        created_available_time = CalendarIntegrationTestFactory.create_available_time(
            calendar=calendar,
        )

        mock_calendar_service.create_available_time.return_value = created_available_time

        url = reverse("api:AvailableTimes-list")
        data = {
            "calendar": calendar.id,
            "start_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
            ).isoformat(),
            "end_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)
            ).isoformat(),
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)

        # Verify the mock was called
        mock_calendar_service.create_available_time.assert_called_once()

    def test_create_recurring_available_time(self, auth_client, calendar, user):
        """Test creating a recurring available time"""
        from di_core.containers import container

        # Set up the calendar to allow available windows management
        calendar.can_manage_available_windows = True
        calendar.save()

        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a mock calendar service
        mock_calendar_service = Mock()

        created_available_time = CalendarIntegrationTestFactory.create_available_time(
            calendar=calendar,
        )

        mock_calendar_service.create_available_time.return_value = created_available_time

        url = reverse("api:AvailableTimes-list")
        data = {
            "calendar": calendar.id,
            "start_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
            ).isoformat(),
            "end_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)
            ).isoformat(),
            "rrule_string": "FREQ=DAILY;COUNT=5;BYDAY=MO,TU,WE,TH,FR",
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        # Note: can_book_partially field doesn't exist in AvailableTime model, only in AvailableTimeWindow

        # Verify the mock was called
        mock_calendar_service.create_available_time.assert_called_once()

    def test_bulk_create_available_times(self, auth_client, calendar, user):
        """Test bulk creating available times"""
        from di_core.containers import container

        # Set up the calendar to allow available windows management
        calendar.can_manage_available_windows = True
        calendar.save()

        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a mock calendar service
        mock_calendar_service = Mock()

        created_available_times = [
            CalendarIntegrationTestFactory.create_available_time(
                calendar=calendar,
            ),
            CalendarIntegrationTestFactory.create_available_time(
                calendar=calendar,
            ),
        ]

        mock_calendar_service.bulk_create_availability_windows.return_value = (
            created_available_times
        )

        url = reverse("api:AvailableTimes-bulk-create")
        data = {
            "available_times": [
                {
                    "calendar": calendar.id,
                    "start_time": (
                        datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
                    ).isoformat(),
                    "end_time": (
                        datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)
                    ).isoformat(),
                },
                {
                    "calendar": calendar.id,
                    "start_time": (
                        datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=3)
                    ).isoformat(),
                    "end_time": (
                        datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=4)
                    ).isoformat(),
                },
            ],
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        assert len(response.data) == 2

        # Verify the mock was called
        mock_calendar_service.bulk_create_availability_windows.assert_called_once()

    def test_get_available_times_expanded(self, auth_client, calendar, user):
        """Test getting expanded available times (including recurring occurrences)"""
        from di_core.containers import container

        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a mock calendar service
        mock_calendar_service = Mock()

        available_times = [
            CalendarIntegrationTestFactory.create_available_time(
                calendar=calendar,
            ),
            CalendarIntegrationTestFactory.create_available_time(
                calendar=calendar,
            ),
        ]

        mock_calendar_service.get_available_times_expanded.return_value = available_times

        url = reverse("api:AvailableTimes-expanded")
        params = {
            "calendar_id": calendar.id,
            "start_time": "2024-01-01T00:00:00Z",  # Fixed: use start_time (not start_datetime)
            "end_time": "2024-01-31T23:59:59Z",  # Fixed: use end_time (not end_datetime)
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.get(url, params)

        assert_response_status_code(response, status.HTTP_200_OK)
        assert len(response.data) == 2

        # Verify the mock was called
        mock_calendar_service.get_available_times_expanded.assert_called_once()

    def test_create_available_time_validation_errors(self, auth_client, calendar):
        """Test creating available time with validation errors"""
        # Set up the calendar to allow available windows management
        calendar.can_manage_available_windows = True
        calendar.save()

        url = reverse("api:AvailableTimes-list")

        # Test missing required fields
        response = auth_client.post(url, {}, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

        # Test invalid time range (end before start)
        now = datetime.datetime.now(datetime.UTC)
        data = {
            "calendar": calendar.id,
            "start_time": (now + datetime.timedelta(hours=2)).isoformat(),
            "end_time": (now + datetime.timedelta(hours=1)).isoformat(),
        }
        response = auth_client.post(url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_retrieve_available_time(self, auth_client, calendar, user):
        """Test retrieving a specific available time"""
        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create available time
        available_time = CalendarIntegrationTestFactory.create_available_time(
            calendar=calendar,
        )

        url = reverse("api:AvailableTimes-detail", kwargs={"pk": available_time.id})
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        assert response.data["id"] == available_time.id

    def test_update_available_time(self, auth_client, calendar, user):
        """Test updating an available time"""
        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create available time
        available_time = CalendarIntegrationTestFactory.create_available_time(
            calendar=calendar,
        )

        url = reverse("api:AvailableTimes-detail", kwargs={"pk": available_time.id})
        updated_data = {
            "start_time": available_time.start_time.isoformat(),
            "end_time": available_time.end_time.isoformat(),
            "calendar": calendar.id,
        }

        response = auth_client.put(url, updated_data, format="json")

        assert_response_status_code(response, status.HTTP_200_OK)

    def test_delete_available_time(self, auth_client, calendar, user):
        """Test deleting an available time"""
        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create available time
        available_time = CalendarIntegrationTestFactory.create_available_time(
            calendar=calendar,
        )

        url = reverse("api:AvailableTimes-detail", kwargs={"pk": available_time.id})
        response = auth_client.delete(url)

        assert_response_status_code(response, status.HTTP_204_NO_CONTENT)

    def test_create_available_time_exception_cancelled(self, auth_client, calendar, user):
        """Test creating a cancelled exception for a recurring available time"""
        from di_core.containers import container

        # Set up the calendar to allow available windows management
        calendar.can_manage_available_windows = True
        calendar.save()

        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a recurring available time
        recurrence_rule = CalendarIntegrationTestFactory.create_recurrence_rule(
            organization=user.organization_membership.organization,
            frequency=RecurrenceFrequency.WEEKLY,
        )

        available_time = CalendarIntegrationTestFactory.create_available_time(
            calendar=calendar,
            recurrence_rule=recurrence_rule,
        )

        # Create a mock calendar service
        mock_calendar_service = Mock()
        mock_calendar_service.create_recurring_available_time_exception.return_value = (
            None  # Cancelled exception
        )

        url = reverse("api:AvailableTimes-create-exception", kwargs={"pk": available_time.id})
        data = {
            "exception_date": "2024-02-15",  # A specific date to cancel
            "is_cancelled": True,
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_204_NO_CONTENT)

        # Verify the mock was called with correct parameters
        mock_calendar_service.create_recurring_available_time_exception.assert_called_once()
        call_args = mock_calendar_service.create_recurring_available_time_exception.call_args
        assert call_args[1]["parent_available_time"] == available_time
        assert call_args[1]["is_cancelled"] is True

    def test_create_available_time_exception_modified(self, auth_client, calendar, user):
        """Test creating a modified exception for a recurring available time"""
        from di_core.containers import container

        # Set up the calendar to allow available windows management
        calendar.can_manage_available_windows = True
        calendar.save()

        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a recurring available time
        recurrence_rule = CalendarIntegrationTestFactory.create_recurrence_rule(
            organization=user.organization_membership.organization,
            frequency=RecurrenceFrequency.WEEKLY,
        )

        available_time = CalendarIntegrationTestFactory.create_available_time(
            calendar=calendar,
            recurrence_rule=recurrence_rule,
        )

        # Create a mock calendar service and mock exception instance
        mock_calendar_service = Mock()
        mock_exception_instance = CalendarIntegrationTestFactory.create_available_time(
            calendar=calendar,
        )
        mock_calendar_service.create_recurring_available_time_exception.return_value = (
            mock_exception_instance
        )

        url = reverse("api:AvailableTimes-create-exception", kwargs={"pk": available_time.id})
        data = {
            "exception_date": "2024-02-15",  # A specific date to modify
            "modified_start_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=3)
            ).isoformat(),
            "modified_end_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=4)
            ).isoformat(),
            "is_cancelled": False,
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        assert "start_time" in response.data  # Response should contain the modified event data

        # Verify the mock was called with correct parameters
        mock_calendar_service.create_recurring_available_time_exception.assert_called_once()
        call_args = mock_calendar_service.create_recurring_available_time_exception.call_args
        assert call_args[1]["parent_available_time"] == available_time
        assert call_args[1]["is_cancelled"] is False

    def test_create_available_time_exception_non_recurring(self, auth_client, calendar, user):
        """Test creating an exception for a non-recurring available time (should fail)"""
        # Set up the calendar to allow available windows management
        calendar.can_manage_available_windows = True
        calendar.save()

        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a non-recurring available time
        available_time = CalendarIntegrationTestFactory.create_available_time(
            calendar=calendar,
        )

        url = reverse("api:AvailableTimes-create-exception", kwargs={"pk": available_time.id})
        data = {
            "exception_date": "2024-02-15",
            "is_cancelled": True,
        }

        response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "Available time is not a recurring" in str(response.data)

    def test_create_available_time_exception_validation_errors(self, auth_client, calendar, user):
        """Test validation errors when creating available time exceptions"""
        # Set up the calendar to allow available windows management
        calendar.can_manage_available_windows = True
        calendar.save()

        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a recurring available time
        recurrence_rule = CalendarIntegrationTestFactory.create_recurrence_rule(
            organization=user.organization_membership.organization,
            frequency=RecurrenceFrequency.WEEKLY,
        )

        available_time = CalendarIntegrationTestFactory.create_available_time(
            calendar=calendar,
            recurrence_rule=recurrence_rule,
        )

        url = reverse("api:AvailableTimes-create-exception", kwargs={"pk": available_time.id})

        # Test missing exception_date
        response = auth_client.post(url, {}, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

        # Test invalid time range (modified_start_time after modified_end_time)
        data = {
            "exception_date": "2024-02-15",
            "modified_start_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=4)
            ).isoformat(),
            "modified_end_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=3)
            ).isoformat(),
            "is_cancelled": False,
        }
        response = auth_client.post(url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

        # Test non-cancelled exception without modifications
        data = {
            "exception_date": "2024-02-15",
            "is_cancelled": False,
        }
        response = auth_client.post(url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)


@pytest.mark.django_db
class TestRecurringBlockedAndAvailableTimeViewSets:
    """Test suite for recurring functionality in BlockedTime and AvailableTime ViewSets"""

    def test_create_recurring_blocked_time_with_recurrence_rule(self, auth_client, calendar, user):
        """Test creating a recurring blocked time with recurrence_rule object"""
        from di_core.containers import container

        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a mock calendar service
        mock_calendar_service = Mock()

        created_blocked_time = CalendarIntegrationTestFactory.create_blocked_time(
            calendar=calendar,
            reason="Daily standup",
        )

        mock_calendar_service.create_blocked_time.return_value = created_blocked_time

        url = reverse("api:BlockedTimes-list")
        data = {
            "calendar": calendar.id,  # Fixed: use calendar_id instead of calendar
            "reason": "Daily standup",
            "start_time": (  # Fixed: use start_time (not start_datetime)
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
            ).isoformat(),
            "end_time": (  # Fixed: use end_time (not end_datetime)
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)
            ).isoformat(),
            "rrule_string": "FREQ=DAILY;INTERVAL=1;COUNT=20;BYDAY=MO,TU,WE,TH,FR",  # Fixed: use RRULE string instead of object
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        assert response.data["reason"] == "Daily standup"

        # Verify the mock was called
        mock_calendar_service.create_blocked_time.assert_called_once()

    def test_create_recurring_available_time_with_recurrence_rule(
        self, auth_client, calendar, user
    ):
        """Test creating a recurring available time with recurrence_rule object"""
        from di_core.containers import container

        # Set up the calendar to allow available windows management
        calendar.can_manage_available_windows = True
        calendar.save()

        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a mock calendar service
        mock_calendar_service = Mock()

        created_available_time = CalendarIntegrationTestFactory.create_available_time(
            calendar=calendar,
        )

        mock_calendar_service.create_available_time.return_value = created_available_time

        url = reverse("api:AvailableTimes-list")
        data = {
            "calendar": calendar.id,  # Fixed: use calendar_id instead of calendar
            "start_time": (  # Fixed: use start_time (not start_datetime)
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
            ).isoformat(),
            "end_time": (  # Fixed: use end_time (not end_datetime)
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)
            ).isoformat(),
            "rrule_string": "FREQ=WEEKLY;INTERVAL=1;COUNT=10;BYDAY=MO,WE,FR",  # Fixed: use RRULE string instead of object
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        # Note: can_book_partially field doesn't exist in AvailableTime model, only in AvailableTimeWindow

        # Verify the mock was called
        mock_calendar_service.create_available_time.assert_called_once()

    def test_recurring_validation_errors(self, auth_client, calendar, user):
        """Test validation errors when creating recurring items"""
        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        blocked_time_url = reverse("api:BlockedTimes-list")
        available_time_url = reverse("api:AvailableTimes-list")

        now = datetime.datetime.now(datetime.UTC)

        # Test missing required fields for blocked time
        data = {
            "calendar_id": calendar.id,
            "reason": "Missing fields test",
            # Missing start_time, end_time, and recurrence_rule
        }

        response = auth_client.post(blocked_time_url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "start_time" in response.data or "This field is required" in str(response.data)

        # Test invalid recurrence rule format
        data = {
            "calendar_id": calendar.id,
            "reason": "Invalid recurrence rule",
            "start_time": (now + datetime.timedelta(hours=1)).isoformat(),
            "end_time": (now + datetime.timedelta(hours=2)).isoformat(),
            "recurrence_rule": "INVALID_RRULE_FORMAT",  # This should cause validation error
        }

        response = auth_client.post(blocked_time_url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

        # Test for available times - set up calendar first
        calendar.can_manage_available_windows = True
        calendar.save()

        # Test missing required fields for available time
        data = {
            "calendar_id": calendar.id,
            # Missing start_time, end_time, and recurrence_rule
        }

        response = auth_client.post(available_time_url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "start_time" in response.data or "This field is required" in str(response.data)

    def test_expanded_endpoints_missing_params(self, auth_client, calendar, user):
        """Test expanded endpoints with missing required parameters"""
        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        blocked_time_url = reverse("api:BlockedTimes-expanded")
        available_time_url = reverse("api:AvailableTimes-expanded")

        # Missing all parameters
        response = auth_client.get(blocked_time_url)
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

        response = auth_client.get(available_time_url)
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

        # Missing end_date
        params = {
            "calendar_id": calendar.id,
            "start_date": "2024-01-01",
        }

        response = auth_client.get(blocked_time_url, params)
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

        response = auth_client.get(available_time_url, params)
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
