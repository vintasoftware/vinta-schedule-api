"""
Tests for Microsoft Calendar Webhook functionality (Phase 3) - Pytest Format
"""

import json
from unittest.mock import Mock, patch

from django.test import override_settings

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarProvider, IncomingWebhookProcessingStatus
from calendar_integration.exceptions import WebhookProcessingFailedError
from calendar_integration.models import Calendar, CalendarWebhookEvent
from calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter import (
    MSOutlookCalendarAdapter,
)
from calendar_integration.services.calendar_service import CalendarService
from organizations.models import Organization


@pytest.fixture
def ms_credentials():
    """Fixture for Microsoft credentials."""
    return {
        "token": "test_token",
        "refresh_token": "test_refresh_token",
    }


@pytest.fixture
def organization():
    """Fixture for organization."""
    return baker.make(Organization)


@pytest.fixture
def calendar(organization):
    """Fixture for Microsoft calendar."""
    return baker.make(
        Calendar,
        organization=organization,
        provider=CalendarProvider.MICROSOFT,
        external_id="test-calendar-123",
        name="Test Microsoft Calendar",
    )


# Microsoft Adapter Tests


@pytest.mark.django_db
@override_settings(MS_CLIENT_ID="test_client_id", MS_CLIENT_SECRET="test_client_secret")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_ms_adapter_parse_webhook_headers(mock_client, ms_credentials):
    """Test parsing of Microsoft webhook headers."""
    # Mock the API client test_connection method
    mock_client_instance = Mock()
    mock_client_instance.test_connection.return_value = True
    mock_client.return_value = mock_client_instance

    adapter = MSOutlookCalendarAdapter(ms_credentials)

    # Create mock headers
    headers = Mock()
    headers.get.side_effect = lambda key, default="": {
        "Content-Type": "application/json",
        "User-Agent": "Microsoft-Graph-Webhooks",
        "Host": "your-domain.com",
    }.get(key, default)

    result = adapter.parse_webhook_headers(headers)

    assert result["Content-Type"] == "application/json"
    assert result["User-Agent"] == "Microsoft-Graph-Webhooks"
    assert result["Host"] == "your-domain.com"


@pytest.mark.django_db
@override_settings(MS_CLIENT_ID="test_client_id", MS_CLIENT_SECRET="test_client_secret")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_ms_adapter_extract_calendar_id_from_request(mock_client, ms_credentials):
    """Test extraction of calendar ID from webhook request."""
    # Mock the API client test_connection method
    mock_client_instance = Mock()
    mock_client_instance.test_connection.return_value = True
    mock_client.return_value = mock_client_instance

    adapter = MSOutlookCalendarAdapter(ms_credentials)

    # Test with notification request
    request = Mock()
    request.GET.get.return_value = None  # No validation token
    request.body = json.dumps(
        {
            "value": [
                {
                    "subscriptionId": "test-sub-123",
                    "changeType": "created",
                    "resource": "/me/calendars/calendar123/events/event456",
                    "clientState": "test-client-state",
                }
            ]
        }
    ).encode()

    calendar_id = adapter.extract_calendar_external_id_from_webhook_request(request)
    assert calendar_id == "calendar123"

    # Test with primary calendar
    request.body = json.dumps(
        {
            "value": [
                {
                    "subscriptionId": "test-sub-123",
                    "changeType": "created",
                    "resource": "/me/events/event456",
                    "clientState": "test-client-state",
                }
            ]
        }
    ).encode()

    calendar_id = adapter.extract_calendar_external_id_from_webhook_request(request)
    assert calendar_id == "primary"


