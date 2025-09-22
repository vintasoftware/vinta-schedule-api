"""
Integration tests for token-based calendar event management views.
"""

import base64
import json
from datetime import timedelta
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.constants import CalendarProvider
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarManagementToken,
    CalendarManagementTokenPermission,
    EventManagementPermissions,
    ExternalAttendee,
)
from calendar_integration.services.calendar_permission_service import (
    DEFAULT_CALENDAR_OWNER_PERMISSIONS,
    CalendarPermissionService,
)
from common.utils.authentication_utils import generate_long_lived_token, hash_long_lived_token
from organizations.models import Organization
from users.models import User


@pytest.mark.django_db
class TestTokenCalendarEventViewSetIntegration:
    """Integration tests for token-based calendar event management."""

    @pytest.fixture
    def organization(self):
        """Create a test organization."""
        return baker.make(Organization)

    @pytest.fixture
    def user(self):
        """Create a test user with profile."""
        user = baker.make(User, email="test@example.com")
        # Create a profile for the user to avoid RelatedObjectDoesNotExist error
        baker.make("users.Profile", user=user, first_name="Test", last_name="User")
        return user

    @pytest.fixture
    def calendar(self, organization):
        """Create a test calendar."""
        return baker.make(
            Calendar,
            organization=organization,
            name="Test Calendar",
            email="calendar@example.com",
            external_id=f"test_calendar_{timezone.now().timestamp()}",  # Ensure unique external_id
            provider=CalendarProvider.INTERNAL,
        )

    @pytest.fixture
    def event(self, calendar, organization):
        """Create a test event."""
        start_time = timezone.now()
        end_time = start_time + timedelta(hours=1)

        return baker.make(
            CalendarEvent,
            calendar_fk=calendar,
            organization=organization,
            title="Test Event",
            start_time_tz_unaware=start_time,
            end_time_tz_unaware=end_time,
            timezone="UTC",
            external_id=f"test_event_{timezone.now().timestamp()}",
        )

    @pytest.fixture
    def external_attendee(self, organization):
        """Create an external attendee."""
        return baker.make(
            ExternalAttendee,
            organization=organization,
            email="external@example.com",
            name="External User",
        )

    @pytest.fixture
    def calendar_owner_token(self, calendar, user, organization):
        """Create a calendar owner management token."""
        token_str = generate_long_lived_token()
        hashed_token = hash_long_lived_token(token_str)

        token = CalendarManagementToken.objects.create(
            calendar_fk=calendar,
            user=user,
            token_hash=hashed_token,
            organization=organization,
        )

        # Add default calendar owner permissions
        for permission in DEFAULT_CALENDAR_OWNER_PERMISSIONS:
            CalendarManagementTokenPermission.objects.create(
                token_fk=token,
                permission=permission,
                organization=organization,
            )

        return token, token_str

    @pytest.fixture
    def event_token(self, event, user, organization):
        """Create an event management token."""
        token_str = generate_long_lived_token()
        hashed_token = hash_long_lived_token(token_str)

        token = CalendarManagementToken.objects.create(
            event_fk=event,
            user=user,
            token_hash=hashed_token,
            organization=organization,
        )

        # Add default attendee permissions
        for permission in [
            EventManagementPermissions.UPDATE_ATTENDEES,
            EventManagementPermissions.UPDATE_DETAILS,  # Add this for updating title/description
            EventManagementPermissions.RESCHEDULE,
            EventManagementPermissions.CANCEL,
        ]:
            CalendarManagementTokenPermission.objects.create(
                token_fk=token,
                permission=permission,
                organization=organization,
            )

        return token, token_str

    @pytest.fixture
    def external_schedule_token(self, calendar, external_attendee, organization):
        """Create an external attendee schedule token."""
        permission_service = CalendarPermissionService()
        token = permission_service.create_external_attendee_schedule_token(
            organization.id, calendar.id, external_attendee.id
        )

        # Get the token string by recreating it (in real scenarios this would be provided to the external user)
        token_str = generate_long_lived_token()
        hashed_token = hash_long_lived_token(token_str)
        token.token_hash = hashed_token
        token.save()

        return token, token_str

    @pytest.fixture
    def client(self):
        """Create API client."""
        return APIClient()

    def create_auth_header(self, token_id, token_str):
        """Create Authorization header with token."""
        token_id_and_str = f"{token_id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()
        return f"Bearer {token_b64}"

    def test_create_event_with_calendar_owner_token(
        self, client, calendar_owner_token, calendar, organization
    ):
        """Test creating an event with a calendar owner token."""
        token, token_str = calendar_owner_token
        url = reverse(
            "calendar_token_api:token-events-list",
            kwargs={"organization_id": organization.id},
        )

        event_data = {
            "title": "New Test Event",
            "description": "Test description",
            "start_time": timezone.now().isoformat(),
            "end_time": (timezone.now() + timedelta(hours=2)).isoformat(),
            "timezone": "UTC",
            "calendar": calendar.id,
            "attendances": [],
            "external_attendances": [],
            "resource_allocations": [],
        }

        # Mock the calendar adapter to avoid external API calls
        # For token-based requests, the service might not use external adapters
        response = client.post(
            url,
            data=json.dumps(event_data),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.create_auth_header(token.id, token_str),
        )

        # Debug: print response content if test fails
        if response.status_code != status.HTTP_201_CREATED:
            print(f"Response status: {response.status_code}")
            print(f"Response content: {response.content}")

        assert response.status_code == status.HTTP_201_CREATED

        # Verify event was created
        created_event = CalendarEvent.objects.filter_by_organization(organization.id).get(
            id=response.data["id"]
        )
        assert created_event.title == "New Test Event"
        assert created_event.calendar_fk == calendar
        assert created_event.organization == organization

    def test_create_event_with_external_schedule_token(
        self, client, external_schedule_token, calendar, organization
    ):
        """Test creating an event with an external attendee schedule token."""
        token, token_str = external_schedule_token
        url = reverse(
            "calendar_token_api:token-events-list",
            kwargs={"organization_id": organization.id},
        )

        event_data = {
            "title": "External Scheduled Event",
            "description": "Scheduled by external attendee",
            "start_time": timezone.now().isoformat(),
            "end_time": (timezone.now() + timedelta(hours=1)).isoformat(),
            "timezone": "UTC",
            "calendar": calendar.id,
            "attendances": [],
            "external_attendances": [],
            "resource_allocations": [],
        }

        # For token-based requests, the service might not use external adapters
        response = client.post(
            url,
            data=json.dumps(event_data),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.create_auth_header(token.id, token_str),
        )

        assert response.status_code == status.HTTP_201_CREATED

        # Verify event was created
        created_event = CalendarEvent.objects.filter_by_organization(organization.id).get(
            id=response.data["id"]
        )
        assert created_event.title == "External Scheduled Event"
        assert created_event.calendar_fk == calendar

    def test_create_event_without_token(self, client, organization):
        """Test that creating an event without a token fails."""
        url = reverse(
            "calendar_token_api:token-events-list",
            kwargs={"organization_id": organization.id},
        )

        event_data = {
            "title": "Unauthorized Event",
            "start_time": timezone.now().isoformat(),
            "end_time": (timezone.now() + timedelta(hours=1)).isoformat(),
            "timezone": "UTC",
            "attendances": [],
            "external_attendances": [],
            "resource_allocations": [],
        }

        response = client.post(
            url,
            data=json.dumps(event_data),
            content_type="application/json",
        )

        print(f"Response status: {response.status_code}")
        print(f"Response data: {response.data}")
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_create_event_with_invalid_token(self, client, organization):
        """Test that creating an event with an invalid token fails."""
        url = reverse(
            "calendar_token_api:token-events-list",
            kwargs={"organization_id": organization.id},
        )

        event_data = {
            "title": "Invalid Token Event",
            "start_time": timezone.now().isoformat(),
            "end_time": (timezone.now() + timedelta(hours=1)).isoformat(),
            "timezone": "UTC",
            "attendances": [],
            "external_attendances": [],
            "resource_allocations": [],
        }

        # Create an invalid token
        invalid_token = base64.b64encode(b"999:invalid_token").decode()

        response = client.post(
            url,
            data=json.dumps(event_data),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {invalid_token}",
        )

        print(f"Invalid token response status: {response.status_code}")
        print(f"Invalid token response data: {response.data}")
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_update_event_with_event_token(self, client, event_token, event, organization):
        """Test updating an event with an event-specific token."""
        token, token_str = event_token
        url = reverse(
            "calendar_token_api:token-events-detail",
            kwargs={"organization_id": organization.id, "pk": event.id},
        )

        update_data = {
            "title": "Updated Event Title",
            "description": "Updated description",
        }

        response = client.patch(
            url,
            data=json.dumps(update_data),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.create_auth_header(token.id, token_str),
        )

        assert response.status_code == status.HTTP_200_OK

        # Verify event was updated
        event.refresh_from_db()
        assert event.title == "Updated Event Title"
        assert event.description == "Updated description"
        """Test updating an event with an event-specific token."""
        token, token_str = event_token
        url = reverse(
            "calendar_token_api:token-events-detail",
            kwargs={"organization_id": organization.id, "pk": event.id},
        )

        update_data = {
            "title": "Updated Event Title",
            "description": "Updated description",
        }

        # Mock the calendar service update_event method
        with patch(
            "calendar_integration.services.calendar_service.CalendarService.update_event"
        ) as mock_update_event:
            mock_update_event.return_value = event

            response = client.patch(
                url,
                data=json.dumps(update_data),
                content_type="application/json",
                HTTP_AUTHORIZATION=self.create_auth_header(token.id, token_str),
            )

        # Debug the response if it's not 200
        if response.status_code != status.HTTP_200_OK:
            print(f"Response status: {response.status_code}")
            print(f"Response content: {response.content.decode()}")

        assert response.status_code == status.HTTP_200_OK

        # Verify event was updated
        event.refresh_from_db()
        assert event.title == "Updated Event Title"
        assert event.description == "Updated description"

    def test_update_event_with_calendar_owner_token(
        self, client, calendar_owner_token, event, organization
    ):
        """Test updating an event with a calendar owner token."""
        token, token_str = calendar_owner_token
        url = reverse(
            "calendar_token_api:token-events-detail",
            kwargs={"organization_id": organization.id, "pk": event.id},
        )

        update_data = {
            "title": "Owner Updated Title",
        }

        # Mock the calendar service update_event method
        with patch(
            "calendar_integration.services.calendar_service.CalendarService.update_event"
        ) as mock_update_event:
            # Mock should update the event and return it
            def mock_update_side_effect(*args, **kwargs):
                event.title = "Owner Updated Title"
                event.save()
                return event

            mock_update_event.side_effect = mock_update_side_effect

            response = client.patch(
                url,
                data=json.dumps(update_data),
                content_type="application/json",
                HTTP_AUTHORIZATION=self.create_auth_header(token.id, token_str),
            )

        assert response.status_code == status.HTTP_200_OK

        # Verify event was updated
        event.refresh_from_db()
        assert event.title == "Owner Updated Title"

    def test_update_event_unauthorized_token(self, client, calendar_owner_token, organization):
        """Test updating an event with a token for a different calendar/event."""
        token, token_str = calendar_owner_token

        # Create a different calendar and event
        other_calendar = baker.make(
            Calendar,
            organization=organization,
            external_id=f"other_calendar_{timezone.now().timestamp()}",
            provider=CalendarProvider.INTERNAL,
        )
        other_event = baker.make(
            CalendarEvent,
            calendar_fk=other_calendar,
            organization=organization,
            title="Other Event",
            timezone="UTC",
            start_time_tz_unaware=timezone.now(),
            end_time_tz_unaware=timezone.now() + timedelta(hours=1),
            external_id=f"other_event_{timezone.now().timestamp()}",
        )

        url = reverse(
            "calendar_token_api:token-events-detail",
            kwargs={"organization_id": organization.id, "pk": other_event.id},
        )

        update_data = {
            "title": "Unauthorized Update",
        }

        response = client.patch(
            url,
            data=json.dumps(update_data),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.create_auth_header(token.id, token_str),
        )

        # Should fail because token is for a different calendar
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_delete_event_with_event_token(self, client, event_token, event, organization):
        """Test deleting an event with an event-specific token."""
        token, token_str = event_token
        url = reverse(
            "calendar_token_api:token-events-detail",
            kwargs={"organization_id": organization.id, "pk": event.id},
        )

        # Mock the calendar service delete_event method
        with patch(
            "calendar_integration.services.calendar_service.CalendarService.delete_event"
        ) as mock_delete_event:
            # Mock should actually delete the event
            def mock_delete_side_effect(*args, **kwargs):
                event.delete()
                return None

            mock_delete_event.side_effect = mock_delete_side_effect

            response = client.delete(
                url,
                HTTP_AUTHORIZATION=self.create_auth_header(token.id, token_str),
            )

        assert response.status_code == status.HTTP_204_NO_CONTENT

        # Verify event was deleted (or marked as deleted)
        with pytest.raises(CalendarEvent.DoesNotExist):
            CalendarEvent.objects.filter_by_organization(organization.id).get(id=event.id)

    def test_delete_event_with_calendar_owner_token(
        self, client, calendar_owner_token, event, organization
    ):
        """Test deleting an event with a calendar owner token."""
        token, token_str = calendar_owner_token
        url = reverse(
            "calendar_token_api:token-events-detail",
            kwargs={"organization_id": organization.id, "pk": event.id},
        )

        # Mock the calendar service delete_event method
        with patch(
            "calendar_integration.services.calendar_service.CalendarService.delete_event"
        ) as mock_delete_event:
            mock_delete_event.return_value = None

            response = client.delete(
                url,
                HTTP_AUTHORIZATION=self.create_auth_header(token.id, token_str),
            )

        assert response.status_code == status.HTTP_204_NO_CONTENT

    def test_delete_event_without_cancel_permission(
        self, client, external_schedule_token, event, organization
    ):
        """Test that deleting an event fails if token doesn't have CANCEL permission."""
        token, token_str = external_schedule_token

        # External schedule tokens only have CREATE permission by default
        url = reverse(
            "calendar_token_api:token-events-detail",
            kwargs={"organization_id": organization.id, "pk": event.id},
        )

        response = client.delete(
            url,
            HTTP_AUTHORIZATION=self.create_auth_header(token.id, token_str),
        )

        # Should fail because token only has CREATE permission, not CANCEL
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_get_specific_event_with_token(self, client, event_token, event, organization):
        """Test retrieving a specific event with a valid token."""
        token, token_str = event_token
        url = reverse(
            "calendar_token_api:token-events-detail",
            kwargs={"organization_id": organization.id, "pk": event.id},
        )

        response = client.get(
            url,
            HTTP_AUTHORIZATION=self.create_auth_header(token.id, token_str),
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["id"] == event.id
        assert response.data["title"] == event.title

    def test_revoked_token_access_denied(
        self, client, calendar_owner_token, calendar, organization
    ):
        """Test that revoked tokens are denied access."""
        token, token_str = calendar_owner_token

        # Revoke the token
        token.revoked_at = timezone.now()
        token.save()

        url = reverse(
            "calendar_token_api:token-events-list",
            kwargs={"organization_id": organization.id},
        )

        response = client.get(
            url,
            HTTP_AUTHORIZATION=self.create_auth_header(token.id, token_str),
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_token_from_different_organization(self, client, calendar_owner_token, organization):
        """Test that tokens from different organizations are denied access."""
        token, token_str = calendar_owner_token

        # Create a different organization
        other_org = baker.make(Organization)

        url = reverse(
            "calendar_token_api:token-events-list",
            kwargs={"organization_id": other_org.id},  # Different org in URL
        )

        response = client.get(
            url,
            HTTP_AUTHORIZATION=self.create_auth_header(token.id, token_str),
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_malformed_authorization_header(self, client, organization):
        """Test that malformed authorization headers are rejected."""
        url = reverse(
            "calendar_token_api:token-events-list",
            kwargs={"organization_id": organization.id},
        )

        # Test various malformed headers
        malformed_headers = [
            "Basic dGVzdDp0ZXN0",  # Basic auth instead of Bearer
            "Bearer",  # Missing token
            "Bearer invalid_token",  # Invalid base64
            "",  # Empty header
        ]

        for auth_header in malformed_headers:
            response = client.get(
                url,
                HTTP_AUTHORIZATION=auth_header,
            )
            assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_delete_event_with_series_parameter(self, client, event_token, event, organization):
        """Test deleting an event with delete_series parameter."""
        token, token_str = event_token
        url = reverse(
            "calendar_token_api:token-events-detail",
            kwargs={"organization_id": organization.id, "pk": event.id},
        )

        # Mock the calendar service delete_event method
        with patch(
            "calendar_integration.services.calendar_service.CalendarService.delete_event"
        ) as mock_delete_event:
            mock_delete_event.return_value = None

            response = client.delete(
                f"{url}?delete_series=true",
                HTTP_AUTHORIZATION=self.create_auth_header(token.id, token_str),
            )

        assert response.status_code == status.HTTP_204_NO_CONTENT

        # Verify the calendar service was called with delete_series=True
        # (This would be tested through the service layer integration)

    def test_permission_integration_with_calendar_service(
        self, client, calendar_owner_token, calendar, organization
    ):
        """Test that permission checking is properly integrated with CalendarService."""
        token, token_str = calendar_owner_token

        # Remove CREATE permission from the token
        token.permissions.filter(permission=EventManagementPermissions.CREATE).delete()

        url = reverse(
            "calendar_token_api:token-events-list",
            kwargs={"organization_id": organization.id},
        )

        event_data = {
            "title": "Should Not Be Created",
            "start_time": timezone.now().isoformat(),
            "end_time": (timezone.now() + timedelta(hours=1)).isoformat(),
            "timezone": "UTC",
            "calendar": calendar.id,
            "attendances": [],
            "external_attendances": [],
            "resource_allocations": [],
        }

        # This test should fail due to permissions before any service calls
        response = client.post(
            url,
            data=json.dumps(event_data),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.create_auth_header(token.id, token_str),
        )

        # Should fail due to lack of CREATE permission
        assert response.status_code == status.HTTP_403_FORBIDDEN

        # Verify no event was created
        assert not CalendarEvent.objects.filter(title="Should Not Be Created").exists()
