import datetime
import json
import uuid
from unittest.mock import Mock, patch

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
    ChildrenCalendarRelationship,
    RecurrenceRule,
)
from calendar_integration.services.dataclasses import (
    AvailableTimeWindow,
    UnavailableTimeWindow,
)
from organizations.models import Organization, OrganizationMembership, OrganizationRole


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
        start_time_tz_unaware=None,
        end_time_tz_unaware=None,
        timezone="UTC",
        external_id=None,
    ):
        if calendar is None:
            calendar = CalendarIntegrationTestFactory.create_calendar()

        if start_time_tz_unaware is None:
            start_time_tz_unaware = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
                hours=1
            )
        if end_time_tz_unaware is None:
            end_time_tz_unaware = start_time_tz_unaware + datetime.timedelta(hours=1)

        if external_id is None:
            external_id = f"test_event_{uuid.uuid4().hex[:8]}"

        return baker.make(
            CalendarEvent,
            calendar=calendar,
            organization=calendar.organization,
            title=title,
            description=description,
            start_time_tz_unaware=start_time_tz_unaware,
            end_time_tz_unaware=end_time_tz_unaware,
            timezone=timezone,
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
        start_time_tz_unaware=None,
        end_time_tz_unaware=None,
        timezone="UTC",
        external_id=None,
        recurrence_rule=None,
    ):
        if calendar is None:
            calendar = CalendarIntegrationTestFactory.create_calendar()

        if start_time_tz_unaware is None:
            start_time_tz_unaware = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
                hours=1
            )
        if end_time_tz_unaware is None:
            end_time_tz_unaware = start_time_tz_unaware + datetime.timedelta(hours=1)

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
            start_time_tz_unaware=start_time_tz_unaware,
            end_time_tz_unaware=end_time_tz_unaware,
            timezone=timezone,
            external_id=external_id,
            recurrence_rule=recurrence_rule,
        )

    @staticmethod
    def create_blocked_time(
        calendar=None,
        reason="Test blocked time",
        start_time_tz_unaware=None,
        end_time_tz_unaware=None,
        timezone="UTC",
        external_id=None,
        recurrence_rule=None,
    ):
        if calendar is None:
            calendar = CalendarIntegrationTestFactory.create_calendar()

        if start_time_tz_unaware is None:
            start_time_tz_unaware = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
                hours=1
            )
        if end_time_tz_unaware is None:
            end_time_tz_unaware = start_time_tz_unaware + datetime.timedelta(hours=1)

        if external_id is None:
            external_id = f"blocked_time_{uuid.uuid4().hex[:8]}"

        return baker.make(
            BlockedTime,
            calendar=calendar,
            organization=calendar.organization,
            reason=reason,
            start_time_tz_unaware=start_time_tz_unaware,
            end_time_tz_unaware=end_time_tz_unaware,
            timezone=timezone,
            external_id=external_id,
            recurrence_rule=recurrence_rule,
        )

    @staticmethod
    def create_available_time(
        calendar=None,
        start_time_tz_unaware=None,
        end_time_tz_unaware=None,
        timezone="UTC",
        recurrence_rule=None,
    ):
        if calendar is None:
            calendar = CalendarIntegrationTestFactory.create_calendar()

        if start_time_tz_unaware is None:
            start_time_tz_unaware = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
                hours=1
            )
        if end_time_tz_unaware is None:
            end_time_tz_unaware = start_time_tz_unaware + datetime.timedelta(hours=1)

        return baker.make(
            AvailableTime,
            calendar=calendar,
            organization=calendar.organization,
            start_time_tz_unaware=start_time_tz_unaware,
            end_time_tz_unaware=end_time_tz_unaware,
            timezone=timezone,
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
            start_time_tz_unaware=now + datetime.timedelta(hours=1),
            end_time_tz_unaware=now + datetime.timedelta(hours=2),
            external_id=f"meeting_{uuid.uuid4().hex[:8]}",
        )
        CalendarIntegrationTestFactory.create_calendar_event(
            calendar=calendar,
            title="Team standup",
            start_time_tz_unaware=now + datetime.timedelta(hours=3),
            end_time_tz_unaware=now + datetime.timedelta(hours=4),
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
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
            timezone="UTC",
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
            "timezone": "UTC",
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
            "timezone": "UTC",
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

    # --- Transfer action tests ---

    def test_transfer_event_success(self, organization, calendar, calendar_event):
        """Admin transfers an in-org event to an in-org target calendar."""
        from rest_framework.test import APIClient

        from di_core.containers import container

        # Admin user
        admin_user = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=admin_user,
            organization=organization,
            role=OrganizationRole.ADMIN,
        )

        # Source calendar owner
        source_owner = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=source_owner,
            organization=organization,
            role=OrganizationRole.MEMBER,
        )
        CalendarIntegrationTestFactory.create_calendar_ownership(source_owner, calendar)

        owner_social_account = baker.make(
            SocialAccount,
            user=source_owner,
            provider=calendar.provider,
        )
        baker.make(
            SocialToken,
            account=owner_social_account,
            token="fake_access_token",
            token_secret="fake_refresh_token",
            expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
        )

        # Target calendar in the same org
        target_calendar = CalendarIntegrationTestFactory.create_calendar(organization=organization)

        # Mock return value — a new event on the target calendar
        transferred_event = CalendarIntegrationTestFactory.create_calendar_event(
            calendar=target_calendar,
            title=calendar_event.title,
        )

        mock_calendar_service = Mock()
        mock_calendar_service.authenticate.return_value = None
        mock_calendar_service.transfer_event.return_value = transferred_event

        client = APIClient()
        client.force_authenticate(user=admin_user)
        url = reverse("api:CalendarEvents-transfer", kwargs={"pk": calendar_event.id})

        with container.calendar_service.override(mock_calendar_service):
            response = client.post(
                url, data={"target_calendar_id": target_calendar.id}, format="json"
            )

        assert_response_status_code(response, status.HTTP_200_OK)
        assert response.data["id"] == transferred_event.id

        # Verify transfer_event called with correct args
        mock_calendar_service.transfer_event.assert_called_once_with(
            event=calendar_event,
            new_calendar=target_calendar,
        )

        # Verify service authenticated with SOURCE OWNER's account
        authenticate_call_args = mock_calendar_service.authenticate.call_args
        assert authenticate_call_args is not None
        account_arg = authenticate_call_args[1]["account"]
        assert account_arg == owner_social_account
        assert account_arg.user == source_owner

    def test_transfer_event_same_calendar_no_op(self, organization, calendar, calendar_event):
        """Admin tries to transfer event to its own calendar → 400, no service call."""
        from rest_framework.test import APIClient

        from di_core.containers import container

        # Admin user
        admin_user = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=admin_user,
            organization=organization,
            role=OrganizationRole.ADMIN,
        )

        # Mock calendar service
        mock_calendar_service = Mock()

        client = APIClient()
        client.force_authenticate(user=admin_user)
        url = reverse("api:CalendarEvents-transfer", kwargs={"pk": calendar_event.id})

        with container.calendar_service.override(mock_calendar_service):
            response = client.post(
                url, data={"target_calendar_id": calendar_event.calendar_fk_id}, format="json"
            )

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert response.data["target_calendar_id"][0] == "Event is already on the target calendar."

        # Verify service was NOT called (guard returned before authentication)
        mock_calendar_service.authenticate.assert_not_called()
        mock_calendar_service.transfer_event.assert_not_called()

    def test_transfer_event_non_admin_forbidden(self, organization, calendar, calendar_event):
        """Non-admin active member receives 403."""
        from rest_framework.test import APIClient

        member_user = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=member_user,
            organization=organization,
            role=OrganizationRole.MEMBER,
        )

        target_calendar = CalendarIntegrationTestFactory.create_calendar(organization=organization)

        client = APIClient()
        client.force_authenticate(user=member_user)
        url = reverse("api:CalendarEvents-transfer", kwargs={"pk": calendar_event.id})

        response = client.post(url, data={"target_calendar_id": target_calendar.id}, format="json")
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_transfer_event_cross_org_event_not_found(self, organization):
        """Event from a different org yields 404 (org-scoped queryset)."""
        from rest_framework.test import APIClient

        admin_user = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=admin_user,
            organization=organization,
            role=OrganizationRole.ADMIN,
        )

        other_org = CalendarIntegrationTestFactory.create_organization(name="Other Org")
        other_calendar = CalendarIntegrationTestFactory.create_calendar(organization=other_org)
        other_event = CalendarIntegrationTestFactory.create_calendar_event(calendar=other_calendar)

        client = APIClient()
        client.force_authenticate(user=admin_user)
        url = reverse("api:CalendarEvents-transfer", kwargs={"pk": other_event.id})

        response = client.post(url, data={"target_calendar_id": 1}, format="json")
        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_transfer_event_missing_target_calendar_id(
        self, organization, calendar, calendar_event
    ):
        """Missing target_calendar_id body field yields 400."""
        from rest_framework.test import APIClient

        admin_user = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=admin_user,
            organization=organization,
            role=OrganizationRole.ADMIN,
        )

        source_owner = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=source_owner,
            organization=organization,
            role=OrganizationRole.MEMBER,
        )
        CalendarIntegrationTestFactory.create_calendar_ownership(source_owner, calendar)

        client = APIClient()
        client.force_authenticate(user=admin_user)
        url = reverse("api:CalendarEvents-transfer", kwargs={"pk": calendar_event.id})

        response = client.post(url, data={}, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_transfer_event_invalid_target_calendar_id(
        self, organization, calendar, calendar_event
    ):
        """Non-existent target_calendar_id yields 400."""
        from rest_framework.test import APIClient

        admin_user = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=admin_user,
            organization=organization,
            role=OrganizationRole.ADMIN,
        )

        source_owner = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=source_owner,
            organization=organization,
            role=OrganizationRole.MEMBER,
        )
        CalendarIntegrationTestFactory.create_calendar_ownership(source_owner, calendar)

        client = APIClient()
        client.force_authenticate(user=admin_user)
        url = reverse("api:CalendarEvents-transfer", kwargs={"pk": calendar_event.id})

        response = client.post(url, data={"target_calendar_id": 999999}, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "invalid or not in your organization" in response.data["target_calendar_id"][0]

    def test_transfer_event_target_calendar_not_in_org(
        self, organization, calendar, calendar_event
    ):
        """target_calendar_id from a different org yields 400."""
        from rest_framework.test import APIClient

        admin_user = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=admin_user,
            organization=organization,
            role=OrganizationRole.ADMIN,
        )

        source_owner = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=source_owner,
            organization=organization,
            role=OrganizationRole.MEMBER,
        )
        CalendarIntegrationTestFactory.create_calendar_ownership(source_owner, calendar)

        other_org = CalendarIntegrationTestFactory.create_organization(name="Other Org")
        other_calendar = CalendarIntegrationTestFactory.create_calendar(organization=other_org)

        client = APIClient()
        client.force_authenticate(user=admin_user)
        url = reverse("api:CalendarEvents-transfer", kwargs={"pk": calendar_event.id})

        response = client.post(url, data={"target_calendar_id": other_calendar.id}, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "invalid or not in your organization" in response.data["target_calendar_id"][0]

    def test_transfer_event_source_owner_no_linked_account(
        self, organization, calendar, calendar_event
    ):
        """Source calendar owner has no linked social account → 400."""
        from rest_framework.test import APIClient

        admin_user = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=admin_user,
            organization=organization,
            role=OrganizationRole.ADMIN,
        )

        source_owner = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=source_owner,
            organization=organization,
            role=OrganizationRole.MEMBER,
        )
        CalendarIntegrationTestFactory.create_calendar_ownership(source_owner, calendar)
        # Intentionally do NOT create a SocialAccount for source_owner

        target_calendar = CalendarIntegrationTestFactory.create_calendar(organization=organization)

        client = APIClient()
        client.force_authenticate(user=admin_user)
        url = reverse("api:CalendarEvents-transfer", kwargs={"pk": calendar_event.id})

        response = client.post(url, data={"target_calendar_id": target_calendar.id}, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "no linked" in response.data["detail"].lower()

    def test_transfer_event_unauthenticated(self, anonymous_client, calendar_event):
        """Unauthenticated request yields 401."""
        url = reverse("api:CalendarEvents-transfer", kwargs={"pk": calendar_event.id})
        response = anonymous_client.post(url, data={"target_calendar_id": 1}, format="json")
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
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
            timezone="UTC",
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
            "timezone": "UTC",
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
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
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
            "timezone": "UTC",
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
            "timezone": "UTC",
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
            "timezone": "UTC",
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
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
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
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
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
            start_time_tz_unaware=modified_start_time,
            end_time_tz_unaware=modified_end_time,
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
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
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
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
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

    def test_bulk_modify_recurring_event(self, auth_client, calendar, user, social_account):
        """Test bulk modifying recurring events from a specific date"""
        from di_core.containers import container

        # Create a recurring event
        start_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        end_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)

        recurring_event = CalendarIntegrationTestFactory.create_recurring_event(
            calendar=calendar,
            title="Weekly Meeting",
            description="Weekly team meeting",
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
            external_id="recurring_weekly_meeting",
        )

        # Create calendar ownership for the user
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a mock modified event for the continuation
        modified_start_time = start_time + datetime.timedelta(days=7, hours=1)
        modified_end_time = end_time + datetime.timedelta(days=7, hours=1)

        continuation_event = CalendarIntegrationTestFactory.create_calendar_event(
            calendar=calendar,
            title="Modified Weekly Meeting",
            description="Modified weekly team meeting",
            start_time_tz_unaware=modified_start_time,
            end_time_tz_unaware=modified_end_time,
            external_id="modified_weekly_meeting",
        )

        # Create a mock calendar service
        mock_calendar_service = Mock()
        mock_calendar_service.authenticate.return_value = None
        mock_calendar_service.modify_recurring_event_from_date.return_value = continuation_event

        url = reverse("api:CalendarEvents-bulk-modify", kwargs={"pk": recurring_event.id})
        modification_date = (start_time + datetime.timedelta(days=7)).date()
        data = {
            "modification_start_date": modification_date.isoformat(),
            "modified_title": "Modified Weekly Meeting",
            "modified_description": "Modified weekly team meeting",
            "modified_start_time_offset": "01:00:00",  # Move start time by 1 hour
            "modified_end_time_offset": "01:00:00",  # Move end time by 1 hour
            "is_cancelled": False,
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_200_OK)
        assert response.data["title"] == "Modified Weekly Meeting"
        assert response.data["description"] == "Modified weekly team meeting"

        # Verify the mock was called with correct parameters
        mock_calendar_service.modify_recurring_event_from_date.assert_called_once()

    def test_bulk_cancel_recurring_event(self, auth_client, calendar, user, social_account):
        """Test bulk cancelling recurring events from a specific date"""
        from di_core.containers import container

        # Create a recurring event
        start_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        end_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)

        recurring_event = CalendarIntegrationTestFactory.create_recurring_event(
            calendar=calendar,
            title="Weekly Meeting",
            description="Weekly team meeting",
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
            external_id="recurring_weekly_meeting",
        )

        # Create calendar ownership for the user
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a mock calendar service
        mock_calendar_service = Mock()
        mock_calendar_service.authenticate.return_value = None
        mock_calendar_service.cancel_recurring_event_from_date.return_value = None

        url = reverse("api:CalendarEvents-bulk-modify", kwargs={"pk": recurring_event.id})
        modification_date = (start_time + datetime.timedelta(days=7)).date()
        data = {
            "modification_start_date": modification_date.isoformat(),
            "is_cancelled": True,
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_204_NO_CONTENT)

        # Verify the mock was called with correct parameters
        mock_calendar_service.cancel_recurring_event_from_date.assert_called_once()

    def test_bulk_modify_recurring_event_with_rrule(
        self, auth_client, calendar, user, social_account
    ):
        """Test bulk modifying recurring events with custom recurrence rule"""
        from di_core.containers import container

        # Create a recurring event
        start_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        end_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)

        recurring_event = CalendarIntegrationTestFactory.create_recurring_event(
            calendar=calendar,
            title="Weekly Meeting",
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
        )

        # Create calendar ownership for the user
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        continuation_event = CalendarIntegrationTestFactory.create_calendar_event(
            calendar=calendar,
            title="Modified Weekly Meeting",
            start_time_tz_unaware=start_time + datetime.timedelta(days=7),
            end_time_tz_unaware=end_time + datetime.timedelta(days=7),
        )

        mock_calendar_service = Mock()
        mock_calendar_service.authenticate.return_value = None
        mock_calendar_service.modify_recurring_event_from_date.return_value = continuation_event

        url = reverse("api:CalendarEvents-bulk-modify", kwargs={"pk": recurring_event.id})
        modification_date = (start_time + datetime.timedelta(days=7)).date()
        data = {
            "modification_start_date": modification_date.isoformat(),
            "modified_title": "Modified Weekly Meeting",
            "rrule_string": "FREQ=DAILY;COUNT=5",
            "is_cancelled": False,
        }

        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_200_OK)
        mock_calendar_service.modify_recurring_event_from_date.assert_called_once()

    def test_bulk_modify_non_recurring_event(self, auth_client, calendar, user):
        """Test bulk modifying a non-recurring event should fail"""
        # Create a non-recurring event
        start_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        end_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)

        non_recurring_event = CalendarIntegrationTestFactory.create_calendar_event(
            calendar=calendar,
            title="Single Meeting",
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
            external_id="single_meeting",
        )

        # Create calendar ownership for the user
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        url = reverse("api:CalendarEvents-bulk-modify", kwargs={"pk": non_recurring_event.id})
        modification_date = (start_time + datetime.timedelta(days=7)).date()
        data = {
            "modification_start_date": modification_date.isoformat(),
            "is_cancelled": True,
        }

        response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "not a recurring event" in str(response.data)

    def test_bulk_modify_validation_errors(self, auth_client, calendar, user):
        """Test validation errors when bulk modifying recurring events"""
        # Create a recurring event
        start_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        end_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)

        recurring_event = CalendarIntegrationTestFactory.create_recurring_event(
            calendar=calendar,
            title="Weekly Meeting",
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
        )

        # Create calendar ownership for the user
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        url = reverse("api:CalendarEvents-bulk-modify", kwargs={"pk": recurring_event.id})

        # Test missing required fields
        response = auth_client.post(url, {}, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

        # Test conflicting recurrence rule fields
        modification_date = (start_time + datetime.timedelta(days=7)).date()
        data = {
            "modification_start_date": modification_date.isoformat(),
            "recurrence_rule": {"frequency": "DAILY", "interval": 1},
            "rrule_string": "FREQ=WEEKLY;COUNT=5",
            "is_cancelled": False,
        }
        response = auth_client.post(url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "Cannot specify both recurrence_rule and rrule_string" in str(response.data)


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
        assert "Invalid isoformat string" in str(response.data)

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

    def test_delete_calendar_soft_disables(self, auth_client, calendar, user):
        """DELETE /calendar/{id}/ sets is_active=False — row persists, not hard-deleted."""
        # Create calendar ownership so user can access the calendar
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        url = reverse("api:Calendars-detail", kwargs={"pk": calendar.id})
        response = auth_client.delete(url)

        assert_response_status_code(response, status.HTTP_204_NO_CONTENT)

        # Row must still exist with is_active=False
        calendar.refresh_from_db()
        assert calendar.is_active is False

    def test_delete_calendar_hidden_from_default_list(self, auth_client, calendar, user):
        """After soft-disable, default GET /calendar/ list excludes the disabled calendar."""
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Soft-disable
        url = reverse("api:Calendars-detail", kwargs={"pk": calendar.id})
        auth_client.delete(url)

        # Default list should be empty
        list_url = reverse("api:Calendars-list")
        response = auth_client.get(list_url)
        assert_response_status_code(response, status.HTTP_200_OK)
        ids = [c["id"] for c in response.data["results"]]
        assert calendar.id not in ids

    def test_include_inactive_shows_disabled_calendar(self, auth_client, calendar, user):
        """GET /calendar/?include_inactive=true includes disabled calendars."""
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Soft-disable
        url = reverse("api:Calendars-detail", kwargs={"pk": calendar.id})
        auth_client.delete(url)

        # include_inactive=true should surface the calendar
        list_url = reverse("api:Calendars-list")
        response = auth_client.get(list_url, {"include_inactive": "true"})
        assert_response_status_code(response, status.HTTP_200_OK)
        ids = [c["id"] for c in response.data["results"]]
        assert calendar.id in ids

    def test_retrieve_disabled_calendar_returns_404_by_default(self, auth_client, calendar, user):
        """Retrieve of a disabled calendar via the default queryset → 404."""
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Soft-disable
        calendar.is_active = False
        calendar.save(update_fields=["is_active"])

        detail_url = reverse("api:Calendars-detail", kwargs={"pk": calendar.id})
        response = auth_client.get(detail_url)
        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_retrieve_disabled_calendar_visible_with_include_inactive(
        self, auth_client, calendar, user
    ):
        """Retrieve of a disabled calendar with ?include_inactive=true → 200."""
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Soft-disable
        calendar.is_active = False
        calendar.save(update_fields=["is_active"])

        detail_url = reverse("api:Calendars-detail", kwargs={"pk": calendar.id})
        response = auth_client.get(detail_url, {"include_inactive": "true"})
        assert_response_status_code(response, status.HTTP_200_OK)
        assert response.data["id"] == calendar.id
        assert response.data["is_active"] is False

    def test_delete_calendar_idempotent(self, auth_client, calendar, user):
        """Soft-disabling an already-inactive calendar returns 404 (calendar already hidden)."""
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        url = reverse("api:Calendars-detail", kwargs={"pk": calendar.id})

        # First soft-disable
        response = auth_client.delete(url)
        assert_response_status_code(response, status.HTTP_204_NO_CONTENT)

        # Second delete attempt — calendar is now hidden from default queryset → 404
        response = auth_client.delete(url)
        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_create_calendar_is_active_defaults_true(self, auth_client, organization, user):
        """Creating a calendar defaults is_active=True; it appears in the default list."""
        from di_core.containers import container

        mock_calendar_service = Mock()
        mock_calendar_service.initialize_without_provider.return_value = None

        created_calendar = CalendarIntegrationTestFactory.create_calendar(
            organization=organization,
            name="Active Calendar",
            description="Should be active by default",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.PERSONAL,
        )
        mock_calendar_service.create_virtual_calendar.return_value = created_calendar

        url = reverse("api:Calendars-list")
        data = {"name": "Active Calendar", "description": "Should be active by default"}

        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        assert response.data["is_active"] is True

        # Calendar should appear in default list
        list_url = reverse("api:Calendars-list")
        list_response = auth_client.get(list_url)
        assert_response_status_code(list_response, status.HTTP_200_OK)
        ids = [c["id"] for c in list_response.data["results"]]
        assert created_calendar.id in ids

    def test_org_scoping_still_holds(self, auth_client, organization, user):
        """Cross-org calendar is still excluded even when include_inactive=true."""
        other_org = CalendarIntegrationTestFactory.create_organization(name="Other Org")
        other_calendar = CalendarIntegrationTestFactory.create_calendar(organization=other_org)

        list_url = reverse("api:Calendars-list")
        response = auth_client.get(list_url, {"include_inactive": "true"})
        assert_response_status_code(response, status.HTTP_200_OK)
        ids = [c["id"] for c in response.data["results"]]
        assert other_calendar.id not in ids

    def test_membership_less_user_gets_empty_list(self):
        """User without org membership gets an empty list (not 500)."""
        from django.contrib.auth import get_user_model as _get_user_model

        from rest_framework.test import APIClient

        user_model = _get_user_model()
        memberless_user = baker.make(user_model)

        client = APIClient()
        client.force_authenticate(user=memberless_user)

        list_url = reverse("api:Calendars-list")
        response = client.get(list_url)
        assert_response_status_code(response, status.HTTP_200_OK)
        assert response.data["results"] == []

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

    def test_request_import_authenticated_with_social_account(
        self, auth_client, user, calendar, social_account
    ):
        """Test requesting calendar import with authenticated user and social account"""
        from di_core.containers import container

        # Create calendar ownership for the user
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a mock calendar service
        mock_calendar_service = Mock()
        mock_calendar_service.authenticate.return_value = None
        mock_calendar_service.request_calendars_import.return_value = None

        url = reverse("api:Calendars-request-import")

        with container.calendar_service.override(mock_calendar_service):
            with patch("calendar_integration.views.transaction.on_commit") as mock_on_commit:
                response = auth_client.post(url)

                # Verify response
                assert_response_status_code(response, status.HTTP_202_ACCEPTED)
                assert response.data["detail"] == "Calendar import requested."

                # Verify the mock was called with the correct arguments
                mock_calendar_service.authenticate.assert_called_once()

                # Verify on_commit was called and invoke the callback
                mock_on_commit.assert_called_once()
                callback = mock_on_commit.call_args[0][0]
                callback()

                # Now verify the service method was called
                mock_calendar_service.request_calendars_import.assert_called_once()

    def test_request_import_no_social_account(self, auth_client, user):
        """Test requesting calendar import without a connected social account"""
        # Create a new organization and membership for the user (but no social account)
        new_org = CalendarIntegrationTestFactory.create_organization()
        CalendarIntegrationTestFactory.create_organization_membership(user, new_org)

        url = reverse("api:Calendars-request-import")

        response = auth_client.post(url)
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "no connected external calendar account" in response.data["detail"].lower()

    def test_request_import_membership_less_user(self, auth_client, anonymous_client):
        """Test requesting calendar import as membership-less user"""
        url = reverse("api:Calendars-request-import")

        response = auth_client.post(url)
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)
        assert "not an active member" in response.data["detail"].lower()

    def test_request_import_unauthenticated(self, anonymous_client):
        """Test requesting calendar import as unauthenticated user"""
        url = reverse("api:Calendars-request-import")

        response = anonymous_client.post(url)
        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    def test_request_import_multiple_social_accounts(self, auth_client, user, organization):
        """Test requesting calendar import with multiple connected social accounts"""
        # Create two social accounts (Google and Microsoft)
        google_account = baker.make(
            SocialAccount,
            user=user,
            provider=CalendarProvider.GOOGLE,
        )
        baker.make(
            SocialToken,
            account=google_account,
            token="fake_google_token",
            token_secret="fake_google_refresh",
            expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
        )

        microsoft_account = baker.make(
            SocialAccount,
            user=user,
            provider=CalendarProvider.MICROSOFT,
        )
        baker.make(
            SocialToken,
            account=microsoft_account,
            token="fake_microsoft_token",
            token_secret="fake_microsoft_refresh",
            expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
        )

        url = reverse("api:Calendars-request-import")

        mock_service = Mock()
        mock_service.authenticate.return_value = None
        mock_service.request_calendars_import.return_value = None

        from di_core.containers import container

        with container.calendar_service.override(mock_service):
            with patch("calendar_integration.views.transaction.on_commit") as mock_on_commit:
                response = auth_client.post(url)

                # Verify response
                assert_response_status_code(response, status.HTTP_202_ACCEPTED)
                assert "2 account(s)" in response.data["detail"]

                # Verify on_commit was called twice (once per account)
                assert mock_on_commit.call_count == 2

                # Verify both callbacks invoke request_calendars_import
                callbacks = [call[0][0] for call in mock_on_commit.call_args_list]
                for callback in callbacks:
                    callback()
                assert mock_service.request_calendars_import.call_count == 2

    def test_request_sync_owner_syncs_own_calendar(
        self, auth_client, user, calendar, social_account
    ):
        """Test owner syncs their own calendar with valid range"""
        from di_core.containers import container

        # Create calendar ownership for the user
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a mock calendar service
        mock_calendar_service = Mock()
        mock_calendar_service.authenticate.return_value = None

        # Create a mock CalendarSync instance
        mock_calendar_sync = Mock()
        mock_calendar_sync.id = 123
        mock_calendar_sync.status = "NOT_STARTED"
        mock_calendar_sync.start_datetime = datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)
        mock_calendar_sync.end_datetime = datetime.datetime(2024, 1, 31, tzinfo=datetime.UTC)
        mock_calendar_sync.should_update_events = False
        mock_calendar_sync.error_message = ""
        mock_calendar_service.request_calendar_sync.return_value = mock_calendar_sync

        url = reverse("api:Calendars-request-sync", kwargs={"pk": calendar.id})

        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(
                url,
                data={
                    "start_datetime": "2024-01-01T00:00:00Z",
                    "end_datetime": "2024-01-31T23:59:59Z",
                    "should_update_events": False,
                },
                format="json",
            )

            # Verify response
            assert_response_status_code(response, status.HTTP_202_ACCEPTED)
            assert response.data["id"] == 123
            assert response.data["status"] == "NOT_STARTED"

            # Verify the mock was called with the correct arguments
            mock_calendar_service.authenticate.assert_called_once()
            mock_calendar_service.request_calendar_sync.assert_called_once()

    def test_request_sync_non_owner_forbidden(self, auth_client, user, calendar):
        """Test non-owner cannot sync a calendar"""
        # Don't create calendar ownership - user is not an owner
        url = reverse("api:Calendars-request-sync", kwargs={"pk": calendar.id})

        response = auth_client.post(
            url,
            data={
                "start_datetime": "2024-01-01T00:00:00Z",
                "end_datetime": "2024-01-31T23:59:59Z",
            },
            format="json",
        )

        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)
        assert "do not own this calendar" in response.data["detail"].lower()

    def test_request_sync_cross_org_calendar_not_found(
        self, auth_client, user, calendar, organization
    ):
        """Test cross-org calendar returns 404"""
        # Create a different organization and calendar
        other_org = CalendarIntegrationTestFactory.create_organization(name="Other Org")
        other_calendar = CalendarIntegrationTestFactory.create_calendar(organization=other_org)

        # User is only a member of the first org (from fixture)
        url = reverse("api:Calendars-request-sync", kwargs={"pk": other_calendar.id})

        response = auth_client.post(
            url,
            data={
                "start_datetime": "2024-01-01T00:00:00Z",
                "end_datetime": "2024-01-31T23:59:59Z",
            },
            format="json",
        )

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_request_sync_missing_datetimes(self, auth_client, user, calendar):
        """Test missing datetime parameters returns 400"""
        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        url = reverse("api:Calendars-request-sync", kwargs={"pk": calendar.id})

        # Missing both datetimes
        response = auth_client.post(url, data={}, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

        # Missing end_datetime
        response = auth_client.post(
            url,
            data={"start_datetime": "2024-01-01T00:00:00Z"},
            format="json",
        )
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_request_sync_invalid_datetimes(self, auth_client, user, calendar):
        """Test invalid datetime format returns 400"""
        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        url = reverse("api:Calendars-request-sync", kwargs={"pk": calendar.id})

        response = auth_client.post(
            url,
            data={
                "start_datetime": "invalid-date",
                "end_datetime": "2024-01-31T23:59:59Z",
            },
            format="json",
        )
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_request_sync_unauthenticated(self, anonymous_client, calendar):
        """Test unauthenticated user cannot sync"""
        url = reverse("api:Calendars-request-sync", kwargs={"pk": calendar.id})

        response = anonymous_client.post(
            url,
            data={
                "start_datetime": "2024-01-01T00:00:00Z",
                "end_datetime": "2024-01-31T23:59:59Z",
            },
            format="json",
        )
        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    def test_request_sync_no_social_account_for_provider(self, auth_client, user, calendar):
        """Test sync fails with 400 when user has no linked account for calendar provider"""
        # Create calendar ownership for the user
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Do NOT create a social account for this provider - this is the test case

        url = reverse("api:Calendars-request-sync", kwargs={"pk": calendar.id})

        response = auth_client.post(
            url,
            data={
                "start_datetime": "2024-01-01T00:00:00Z",
                "end_datetime": "2024-01-31T23:59:59Z",
            },
            format="json",
        )

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "No linked account found" in str(response.data)
        assert calendar.provider in str(response.data)

    def test_admin_sync_another_users_calendar(self, auth_client, organization, calendar):
        """Test admin syncs another user's calendar"""
        from di_core.containers import container

        # Create admin user in the organization
        admin_user = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=admin_user,
            organization=organization,
            role=OrganizationRole.ADMIN,
        )

        # Create calendar owner (different user)
        calendar_owner = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=calendar_owner,
            organization=organization,
            role=OrganizationRole.MEMBER,
        )

        # Create calendar ownership linking owner to calendar
        CalendarIntegrationTestFactory.create_calendar_ownership(calendar_owner, calendar)

        # Create social account for the calendar owner (not the admin)
        owner_social_account = baker.make(
            SocialAccount,
            user=calendar_owner,
            provider=calendar.provider,
        )
        baker.make(
            SocialToken,
            account=owner_social_account,
            token="fake_access_token",
            token_secret="fake_refresh_token",
            expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
        )

        # Create a mock calendar service
        mock_calendar_service = Mock()
        mock_calendar_service.authenticate.return_value = None

        # Create a mock CalendarSync instance
        mock_calendar_sync = Mock()
        mock_calendar_sync.id = 123
        mock_calendar_sync.status = "NOT_STARTED"
        mock_calendar_sync.start_datetime = datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)
        mock_calendar_sync.end_datetime = datetime.datetime(2024, 1, 31, tzinfo=datetime.UTC)
        mock_calendar_sync.should_update_events = False
        mock_calendar_sync.error_message = ""
        mock_calendar_service.request_calendar_sync.return_value = mock_calendar_sync

        # Authenticate as admin and make request
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=admin_user)

        url = reverse("api:Calendars-admin-sync", kwargs={"pk": calendar.id})

        with container.calendar_service.override(mock_calendar_service):
            response = client.post(
                url,
                data={
                    "start_datetime": "2024-01-01T00:00:00Z",
                    "end_datetime": "2024-01-31T23:59:59Z",
                    "should_update_events": False,
                },
                format="json",
            )

            # Verify response
            assert_response_status_code(response, status.HTTP_202_ACCEPTED)
            assert response.data["id"] == 123
            assert response.data["status"] == "NOT_STARTED"

            # Verify authenticate was called with OWNER's account, not admin's
            authenticate_call_args = mock_calendar_service.authenticate.call_args
            assert authenticate_call_args is not None
            account_arg = authenticate_call_args[1]["account"]
            assert account_arg == owner_social_account
            assert account_arg.user == calendar_owner
            assert account_arg.user != admin_user

            # Verify request_calendar_sync was called
            mock_calendar_service.request_calendar_sync.assert_called_once()

    def test_admin_sync_non_admin_member_forbidden(self, auth_client, organization, calendar):
        """Test non-admin member cannot sync any calendar"""
        # Create regular member user
        member_user = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=member_user,
            organization=organization,
            role=OrganizationRole.MEMBER,
        )

        # Create calendar owner (another user)
        calendar_owner = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=calendar_owner,
            organization=organization,
            role=OrganizationRole.MEMBER,
        )

        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(calendar_owner, calendar)

        # Authenticate as regular member
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=member_user)

        url = reverse("api:Calendars-admin-sync", kwargs={"pk": calendar.id})

        response = client.post(
            url,
            data={
                "start_datetime": "2024-01-01T00:00:00Z",
                "end_datetime": "2024-01-31T23:59:59Z",
            },
            format="json",
        )

        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_admin_sync_cross_org_calendar_not_found(self, auth_client, organization):
        """Test admin cannot sync calendar from different organization"""
        # Create admin in first organization
        admin_user = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=admin_user,
            organization=organization,
            role=OrganizationRole.ADMIN,
        )

        # Create calendar in different organization
        other_org = CalendarIntegrationTestFactory.create_organization(name="Other Org")
        other_calendar = CalendarIntegrationTestFactory.create_calendar(organization=other_org)

        # Authenticate as admin and try to sync cross-org calendar
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=admin_user)

        url = reverse("api:Calendars-admin-sync", kwargs={"pk": other_calendar.id})

        response = client.post(
            url,
            data={
                "start_datetime": "2024-01-01T00:00:00Z",
                "end_datetime": "2024-01-31T23:59:59Z",
            },
            format="json",
        )

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_admin_sync_owner_has_no_linked_account(self, auth_client, organization, calendar):
        """Test admin cannot sync if calendar owner has no linked account for provider"""
        # Create admin user
        admin_user = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=admin_user,
            organization=organization,
            role=OrganizationRole.ADMIN,
        )

        # Create calendar owner
        calendar_owner = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=calendar_owner,
            organization=organization,
            role=OrganizationRole.MEMBER,
        )

        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(calendar_owner, calendar)

        # Do NOT create social account for the owner - this is the test case

        # Authenticate as admin and make request
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=admin_user)

        url = reverse("api:Calendars-admin-sync", kwargs={"pk": calendar.id})

        response = client.post(
            url,
            data={
                "start_datetime": "2024-01-01T00:00:00Z",
                "end_datetime": "2024-01-31T23:59:59Z",
            },
            format="json",
        )

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "has no linked" in response.data["detail"].lower()
        assert calendar.provider in response.data["detail"]

    def test_admin_sync_calendar_has_no_owner(self, auth_client, organization):
        """Test admin cannot sync calendar with no owner"""
        # Create admin user
        admin_user = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=admin_user,
            organization=organization,
            role=OrganizationRole.ADMIN,
        )

        # Create calendar with no ownership
        calendar = CalendarIntegrationTestFactory.create_calendar(organization=organization)

        # Authenticate as admin and make request
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=admin_user)

        url = reverse("api:Calendars-admin-sync", kwargs={"pk": calendar.id})

        response = client.post(
            url,
            data={
                "start_datetime": "2024-01-01T00:00:00Z",
                "end_datetime": "2024-01-31T23:59:59Z",
            },
            format="json",
        )

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "has no owner" in response.data["detail"].lower()

    def test_admin_sync_invalid_datetimes(self, auth_client, organization, calendar):
        """Test admin-sync with invalid datetimes returns 400"""
        # Create admin user
        admin_user = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=admin_user,
            organization=organization,
            role=OrganizationRole.ADMIN,
        )

        # Create calendar owner
        calendar_owner = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=calendar_owner,
            organization=organization,
            role=OrganizationRole.MEMBER,
        )

        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(calendar_owner, calendar)

        # Authenticate as admin
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=admin_user)

        url = reverse("api:Calendars-admin-sync", kwargs={"pk": calendar.id})

        response = client.post(
            url,
            data={
                "start_datetime": "invalid-date",
                "end_datetime": "2024-01-31T23:59:59Z",
            },
            format="json",
        )

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_admin_sync_unauthenticated(self, anonymous_client, calendar):
        """Test unauthenticated user cannot admin-sync"""
        url = reverse("api:Calendars-admin-sync", kwargs={"pk": calendar.id})

        response = anonymous_client.post(
            url,
            data={
                "start_datetime": "2024-01-01T00:00:00Z",
                "end_datetime": "2024-01-31T23:59:59Z",
            },
            format="json",
        )

        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    def test_admin_sync_disabled_calendar_reachable_with_include_inactive(self, organization):
        """Admin can reach disabled calendar on action route via ?include_inactive=true."""
        from di_core.containers import container

        # Create admin user
        admin_user = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=admin_user,
            organization=organization,
            role=OrganizationRole.ADMIN,
        )

        # Create calendar owner
        calendar_owner = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=calendar_owner,
            organization=organization,
            role=OrganizationRole.MEMBER,
        )

        # Create calendar and ownership
        calendar = CalendarIntegrationTestFactory.create_calendar(organization=organization)
        CalendarIntegrationTestFactory.create_calendar_ownership(calendar_owner, calendar)

        # Create social account for the owner
        owner_social_account = baker.make(
            SocialAccount,
            user=calendar_owner,
            provider=calendar.provider,
        )
        baker.make(
            SocialToken,
            account=owner_social_account,
            token="fake_access_token",
            token_secret="fake_refresh_token",
            expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
        )

        # Disable the calendar
        calendar.is_active = False
        calendar.save(update_fields=["is_active"])

        # Create mock calendar service
        mock_calendar_service = Mock()
        mock_calendar_service.authenticate.return_value = None
        mock_calendar_sync = Mock()
        mock_calendar_sync.id = 456
        mock_calendar_sync.status = "NOT_STARTED"
        mock_calendar_sync.start_datetime = datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)
        mock_calendar_sync.end_datetime = datetime.datetime(2024, 1, 31, tzinfo=datetime.UTC)
        mock_calendar_sync.should_update_events = False
        mock_calendar_sync.error_message = ""
        mock_calendar_service.request_calendar_sync.return_value = mock_calendar_sync

        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=admin_user)

        url = reverse("api:Calendars-admin-sync", kwargs={"pk": calendar.id})

        with container.calendar_service.override(mock_calendar_service):
            # Test WITHOUT include_inactive — should be 404
            response = client.post(
                url,
                data={
                    "start_datetime": "2024-01-01T00:00:00Z",
                    "end_datetime": "2024-01-31T23:59:59Z",
                    "should_update_events": False,
                },
                format="json",
            )
            assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

            # Test WITH include_inactive=true — should be 202
            response = client.post(
                url,
                data={
                    "start_datetime": "2024-01-01T00:00:00Z",
                    "end_datetime": "2024-01-31T23:59:59Z",
                    "should_update_events": False,
                },
                format="json",
                **{"HTTP_X_INCLUDE_INACTIVE": "true"} if False else {},
            )
            # Need to use query params instead of headers for action routes
            response = client.post(
                f"{url}?include_inactive=true",
                data={
                    "start_datetime": "2024-01-01T00:00:00Z",
                    "end_datetime": "2024-01-31T23:59:59Z",
                    "should_update_events": False,
                },
                format="json",
            )
            assert_response_status_code(response, status.HTTP_202_ACCEPTED)
            assert response.data["id"] == 456
            assert response.data["status"] == "NOT_STARTED"


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
            start_time_tz_unaware=now + datetime.timedelta(hours=1),
            end_time_tz_unaware=now + datetime.timedelta(hours=2),
            external_id=f"early_{uuid.uuid4().hex[:8]}",
        )
        CalendarIntegrationTestFactory.create_calendar_event(
            calendar=calendar,
            title="Late Event",
            start_time_tz_unaware=now + datetime.timedelta(hours=3),
            end_time_tz_unaware=now + datetime.timedelta(hours=4),
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
            start_time_tz_unaware=now + datetime.timedelta(hours=1),
            end_time_tz_unaware=now + datetime.timedelta(hours=2),
            external_id=f"short_{uuid.uuid4().hex[:8]}",
        )
        CalendarIntegrationTestFactory.create_calendar_event(
            calendar=calendar,
            title="Long Event",
            start_time_tz_unaware=now + datetime.timedelta(hours=1),
            end_time_tz_unaware=now + datetime.timedelta(hours=4),
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
            "timezone": "UTC",
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
            "calendar": calendar.id,
            "reason": "Weekly team sync",
            "start_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
            ).isoformat(),
            "end_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)
            ).isoformat(),
            "timezone": "UTC",
            "rrule_string": "FREQ=WEEKLY;COUNT=10;BYDAY=MO",
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
                    "timezone": "UTC",
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
                    "timezone": "UTC",
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
            "timezone": "UTC",
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
            "timezone": "UTC",
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
        assert "Blocked time is not recurring" in str(response.data)

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

    def test_bulk_modify_recurring_blocked_time(self, auth_client, calendar, user):
        """Test bulk modifying recurring blocked time from a specific date"""
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
            reason="Weekly maintenance",
            recurrence_rule=recurrence_rule,
        )

        # Create a mock calendar service and continuation blocked time
        mock_calendar_service = Mock()
        continuation_blocked_time = CalendarIntegrationTestFactory.create_blocked_time(
            calendar=calendar,
            reason="Modified weekly maintenance",
        )
        mock_calendar_service.modify_recurring_blocked_time_from_date.return_value = (
            continuation_blocked_time
        )

        url = reverse("api:BlockedTimes-bulk-modify", kwargs={"pk": blocked_time.id})
        modification_date = (
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=7)
        ).date()
        data = {
            "modification_start_date": modification_date.isoformat(),
            "modified_reason": "Modified weekly maintenance",
            "modified_start_time_offset": "01:00:00",  # Move start time by 1 hour
            "modified_end_time_offset": "01:00:00",  # Move end time by 1 hour
            "is_cancelled": False,
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_200_OK)
        assert response.data["reason"] == "Modified weekly maintenance"

        # Verify the mock was called with correct parameters
        mock_calendar_service.modify_recurring_blocked_time_from_date.assert_called_once()

    def test_bulk_cancel_recurring_blocked_time(self, auth_client, calendar, user):
        """Test bulk cancelling recurring blocked time from a specific date"""
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
            reason="Weekly maintenance",
            recurrence_rule=recurrence_rule,
        )

        # Create a mock calendar service
        mock_calendar_service = Mock()
        mock_calendar_service.cancel_recurring_blocked_time_from_date.return_value = None

        url = reverse("api:BlockedTimes-bulk-modify", kwargs={"pk": blocked_time.id})
        modification_date = (
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=7)
        ).date()
        data = {
            "modification_start_date": modification_date.isoformat(),
            "is_cancelled": True,
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_204_NO_CONTENT)

        # Verify the mock was called with correct parameters
        mock_calendar_service.cancel_recurring_blocked_time_from_date.assert_called_once()

    def test_bulk_modify_recurring_blocked_time_with_rrule(self, auth_client, calendar, user):
        """Test bulk modifying recurring blocked time with custom recurrence rule"""
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
            reason="Weekly maintenance",
            recurrence_rule=recurrence_rule,
        )

        continuation_blocked_time = CalendarIntegrationTestFactory.create_blocked_time(
            calendar=calendar,
            reason="Modified maintenance",
        )

        mock_calendar_service = Mock()
        mock_calendar_service.modify_recurring_blocked_time_from_date.return_value = (
            continuation_blocked_time
        )

        url = reverse("api:BlockedTimes-bulk-modify", kwargs={"pk": blocked_time.id})
        modification_date = (
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=7)
        ).date()
        data = {
            "modification_start_date": modification_date.isoformat(),
            "modified_reason": "Modified maintenance",
            "rrule_string": "FREQ=DAILY;COUNT=5",
            "is_cancelled": False,
        }

        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_200_OK)
        mock_calendar_service.modify_recurring_blocked_time_from_date.assert_called_once()

    def test_bulk_modify_non_recurring_blocked_time(self, auth_client, calendar, user):
        """Test bulk modifying a non-recurring blocked time should fail"""
        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a non-recurring blocked time
        blocked_time = CalendarIntegrationTestFactory.create_blocked_time(
            calendar=calendar,
            reason="One-time maintenance",
        )

        url = reverse("api:BlockedTimes-bulk-modify", kwargs={"pk": blocked_time.id})
        modification_date = (
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=7)
        ).date()
        data = {
            "modification_start_date": modification_date.isoformat(),
            "is_cancelled": True,
        }

        response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "not recurring" in str(response.data)

    def test_bulk_modify_blocked_time_validation_errors(self, auth_client, calendar, user):
        """Test validation errors when bulk modifying recurring blocked times"""
        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a recurring blocked time
        recurrence_rule = CalendarIntegrationTestFactory.create_recurrence_rule(
            organization=user.organization_membership.organization,
            frequency=RecurrenceFrequency.WEEKLY,
        )

        blocked_time = CalendarIntegrationTestFactory.create_blocked_time(
            calendar=calendar,
            reason="Weekly maintenance",
            recurrence_rule=recurrence_rule,
        )

        url = reverse("api:BlockedTimes-bulk-modify", kwargs={"pk": blocked_time.id})

        # Test missing required fields
        response = auth_client.post(url, {}, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

        # Test conflicting recurrence rule fields
        modification_date = (
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=7)
        ).date()
        data = {
            "modification_start_date": modification_date.isoformat(),
            "recurrence_rule": {"frequency": "DAILY", "interval": 1},
            "rrule_string": "FREQ=WEEKLY;COUNT=5",
            "is_cancelled": False,
        }
        response = auth_client.post(url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "Cannot specify both recurrence_rule and rrule_string" in str(response.data)


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
            "timezone": "UTC",
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
            "timezone": "UTC",
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
                    "timezone": "UTC",
                },
                {
                    "calendar": calendar.id,
                    "start_time": (
                        datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=3)
                    ).isoformat(),
                    "end_time": (
                        datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=4)
                    ).isoformat(),
                    "timezone": "UTC",
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
            "start_time": "2024-01-01T00:00:00Z",
            "end_time": "2024-01-31T23:59:59Z",
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
            "timezone": "UTC",
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
        assert "Available time is not recurring" in str(response.data)

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

    def test_bulk_modify_recurring_available_time(self, auth_client, calendar, user):
        """Test bulk modifying recurring available time from a specific date"""
        from di_core.containers import container

        # Set up calendar to allow available windows management
        calendar.manage_available_windows = True
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

        # Create a mock calendar service and continuation available time
        mock_calendar_service = Mock()
        continuation_available_time = CalendarIntegrationTestFactory.create_available_time(
            calendar=calendar,
        )
        mock_calendar_service.modify_recurring_available_time_from_date.return_value = (
            continuation_available_time
        )

        url = reverse("api:AvailableTimes-bulk-modify", kwargs={"pk": available_time.id})
        modification_date = (
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=7)
        ).date()
        data = {
            "modification_start_date": modification_date.isoformat(),
            "modified_start_time_offset": "01:00:00",  # Move start time by 1 hour
            "modified_end_time_offset": "01:00:00",  # Move end time by 1 hour
            "is_cancelled": False,
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_200_OK)

        # Verify the mock was called with correct parameters
        mock_calendar_service.modify_recurring_available_time_from_date.assert_called_once()

    def test_bulk_cancel_recurring_available_time(self, auth_client, calendar, user):
        """Test bulk cancelling recurring available time from a specific date"""
        from di_core.containers import container

        # Set up calendar to allow available windows management
        calendar.manage_available_windows = True
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
        mock_calendar_service.cancel_recurring_available_time_from_date.return_value = None

        url = reverse("api:AvailableTimes-bulk-modify", kwargs={"pk": available_time.id})
        modification_date = (
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=7)
        ).date()
        data = {
            "modification_start_date": modification_date.isoformat(),
            "is_cancelled": True,
        }

        # Use container override to inject the mock service
        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_204_NO_CONTENT)

        # Verify the mock was called with correct parameters
        mock_calendar_service.cancel_recurring_available_time_from_date.assert_called_once()

    def test_bulk_modify_recurring_available_time_with_rrule(self, auth_client, calendar, user):
        """Test bulk modifying recurring available time with custom recurrence rule"""
        from di_core.containers import container

        # Set up calendar to allow available windows management
        calendar.manage_available_windows = True
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

        continuation_available_time = CalendarIntegrationTestFactory.create_available_time(
            calendar=calendar,
        )

        mock_calendar_service = Mock()
        mock_calendar_service.modify_recurring_available_time_from_date.return_value = (
            continuation_available_time
        )

        url = reverse("api:AvailableTimes-bulk-modify", kwargs={"pk": available_time.id})
        modification_date = (
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=7)
        ).date()
        data = {
            "modification_start_date": modification_date.isoformat(),
            "rrule_string": "FREQ=DAILY;COUNT=5",
            "is_cancelled": False,
        }

        with container.calendar_service.override(mock_calendar_service):
            response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_200_OK)
        mock_calendar_service.modify_recurring_available_time_from_date.assert_called_once()

    def test_bulk_modify_non_recurring_available_time(self, auth_client, calendar, user):
        """Test bulk modifying a non-recurring available time should fail"""
        # Set up calendar to allow available windows management
        calendar.manage_available_windows = True
        calendar.save()

        # Create calendar ownership
        CalendarIntegrationTestFactory.create_calendar_ownership(user, calendar)

        # Create a non-recurring available time
        available_time = CalendarIntegrationTestFactory.create_available_time(
            calendar=calendar,
        )

        url = reverse("api:AvailableTimes-bulk-modify", kwargs={"pk": available_time.id})
        modification_date = (
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=7)
        ).date()
        data = {
            "modification_start_date": modification_date.isoformat(),
            "is_cancelled": True,
        }

        response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "not recurring" in str(response.data)

    def test_bulk_modify_available_time_validation_errors(self, auth_client, calendar, user):
        """Test validation errors when bulk modifying recurring available times"""
        # Set up calendar to allow available windows management
        calendar.manage_available_windows = True
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

        url = reverse("api:AvailableTimes-bulk-modify", kwargs={"pk": available_time.id})

        # Test missing required fields
        response = auth_client.post(url, {}, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

        # Test conflicting recurrence rule fields
        modification_date = (
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=7)
        ).date()
        data = {
            "modification_start_date": modification_date.isoformat(),
            "recurrence_rule": {"frequency": "DAILY", "interval": 1},
            "rrule_string": "FREQ=WEEKLY;COUNT=5",
            "is_cancelled": False,
        }
        response = auth_client.post(url, data, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "Cannot specify both recurrence_rule and rrule_string" in str(response.data)


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
            "calendar": calendar.id,
            "reason": "Daily standup",
            "start_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
            ).isoformat(),
            "end_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)
            ).isoformat(),
            "timezone": "UTC",
            "rrule_string": "FREQ=DAILY;INTERVAL=1;COUNT=20;BYDAY=MO,TU,WE,TH,FR",
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
            "calendar": calendar.id,
            "start_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
            ).isoformat(),
            "end_time": (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)
            ).isoformat(),
            "timezone": "UTC",
            "rrule_string": "FREQ=WEEKLY;INTERVAL=1;COUNT=10;BYDAY=MO,WE,FR",
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
            "timezone": "UTC",
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