@pytest.mark.django_db
@override_settings(MS_CLIENT_ID="test_client_id", MS_CLIENT_SECRET="test_client_secret")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_ms_adapter_validate_webhook_notification_valid(mock_client, ms_credentials):
    """Test validation of valid Microsoft webhook notification."""
    # Mock the API client test_connection method
    mock_client_instance = Mock()
    mock_client_instance.test_connection.return_value = True
    mock_client.return_value = mock_client_instance

    adapter = MSOutlookCalendarAdapter(ms_credentials)

    headers = {"Content-Type": "application/json"}
    body = json.dumps(
        {
            "value": [
                {
                    "subscriptionId": "test-sub-123",
                    "changeType": "created",
                    "resource": "/me/calendars/calendar123/events/event456",
                    "clientState": "test-client-state",
                }
            ]
        }
    )

    result = adapter.validate_webhook_notification(headers, body)

    assert result["provider"] == "microsoft"
    assert result["calendar_id"] == "calendar123"
    assert result["event_type"] == "created"
    assert len(result["notifications"]) == 1

    notification = result["notifications"][0]
    assert notification["subscription_id"] == "test-sub-123"
    assert notification["change_type"] == "created"
    assert notification["calendar_id"] == "calendar123"
    assert notification["event_id"] == "event456"


@pytest.mark.django_db
@override_settings(MS_CLIENT_ID="test_client_id", MS_CLIENT_SECRET="test_client_secret")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_ms_adapter_validate_webhook_notification_missing_fields(mock_client, ms_credentials):
    """Test validation with missing required fields."""
    # Mock the API client test_connection method
    mock_client_instance = Mock()
    mock_client_instance.test_connection.return_value = True
    mock_client.return_value = mock_client_instance

    adapter = MSOutlookCalendarAdapter(ms_credentials)

    headers = {"Content-Type": "application/json"}
    body = json.dumps(
        {
            "value": [
                {
                    "subscriptionId": "test-sub-123",
                    # Missing changeType and resource
                    "clientState": "test-client-state",
                }
            ]
        }
    )

    with pytest.raises(
        WebhookProcessingFailedError, match="Missing required Microsoft webhook notification fields"
    ):
        adapter.validate_webhook_notification(headers, body)


@pytest.mark.django_db
@override_settings(MS_CLIENT_ID="test_client_id", MS_CLIENT_SECRET="test_client_secret")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_ms_adapter_validate_webhook_notification_invalid_json(mock_client, ms_credentials):
    """Test validation with invalid JSON."""
    # Mock the API client test_connection method
    mock_client_instance = Mock()
    mock_client_instance.test_connection.return_value = True
    mock_client.return_value = mock_client_instance

    adapter = MSOutlookCalendarAdapter(ms_credentials)

    headers = {"Content-Type": "application/json"}
    body = "invalid json"

    with pytest.raises(WebhookProcessingFailedError, match="Invalid JSON payload"):
        adapter.validate_webhook_notification(headers, body)


@pytest.mark.django_db
def test_ms_adapter_validate_webhook_notification_static():
    """Test static validation method."""
    headers = {"Content-Type": "application/json"}
    body = json.dumps(
        {
            "value": [
                {
                    "subscriptionId": "test-sub-123",
                    "changeType": "updated",
                    "resource": "/me/events/event456",
                    "clientState": "test-client-state",
                }
            ]
        }
    )

    result = MSOutlookCalendarAdapter.validate_webhook_notification_static(headers, body)

    assert result["provider"] == "microsoft"
    assert result["calendar_id"] == "primary"
    assert result["event_type"] == "updated"


@pytest.mark.django_db
@override_settings(MS_CLIENT_ID="test_client_id", MS_CLIENT_SECRET="test_client_secret")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_ms_adapter_create_webhook_subscription_with_tracking(mock_client, ms_credentials):
    """Test creating webhook subscription with tracking parameters."""
    # Mock the API client
    mock_client_instance = Mock()
    mock_client_instance.test_connection.return_value = True
    mock_client_instance.subscribe_to_calendar_events.return_value = {
        "id": "subscription-id-123",
        "resource": "/me/calendars/calendar123/events",
        "changeType": "created,updated,deleted",
    }
    mock_client.return_value = mock_client_instance

    adapter = MSOutlookCalendarAdapter(ms_credentials)

    result = adapter.create_webhook_subscription_with_tracking(
        resource_id="calendar123",
        callback_url="https://test.com/webhook",
        tracking_params={"client_state": "custom-state", "expiration_hours": 48},
    )

    assert result["subscription_id"] == "subscription-id-123"
    assert result["calendar_id"] == "calendar123"
    assert result["callback_url"] == "https://test.com/webhook"
    assert result["client_state"] == "custom-state"

    # Verify the API client was called correctly
    mock_client_instance.subscribe_to_calendar_events.assert_called_once()