@pytest.mark.django_db
class TestCalendarBundleUpdateAction:
    """Tests for PATCH /calendar/{id}/bundle/ (update bundle children and primary)."""

    # --- Helpers ---

    @staticmethod
    def _make_bundle(organization):
        """Create a bundle calendar with two child calendars; return (bundle, child1, child2)."""
        child1 = CalendarIntegrationTestFactory.create_calendar(
            organization=organization,
            name="Child A",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.PERSONAL,
        )
        child2 = CalendarIntegrationTestFactory.create_calendar(
            organization=organization,
            name="Child B",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.PERSONAL,
        )
        bundle = CalendarIntegrationTestFactory.create_calendar(
            organization=organization,
            name="My Bundle",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.BUNDLE,
        )
        # Wire up relationships directly (bypass service for test setup)
        baker.make(
            ChildrenCalendarRelationship,
            bundle_calendar=bundle,
            child_calendar=child1,
            organization=organization,
            is_primary=True,
        )
        baker.make(
            ChildrenCalendarRelationship,
            bundle_calendar=bundle,
            child_calendar=child2,
            organization=organization,
            is_primary=False,
        )
        return bundle, child1, child2

    @staticmethod
    def _make_admin(organization):
        """Create an admin user and membership; return (user, APIClient)."""
        from rest_framework.test import APIClient

        admin = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=admin,
            organization=organization,
            role=OrganizationRole.ADMIN,
        )
        client = APIClient()
        client.force_authenticate(user=admin)
        return admin, client

    @staticmethod
    def _make_member(organization):
        """Create a regular member and return (user, APIClient)."""
        from rest_framework.test import APIClient

        member = baker.make(User)
        baker.make(
            OrganizationMembership,
            user=member,
            organization=organization,
            role=OrganizationRole.MEMBER,
        )
        client = APIClient()
        client.force_authenticate(user=member)
        return member, client

    # --- Happy-path ---

    def test_update_bundle_add_child_remove_child_change_primary(self, organization):
        """Admin adds a child, removes a child, and changes primary → 200, DB updated."""
        bundle, child1, child2 = self._make_bundle(organization)
        _, admin_client = self._make_admin(organization)

        child3 = CalendarIntegrationTestFactory.create_calendar(
            organization=organization,
            name="Child C",
            provider=CalendarProvider.INTERNAL,
            calendar_type=CalendarType.PERSONAL,
        )

        url = reverse("api:Calendars-bundle-update", kwargs={"pk": bundle.id})
        data = {
            "bundle_calendars": [child2.id, child3.id],  # drop child1, add child3
            "primary_calendar": child3.id,
        }

        response = admin_client.patch(url, data, format="json")

        assert_response_status_code(response, status.HTTP_200_OK)
        assert response.data["id"] == bundle.id

        # child1 should be gone
        assert not ChildrenCalendarRelationship.objects.filter(
            bundle_calendar=bundle,
            child_calendar_fk_id=child1.id,
            organization=organization,
        ).exists()

        # child2 retained, child3 added
        assert ChildrenCalendarRelationship.objects.filter(
            bundle_calendar=bundle,
            child_calendar_fk_id=child2.id,
            organization=organization,
        ).exists()
        assert ChildrenCalendarRelationship.objects.filter(
            bundle_calendar=bundle,
            child_calendar_fk_id=child3.id,
            organization=organization,
        ).exists()

        # Exactly one primary — child3
        assert (
            ChildrenCalendarRelationship.objects.filter(
                bundle_calendar=bundle,
                organization=organization,
                is_primary=True,
            ).count()
            == 1
        )
        assert (
            ChildrenCalendarRelationship.objects.get(
                bundle_calendar=bundle,
                organization=organization,
                is_primary=True,
            ).child_calendar_fk_id
            == child3.id
        )

    # --- Validation errors ---

    def test_update_bundle_non_bundle_calendar_400(self, organization):
        """PATCH on a PERSONAL calendar returns 400."""
        _, admin_client = self._make_admin(organization)
        personal = CalendarIntegrationTestFactory.create_calendar(
            organization=organization,
            calendar_type=CalendarType.PERSONAL,
        )
        child1 = CalendarIntegrationTestFactory.create_calendar(organization=organization)
        child2 = CalendarIntegrationTestFactory.create_calendar(organization=organization)

        url = reverse("api:Calendars-bundle-update", kwargs={"pk": personal.id})
        data = {"bundle_calendars": [child1.id, child2.id]}

        response = admin_client.patch(url, data, format="json")

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "not a bundle" in response.data["detail"].lower()

    def test_update_bundle_fewer_than_two_children_400(self, organization):
        """Providing only one child calendar returns 400."""
        bundle, child1, _child2 = self._make_bundle(organization)
        _, admin_client = self._make_admin(organization)

        url = reverse("api:Calendars-bundle-update", kwargs={"pk": bundle.id})
        data = {"bundle_calendars": [child1.id]}

        response = admin_client.patch(url, data, format="json")

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_update_bundle_primary_not_in_children_400(self, organization):
        """primary_calendar not in bundle_calendars returns 400."""
        bundle, child1, child2 = self._make_bundle(organization)
        _, admin_client = self._make_admin(organization)

        outside = CalendarIntegrationTestFactory.create_calendar(
            organization=organization,
            calendar_type=CalendarType.PERSONAL,
        )

        url = reverse("api:Calendars-bundle-update", kwargs={"pk": bundle.id})
        data = {
            "bundle_calendars": [child1.id, child2.id],
            "primary_calendar": outside.id,
        }

        response = admin_client.patch(url, data, format="json")

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_update_bundle_child_not_in_org_400(self, organization):
        """Child calendar from another org is rejected by PrimaryKeyRelatedField → 400."""
        bundle, child1, _child2 = self._make_bundle(organization)
        _, admin_client = self._make_admin(organization)

        other_org = CalendarIntegrationTestFactory.create_organization(name="Other Org")
        cross_org_calendar = CalendarIntegrationTestFactory.create_calendar(
            organization=other_org,
        )

        url = reverse("api:Calendars-bundle-update", kwargs={"pk": bundle.id})
        data = {
            "bundle_calendars": [child1.id, cross_org_calendar.id],
        }

        response = admin_client.patch(url, data, format="json")

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_update_bundle_disabled_child_calendar_400(self, organization):
        """Providing an is_active=False calendar as a child is rejected → 400."""
        bundle, child1, _child2 = self._make_bundle(organization)
        _, admin_client = self._make_admin(organization)

        disabled = CalendarIntegrationTestFactory.create_calendar(
            organization=organization,
            calendar_type=CalendarType.PERSONAL,
        )
        disabled.is_active = False
        disabled.save(update_fields=["is_active"])

        url = reverse("api:Calendars-bundle-update", kwargs={"pk": bundle.id})
        data = {"bundle_calendars": [child1.id, disabled.id]}

        response = admin_client.patch(url, data, format="json")

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    # --- Permission and access ---

    def test_update_bundle_non_admin_member_403(self, organization):
        """Regular member receives 403."""
        bundle, child1, child2 = self._make_bundle(organization)
        _, member_client = self._make_member(organization)

        url = reverse("api:Calendars-bundle-update", kwargs={"pk": bundle.id})
        data = {"bundle_calendars": [child1.id, child2.id]}

        response = member_client.patch(url, data, format="json")

        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_update_bundle_cross_org_bundle_404(self):
        """Bundle from a different org yields 404 via org-scoped get_queryset."""
        org_a = CalendarIntegrationTestFactory.create_organization(name="Org A")
        org_b = CalendarIntegrationTestFactory.create_organization(name="Org B")

        _, admin_client = self._make_admin(org_a)

        # Bundle lives in org_b
        _, child1, child2 = self._make_bundle(org_b)
        other_bundle = CalendarIntegrationTestFactory.create_calendar(
            organization=org_b,
            calendar_type=CalendarType.BUNDLE,
        )

        url = reverse("api:Calendars-bundle-update", kwargs={"pk": other_bundle.id})
        data = {"bundle_calendars": [child1.id, child2.id]}

        response = admin_client.patch(url, data, format="json")

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_update_bundle_anonymous_401(self, anonymous_client, organization):
        """Unauthenticated request returns 401."""
        bundle, _, _ = self._make_bundle(organization)

        url = reverse("api:Calendars-bundle-update", kwargs={"pk": bundle.id})
        data = {"bundle_calendars": [1, 2]}

        response = anonymous_client.patch(url, data, format="json")

        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)