# Calendar Service Tests


@pytest.mark.django_db
@patch(
    "calendar_integration.services.calendar_service.is_initialized_or_authenticated_calendar_service"
)
def test_calendar_service_request_webhook_triggered_sync_calendar_not_found(
    mock_auth_check, organization
):
    """Test webhook triggered sync when calendar is not found."""
    mock_auth_check.return_value = True

    service = CalendarService()
    service.organization = organization

    webhook_event = baker.make(
        CalendarWebhookEvent,
        organization=organization,
        provider=CalendarProvider.MICROSOFT,
    )

    result = service.request_webhook_triggered_sync(
        external_calendar_id="nonexistent-calendar",
        webhook_event=webhook_event,
    )

    assert result is None


@pytest.mark.django_db
@patch(
    "calendar_integration.services.calendar_service.is_initialized_or_authenticated_calendar_service"
)
@patch("calendar_integration.services.calendar_service.CalendarService.request_calendar_sync")
def test_calendar_service_request_webhook_triggered_sync_success(
    mock_sync, mock_auth_check, organization, calendar
):
    """Test successful webhook triggered sync."""
    mock_auth_check.return_value = True

    # Create a real CalendarSync instance
    from calendar_integration.models import CalendarSync

    calendar_sync = baker.make(CalendarSync, calendar=calendar, organization=organization)
    mock_sync.return_value = calendar_sync

    service = CalendarService()
    service.organization = organization

    webhook_event = baker.make(
        CalendarWebhookEvent,
        organization=organization,
        provider=CalendarProvider.MICROSOFT,
    )

    result = service.request_webhook_triggered_sync(
        external_calendar_id=calendar.external_id,
        webhook_event=webhook_event,
    )

    assert result == calendar_sync
    assert webhook_event.processing_status == IncomingWebhookProcessingStatus.PROCESSED
    assert webhook_event.calendar_sync == calendar_sync


# Webhook View Tests


@pytest.mark.django_db
def test_microsoft_webhook_view_validation_token(client, organization):
    """Test Microsoft webhook view with validation token."""
    url = f"/api/webhooks/microsoft-calendar/{organization.id}/"

    response = client.post(
        url + "?validationToken=123e4567-e89b-12d3-a456-426614174000",
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.content.decode() == "123e4567-e89b-12d3-a456-426614174000"


@pytest.mark.django_db
def test_microsoft_webhook_view_invalid_validation_token(client, organization):
    """Test Microsoft webhook view with invalid validation token."""
    url = f"/api/webhooks/microsoft-calendar/{organization.id}/"

    response = client.post(
        url + "?validationToken=<script>alert('xss')</script>", content_type="application/json"
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_microsoft_webhook_view_organization_not_found(client):
    """Test Microsoft webhook view with nonexistent organization."""
    url = "/api/webhooks/microsoft-calendar/99999/"

    response = client.post(url, content_type="application/json")

    assert response.status_code == 404


@pytest.mark.django_db
@patch("calendar_integration.services.calendar_service.CalendarService.handle_webhook")
def test_microsoft_webhook_view_notification_processing(
    mock_handle_webhook, client, organization, calendar
):
    """Test Microsoft webhook view notification processing."""
    mock_handle_webhook.return_value = Mock()

    url = f"/api/webhooks/microsoft-calendar/{organization.id}/"

    payload = {
        "value": [
            {
                "subscriptionId": "test-sub-123",
                "changeType": "created",
                "resource": "/me/calendars/calendar123/events/event456",
                "clientState": "test-client-state",
            }
        ]
    }

    response = client.post(url, data=json.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    mock_handle_webhook.assert_called_once_with(CalendarProvider.MICROSOFT, response.wsgi_request)
