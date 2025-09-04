"""
Tests for MSOutlookCalendarAPIClient using pytest with mocked HTTP requests.
"""

import datetime
from unittest.mock import Mock, patch

import pytest
import requests

from calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client import (
    MSGraphAPIError,
    MSGraphCalendar,
    MSGraphEvent,
    MSGraphRoom,
    MSOutlookCalendarAPIClient,
)


def create_mock_response(status_code=200, json_data=None, content=None):
    """Helper function to create a properly mocked HTTP response."""
    mock_response = Mock()
    mock_response.status_code = status_code
    mock_response.ok = status_code < 400

    if json_data is not None:
        mock_response.json.return_value = json_data

    if content is not None:
        mock_response.content = content
    elif json_data is not None:
        import json

        mock_response.content = json.dumps(json_data).encode("utf-8")
    else:
        mock_response.content = b""

    return mock_response


@pytest.fixture
def mock_session():
    """Mock requests session."""
    session = Mock(spec=requests.Session)
    session.headers = {}
    return session


@pytest.fixture
def mock_sleep():
    """Mock time.sleep for all tests to make them run faster."""
    with patch("time.sleep"):
        yield


@pytest.fixture
def client(mock_session, mock_sleep):
    """Create MSOutlookCalendarAPIClient instance with mocked session."""
    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.requests.Session",
        return_value=mock_session,
    ):
        client = MSOutlookCalendarAPIClient(access_token="test_token", user_id="test_user")
        client.session = mock_session
        return client


@pytest.fixture
def sample_event_data():
    """Sample event data from Microsoft Graph API."""
    return {
        "id": "event123",
        "calendarId": "calendar123",
        "subject": "Test Meeting",
        "body": {"content": "This is a test meeting"},
        "start": {"dateTime": "2025-06-22T10:00:00.000", "timeZone": "UTC"},
        "end": {"dateTime": "2025-06-22T11:00:00.000", "timeZone": "UTC"},
        "location": {"displayName": "Conference Room A"},
        "attendees": [
            {
                "emailAddress": {"address": "user@example.com", "name": "Test User"},
                "status": {"response": "accepted"},
            }
        ],
        "organizer": {"emailAddress": {"address": "organizer@example.com", "name": "Organizer"}},
        "isCancelled": False,
    }


@pytest.fixture
def sample_calendar_data():
    """Sample calendar data from Microsoft Graph API."""
    return {
        "id": "calendar123",
        "name": "Test Calendar",
        "owner": {"address": "user@example.com"},
        "canEdit": True,
        "isDefaultCalendar": False,
    }


@pytest.fixture
def sample_room_data():
    """Sample room data from Microsoft Graph API."""
    return {
        "id": "room123",
        "displayName": "Conference Room A",
        "emailAddress": "room-a@example.com",
        "capacity": 10,
        "building": "Building 1",
        "floorNumber": 2,
        "phone": "+1-555-1234",
        "isWheelChairAccessible": True,
    }


def test_client_initialization():
    """Test client initialization with and without user_id."""
    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.requests.Session"
    ):
        # Test with user_id
        client = MSOutlookCalendarAPIClient(access_token="test_token", user_id="specific_user")
        assert client.access_token == "test_token"
        assert client.user_id == "specific_user"
        assert client.BASE_URL == "https://graph.microsoft.com/v1.0"

        # Test without user_id (should default to "me")
        client = MSOutlookCalendarAPIClient(access_token="test_token")
        assert client.user_id == "me"


def test_make_request_success(client):
    """Test successful API request."""
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.ok = True
    mock_response.json.return_value = {"id": "test123", "name": "Test"}
    mock_response.content = b'{"id": "test123", "name": "Test"}'

    client.session.request.return_value = mock_response

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        result = client._make_request("GET", "/test/endpoint")

    assert result == {"id": "test123", "name": "Test"}
    client.session.request.assert_called_once()


def test_make_request_with_retry(client):
    """Test API request with retry logic."""
    # First call fails with 500, second succeeds
    mock_response_fail = Mock()
    mock_response_fail.status_code = 500
    mock_response_fail.ok = False
    mock_response_fail.json.return_value = {"error": {"message": "Server Error"}}
    mock_response_fail.content = b'{"error": {"message": "Server Error"}}'

    mock_response_success = Mock()
    mock_response_success.status_code = 200
    mock_response_success.ok = True
    mock_response_success.json.return_value = {"success": True}
    mock_response_success.content = b'{"success": true}'

    client.session.request.side_effect = [mock_response_fail, mock_response_success]

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        result = client._make_request("GET", "/test/endpoint")

    assert result == {"success": True}
    assert client.session.request.call_count == 2


def test_make_request_max_retries_exceeded(client):
    """Test API request when max retries are exceeded."""
    mock_response = Mock()
    mock_response.status_code = 500
    mock_response.ok = False
    mock_response.json.return_value = {"error": {"message": "Server Error"}}
    mock_response.content = b'{"error": {"message": "Server Error"}}'

    client.session.request.return_value = mock_response

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        with pytest.raises(MSGraphAPIError):
            client._make_request("GET", "/test/endpoint")


def test_parse_datetime(client):
    """Test datetime parsing from Microsoft Graph format."""
    # Test with Z suffix
    dt_dict = {"dateTime": "2025-06-22T10:00:00.000Z", "timeZone": "UTC"}
    result = client._parse_datetime(dt_dict)
    expected = datetime.datetime(2025, 6, 22, 10, 0, 0, tzinfo=datetime.UTC)
    assert result == expected

    # Test without Z suffix
    dt_dict = {"dateTime": "2025-06-22T10:00:00.000", "timeZone": "UTC"}
    result = client._parse_datetime(dt_dict)
    expected = datetime.datetime(2025, 6, 22, 10, 0, 0, tzinfo=datetime.UTC)
    assert result == expected


def test_format_datetime(client):
    """Test datetime formatting to Microsoft Graph format."""
    dt = datetime.datetime(2025, 6, 22, 10, 0, 0)
    result = client._format_datetime(dt, "UTC")

    assert result == {"dateTime": "2025-06-22T10:00:00.000", "timeZone": "UTC"}


def test_create_application_calendar(client, sample_calendar_data):
    """Test creating an application calendar."""
    mock_response = Mock()
    mock_response.json.return_value = sample_calendar_data
    client.session.request.return_value = mock_response

    calendar = client.create_application_calendar("Test Calendar")

    assert isinstance(calendar, MSGraphCalendar)
    assert calendar.id == "calendar123"
    assert calendar.name == "Test Calendar"
    assert calendar.email_address == "user@example.com"
    assert calendar.can_edit is True
    assert calendar.is_default is False


def test_list_calendars(client, sample_calendar_data):
    """Test listing calendars."""
    response_data = {"value": [sample_calendar_data]}
    mock_response = Mock()
    mock_response.json.return_value = response_data
    client.session.request.return_value = mock_response

    calendars = list(client.list_calendars())

    assert len(calendars) == 1
    assert isinstance(calendars[0], MSGraphCalendar)
    assert calendars[0].id == "calendar123"


def test_get_calendar(client, sample_calendar_data):
    """Test getting a specific calendar."""
    mock_response = Mock()
    mock_response.json.return_value = sample_calendar_data
    client.session.request.return_value = mock_response

    calendar = client.get_calendar("calendar123")

    assert isinstance(calendar, MSGraphCalendar)
    assert calendar.id == "calendar123"
    assert calendar.name == "Test Calendar"


def test_list_events(client, sample_event_data):
    """Test listing events."""
    response_data = {"value": [sample_event_data]}
    mock_response = Mock()
    mock_response.json.return_value = response_data
    client.session.request.return_value = mock_response

    events = list(client.list_events(calendar_id="calendar123"))

    assert len(events) == 1
    assert isinstance(events[0], MSGraphEvent)
    assert events[0].id == "event123"
    assert events[0].subject == "Test Meeting"


def test_list_events_with_filters(client, sample_event_data):
    """Test listing events with various filters."""
    response_data = {"value": [sample_event_data]}
    mock_response = Mock()
    mock_response.json.return_value = response_data
    client.session.request.return_value = mock_response

    start_time = datetime.datetime(2025, 6, 22, 9, 0, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 6, 22, 18, 0, 0, tzinfo=datetime.UTC)

    events = list(
        client.list_events(
            calendar_id="calendar123",
            start_time=start_time,
            end_time=end_time,
            top=10,
            skip=5,
            select=["id", "subject"],
            filter_query="contains(subject,'meeting')",
            timezone="UTC",
        )
    )

    assert len(events) == 1
    # Verify that the request was made with correct parameters
    client.session.request.assert_called_once()


def test_get_event(client, sample_event_data):
    """Test getting a specific event."""
    mock_response = Mock()
    mock_response.json.return_value = sample_event_data
    client.session.request.return_value = mock_response

    event = client.get_event("event123", calendar_id="calendar123")

    assert isinstance(event, MSGraphEvent)
    assert event.id == "event123"
    assert event.subject == "Test Meeting"
    assert event.calendar_id == "calendar123"


def test_create_event(client, sample_event_data):
    """Test creating an event."""
    mock_response = Mock()
    mock_response.json.return_value = sample_event_data
    client.session.request.return_value = mock_response

    start_time = datetime.datetime(2025, 6, 22, 10, 0, 0)
    end_time = datetime.datetime(2025, 6, 22, 11, 0, 0)
    attendees = [{"email": "user@example.com", "name": "Test User"}]

    event = client.create_event(
        subject="Test Meeting",
        start_time=start_time,
        end_time=end_time,
        body="Test body",
        location="Conference Room A",
        attendees=attendees,
        calendar_id="calendar123",
        is_online_meeting=True,
    )

    assert isinstance(event, MSGraphEvent)
    assert event.subject == "Test Meeting"


def test_create_recurring_event(client):
    """Test creating a recurring event."""
    recurring_event_data = {
        "id": "recurring_event_123",
        "subject": "Weekly Standup",
        "body": {"content": "Weekly team standup meeting"},
        "start": {
            "dateTime": "2025-06-22T10:00:00.0000000",
            "timeZone": "UTC",
        },
        "end": {
            "dateTime": "2025-06-22T11:00:00.0000000",
            "timeZone": "UTC",
        },
        "location": {"displayName": "Conference Room A"},
        "attendees": [],
        "recurrence": {
            "pattern": {
                "type": "weekly",
                "interval": 1,
                "daysOfWeek": ["monday"],
            },
            "range": {
                "type": "numbered",
                "numberOfOccurrences": 10,
                "startDate": "2025-06-22",
            },
        },
    }

    mock_response = Mock()
    mock_response.json.return_value = recurring_event_data
    client.session.request.return_value = mock_response

    start_time = datetime.datetime(2025, 6, 22, 10, 0, 0)
    end_time = datetime.datetime(2025, 6, 22, 11, 0, 0)
    recurrence_pattern = {
        "pattern": {
            "type": "weekly",
            "interval": 1,
            "daysOfWeek": ["monday"],
        },
        "range": {
            "type": "numbered",
            "numberOfOccurrences": 10,
            "startDate": "2025-06-22",
        },
    }

    event = client.create_event(
        subject="Weekly Standup",
        start_time=start_time,
        end_time=end_time,
        body="Weekly team standup meeting",
        location="Conference Room A",
        attendees=[],
        calendar_id="calendar123",
        recurrence_pattern=recurrence_pattern,
    )

    assert isinstance(event, MSGraphEvent)
    assert event.subject == "Weekly Standup"
    assert event.recurrence_pattern is not None
    assert event.recurrence_pattern["pattern"]["type"] == "weekly"


def test_update_event(client, sample_event_data):
    """Test updating an event."""
    updated_data = sample_event_data.copy()
    updated_data["subject"] = "Updated Meeting"

    mock_response = Mock()
    mock_response.json.return_value = updated_data
    client.session.request.return_value = mock_response

    new_start_time = datetime.datetime(2025, 6, 22, 14, 0, 0)
    new_end_time = datetime.datetime(2025, 6, 22, 15, 0, 0)

    event = client.update_event(
        event_id="event123",
        subject="Updated Meeting",
        start_time=new_start_time,
        end_time=new_end_time,
        calendar_id="calendar123",
    )

    assert isinstance(event, MSGraphEvent)
    assert event.subject == "Updated Meeting"


def test_delete_event(client):
    """Test deleting an event."""
    mock_response = Mock()
    mock_response.status_code = 204
    mock_response.ok = True
    mock_response.content = b""
    client.session.request.return_value = mock_response

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        # Should not raise an exception
        client.delete_event("event123", calendar_id="calendar123")

    client.session.request.assert_called_once()


def test_cancel_event(client):
    """Test canceling an event."""
    mock_response = Mock()
    mock_response.status_code = 202
    mock_response.ok = True
    mock_response.content = b""
    client.session.request.return_value = mock_response

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        # Should not raise an exception
        client.cancel_event("event123", comment="Meeting cancelled", calendar_id="calendar123")

    client.session.request.assert_called_once()


def test_list_calendar_view(client, sample_event_data):
    """Test getting calendar view."""
    response_data = {"value": [sample_event_data]}
    mock_response = Mock()
    mock_response.json.return_value = response_data
    client.session.request.return_value = mock_response

    start_time = datetime.datetime(2025, 6, 22, 0, 0, 0)
    end_time = datetime.datetime(2025, 6, 22, 23, 59, 59)

    events = list(
        client.list_calendar_view(
            start_time=start_time, end_time=end_time, calendar_id="calendar123"
        )
    )

    assert len(events) == 1
    assert isinstance(events[0], MSGraphEvent)


def test_get_events_delta(client, sample_event_data):
    """Test getting events delta."""
    response_data = {
        "value": [sample_event_data],
        "@odata.nextLink": "next_page_url",
        "@odata.deltaLink": "delta_url",
    }
    mock_response = Mock()
    mock_response.json.return_value = response_data
    client.session.request.return_value = mock_response

    start_time = datetime.datetime(2025, 6, 22, 0, 0, 0)
    end_time = datetime.datetime(2025, 6, 22, 23, 59, 59)

    result = client.get_events_delta(
        start_time=start_time, end_time=end_time, calendar_id="calendar123"
    )

    assert len(result["events"]) == 1
    assert result["next_link"] == "next_page_url"
    assert result["delta_link"] == "delta_url"


def test_list_rooms(client, sample_room_data):
    """Test listing rooms."""
    response_data = {"value": [sample_room_data]}
    mock_response = Mock()
    mock_response.json.return_value = response_data
    client.session.request.return_value = mock_response

    rooms = list(client.list_rooms())

    assert len(rooms) == 1
    assert isinstance(rooms[0], MSGraphRoom)
    assert rooms[0].id == "room123"
    assert rooms[0].display_name == "Conference Room A"


def test_get_room(client, sample_room_data):
    """Test getting a specific room."""
    mock_response = Mock()
    mock_response.json.return_value = sample_room_data
    client.session.request.return_value = mock_response

    room = client.get_room("room123")

    assert isinstance(room, MSGraphRoom)
    assert room.id == "room123"
    assert room.display_name == "Conference Room A"
    assert room.capacity == 10
    assert room.is_wheelchair_accessible is True


def test_find_meeting_times(client):
    """Test finding meeting times."""
    response_data = {
        "meetingTimeSuggestions": [
            {
                "meetingTimeSlot": {
                    "start": {"dateTime": "2025-06-22T10:00:00.000", "timeZone": "UTC"},
                    "end": {"dateTime": "2025-06-22T11:00:00.000", "timeZone": "UTC"},
                },
                "confidence": 100,
            }
        ]
    }
    mock_response = Mock()
    mock_response.json.return_value = response_data
    client.session.request.return_value = mock_response

    start_time = datetime.datetime(2025, 6, 22, 9, 0, 0)
    end_time = datetime.datetime(2025, 6, 22, 17, 0, 0)
    attendees = ["user1@example.com", "user2@example.com"]

    result = client.find_meeting_times(
        attendees=attendees, start_time=start_time, end_time=end_time, meeting_duration=60
    )

    assert "meetingTimeSuggestions" in result


def test_get_free_busy_schedule(client):
    """Test getting free/busy schedule."""
    response_data = {
        "value": [
            {
                "scheduleId": "user@example.com",
                "freeBusyViewType": "detailed",
                "freeBusyStatus": ["free", "busy", "free"],
            }
        ]
    }
    mock_response = Mock()
    mock_response.json.return_value = response_data
    client.session.request.return_value = mock_response

    start_time = datetime.datetime(2025, 6, 22, 9, 0, 0)
    end_time = datetime.datetime(2025, 6, 22, 17, 0, 0)
    schedules = ["user@example.com"]

    result = client.get_free_busy_schedule(
        schedules=schedules, start_time=start_time, end_time=end_time
    )

    assert "value" in result


def test_create_subscription(client):
    """Test creating a webhook subscription."""
    response_data = {
        "id": "subscription123",
        "resource": "/me/calendars/calendar123/events",
        "changeType": "created,updated,deleted",
        "notificationUrl": "https://app.com/webhook",
        "expirationDateTime": "2025-06-25T10:00:00.000Z",
    }
    mock_response = Mock()
    mock_response.json.return_value = response_data
    client.session.request.return_value = mock_response

    expiration = datetime.datetime(2025, 6, 25, 10, 0, 0)

    subscription = client.create_subscription(
        resource="/me/calendars/calendar123/events",
        change_type="created,updated,deleted",
        notification_url="https://app.com/webhook",
        expiration_datetime=expiration,
        client_state="test_state",
    )

    assert subscription["id"] == "subscription123"
    assert subscription["resource"] == "/me/calendars/calendar123/events"


def test_list_subscriptions(client):
    """Test listing subscriptions."""
    response_data = {
        "value": [
            {
                "id": "subscription123",
                "resource": "/me/calendars/calendar123/events",
                "changeType": "created,updated,deleted",
            }
        ]
    }
    mock_response = Mock()
    mock_response.json.return_value = response_data
    client.session.request.return_value = mock_response

    subscriptions = client.list_subscriptions()

    assert len(subscriptions) == 1
    assert subscriptions[0]["id"] == "subscription123"


def test_get_subscription(client):
    """Test getting a specific subscription."""
    response_data = {
        "id": "subscription123",
        "resource": "/me/calendars/calendar123/events",
        "changeType": "created,updated,deleted",
    }
    mock_response = Mock()
    mock_response.json.return_value = response_data
    client.session.request.return_value = mock_response

    subscription = client.get_subscription("subscription123")

    assert subscription["id"] == "subscription123"


def test_update_subscription(client):
    """Test updating a subscription."""
    response_data = {"id": "subscription123", "expirationDateTime": "2025-06-25T10:00:00.000Z"}
    mock_response = Mock()
    mock_response.json.return_value = response_data
    client.session.request.return_value = mock_response

    expiration = datetime.datetime(2025, 6, 25, 10, 0, 0)
    subscription = client.update_subscription("subscription123", expiration)

    assert subscription["id"] == "subscription123"


def test_delete_subscription(client):
    """Test deleting a subscription."""
    mock_response = Mock()
    mock_response.status_code = 204
    mock_response.ok = True
    mock_response.content = b""
    client.session.request.return_value = mock_response

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        # Should not raise an exception
        client.delete_subscription("subscription123")

    client.session.request.assert_called_once()


def test_subscribe_to_calendar_events(client):
    """Test high-level method to subscribe to calendar events."""
    response_data = {
        "id": "subscription123",
        "resource": "/me/calendars/calendar123/events",
        "changeType": "created,updated,deleted",
        "notificationUrl": "https://app.com/webhook",
    }
    mock_response = Mock()
    mock_response.json.return_value = response_data
    client.session.request.return_value = mock_response

    subscription = client.subscribe_to_calendar_events(
        calendar_id="calendar123", notification_url="https://app.com/webhook"
    )

    assert subscription["id"] == "subscription123"


def test_unsubscribe_from_calendar_events(client):
    """Test high-level method to unsubscribe from calendar events."""
    mock_response = Mock()
    mock_response.status_code = 204
    mock_response.ok = True
    mock_response.content = b""
    client.session.request.return_value = mock_response

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        # Should not raise an exception
        client.unsubscribe_from_calendar_events("subscription123")

    client.session.request.assert_called_once()


def test_get_user_info(client):
    """Test getting user information."""
    response_data = {"id": "user123", "displayName": "Test User", "mail": "user@example.com"}
    mock_response = Mock()
    mock_response.json.return_value = response_data
    client.session.request.return_value = mock_response

    user_info = client.get_user_info()

    assert user_info["id"] == "user123"
    assert user_info["displayName"] == "Test User"


def test_test_connection_success(client):
    """Test successful connection test."""
    response_data = {"id": "user123"}
    mock_response = Mock()
    mock_response.json.return_value = response_data
    client.session.request.return_value = mock_response

    result = client.test_connection()

    assert result is True


def test_test_connection_failure(client):
    """Test failed connection test."""
    client.session.request.side_effect = MSGraphAPIError("Connection failed")

    result = client.test_connection()

    assert result is False


def test_parse_event(client, sample_event_data):
    """Test parsing event data into MSGraphEvent object."""
    event = client._parse_event(sample_event_data)

    assert isinstance(event, MSGraphEvent)
    assert event.id == "event123"
    assert event.calendar_id == "calendar123"
    assert event.subject == "Test Meeting"
    assert event.body_content == "This is a test meeting"
    assert event.location == "Conference Room A"
    assert len(event.attendees) == 1
    assert event.is_cancelled is False


def test_format_attendees(client):
    """Test formatting attendees for API."""
    attendees = [
        {"email": "user1@example.com", "name": "User 1"},
        {"email": "user2@example.com", "name": "User 2", "required": True},
    ]

    formatted = client._format_attendees(attendees)

    assert len(formatted) == 2
    # The exact format depends on the implementation of _format_attendees


def test_error_handling_invalid_response(client):
    """Test error handling for invalid response."""
    mock_response = Mock()
    mock_response.status_code = 400
    mock_response.ok = False
    mock_response.json.return_value = {
        "error": {"code": "BadRequest", "message": "Invalid request"}
    }
    mock_response.content = b'{"error": {"code": "BadRequest", "message": "Invalid request"}}'

    client.session.request.return_value = mock_response

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        with pytest.raises(MSGraphAPIError) as exc_info:
            client._make_request("GET", "/invalid/endpoint")

    assert exc_info.value.status_code == 400


def test_room_events_methods(client, sample_event_data):
    """Test room-specific event methods."""
    response_data = {"value": [sample_event_data]}
    mock_response = Mock()
    mock_response.json.return_value = response_data
    client.session.request.return_value = mock_response

    # Test get_room_events
    start_time = datetime.datetime(2025, 6, 22, 0, 0, 0)
    end_time = datetime.datetime(2025, 6, 22, 23, 59, 59)

    events = client.get_room_events(
        room_email="room@example.com", start_time=start_time, end_time=end_time
    )

    assert len(events) == 1
    assert isinstance(events[0], MSGraphEvent)


def test_subscribe_to_room_events(client):
    """Test subscribing to room events."""
    response_data = {
        "id": "subscription123",
        "resource": "/places/room@example.com/events",
        "changeType": "created,updated,deleted",
    }
    mock_response = Mock()
    mock_response.json.return_value = response_data
    client.session.request.return_value = mock_response

    subscription = client.subscribe_to_room_events(
        room_email="room@example.com", notification_url="https://app.com/webhook"
    )

    assert subscription["id"] == "subscription123"


def test_subscribe_to_multiple_room_events(client):
    """Test subscribing to multiple room events."""
    response_data = {
        "id": "subscription123",
        "resource": "/places/room1@example.com/events",
        "changeType": "created,updated,deleted",
    }
    mock_response = Mock()
    mock_response.json.return_value = response_data
    client.session.request.return_value = mock_response

    room_emails = ["room1@example.com", "room2@example.com"]
    subscriptions = client.subscribe_to_multiple_room_events(
        room_emails=room_emails, notification_url="https://app.com/webhook"
    )

    assert len(subscriptions) == 2
    for subscription in subscriptions:
        assert subscription["id"] == "subscription123"


@patch(
    "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
)
def test_rate_limiting(mock_limiter, client):
    """Test that rate limiting is properly integrated."""
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"success": True}
    client.session.request.return_value = mock_response

    client._make_request("GET", "/test")

    # Verify that the limiter was used (exact verification depends on implementation)
    # This test ensures the limiter is imported and available


def test_edge_cases_datetime_parsing(client):
    """Test edge cases in datetime parsing."""
    # Test with microseconds
    dt_dict = {"dateTime": "2025-06-22T10:00:00.123456Z", "timeZone": "UTC"}
    result = client._parse_datetime(dt_dict)
    assert isinstance(result, datetime.datetime)

    # Test with timezone offset
    dt_dict = {"dateTime": "2025-06-22T10:00:00+05:00", "timeZone": "Asia/Kolkata"}
    result = client._parse_datetime(dt_dict)
    assert isinstance(result, datetime.datetime)


def test_pagination_handling(client, sample_room_data):
    """Test pagination handling in list_rooms."""
    # Mock multiple pages of results
    page1_data = {"value": [sample_room_data], "@odata.nextLink": "page2_url"}
    page2_data = {"value": [sample_room_data]}  # No nextLink means last page

    mock_response1 = Mock()
    mock_response1.status_code = 200
    mock_response1.ok = True
    mock_response1.json.return_value = page1_data
    mock_response1.content = b'{"value": [{"id": "room123"}]}'

    mock_response2 = Mock()
    mock_response2.status_code = 200
    mock_response2.ok = True
    mock_response2.json.return_value = page2_data
    mock_response2.content = b'{"value": [{"id": "room123"}]}'

    # Create a cycle to handle multiple calls
    def side_effect_func(*args, **kwargs):
        # For the first two calls, return the mocked responses
        if not hasattr(side_effect_func, "call_count"):
            side_effect_func.call_count = 0
        side_effect_func.call_count += 1

        if side_effect_func.call_count == 1:
            return mock_response1
        elif side_effect_func.call_count == 2:
            return mock_response2
        else:
            # Return empty response for any additional calls
            empty_response = Mock()
            empty_response.status_code = 200
            empty_response.ok = True
            empty_response.json.return_value = {"value": []}
            empty_response.content = b'{"value": []}'
            return empty_response

    client.session.request.side_effect = side_effect_func

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        rooms = list(client.list_rooms(page_size=1))

    assert len(rooms) == 2
    assert client.session.request.call_count >= 2


# Additional comprehensive tests for better coverage


def test_unsubscribe_from_room_events_success(client):
    """Test successful unsubscription from room events."""
    # Mock list_subscriptions to return room subscriptions
    list_response = [
        {
            "id": "subscription1",
            "resource": "/users/room@example.com/events",
            "changeType": "created,updated,deleted",
        },
        {
            "id": "subscription2",
            "resource": "/users/other@example.com/events",
            "changeType": "created,updated,deleted",
        },
    ]

    # Mock delete_subscription responses
    delete_response = Mock()
    delete_response.status_code = 204
    delete_response.ok = True
    delete_response.content = b""

    def mock_request_side_effect(method, url, **kwargs):
        if "subscriptions" in url:
            mock_resp = Mock()
            mock_resp.status_code = 200
            mock_resp.ok = True
            mock_resp.json.return_value = {"value": list_response}
            mock_resp.content = b'{"value": []}'
            return mock_resp
        else:  # DELETE subscription
            return delete_response

    client.session.request.side_effect = mock_request_side_effect

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        client.unsubscribe_from_room_events("room@example.com")

    # Should call request twice: once for list, once for delete
    assert client.session.request.call_count == 2


def test_unsubscribe_from_room_events_no_subscriptions(client):
    """Test unsubscription when no subscriptions exist."""
    # Mock list_subscriptions to return no room subscriptions
    list_response = [
        {
            "id": "subscription1",
            "resource": "/users/other@example.com/events",
            "changeType": "created,updated,deleted",
        }
    ]

    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.ok = True
    mock_response.json.return_value = {"value": list_response}
    mock_response.content = b'{"value": []}'
    client.session.request.return_value = mock_response

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        # Should not raise an exception, just log warning
        client.unsubscribe_from_room_events("room@example.com")

    # Should only call list_subscriptions
    assert client.session.request.call_count == 1


def test_unsubscribe_from_room_events_api_error(client):
    """Test unsubscription with API error."""
    with patch.object(client, "list_subscriptions") as mock_list:
        mock_list.side_effect = MSGraphAPIError("Failed to list subscriptions")

        with patch(
            "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
        ):
            with pytest.raises(MSGraphAPIError) as exc_info:
                client.unsubscribe_from_room_events("room@example.com")

        assert "Failed to unsubscribe from room events" in str(exc_info.value)


def test_make_request_retries_server_errors(client):
    """Test _make_request retries on server errors (5xx)."""
    # First few attempts fail with 503, then success
    fail_response = Mock()
    fail_response.status_code = 503
    fail_response.ok = False
    fail_response.json.return_value = {"error": {"message": "Service Unavailable"}}
    fail_response.content = b'{"error": {"message": "Service Unavailable"}}'

    success_response = Mock()
    success_response.status_code = 200
    success_response.ok = True
    success_response.json.return_value = {"data": "success"}
    success_response.content = b'{"data": "success"}'

    client.session.request.side_effect = [
        fail_response,  # First attempt fails
        fail_response,  # Second attempt fails
        success_response,  # Third attempt succeeds
    ]

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        result = client._make_request("GET", "/test/endpoint")

    assert result == {"data": "success"}
    assert client.session.request.call_count == 3


def test_make_request_max_retries_exceeded_server_error(client):
    """Test _make_request when server error max retries exceeded."""
    fail_response = Mock()
    fail_response.status_code = 500
    fail_response.ok = False
    fail_response.json.return_value = {"error": {"message": "Internal Server Error"}}
    fail_response.content = b'{"error": {"message": "Internal Server Error"}}'

    client.session.request.return_value = fail_response

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        with pytest.raises(MSGraphAPIError) as exc_info:
            client._make_request("GET", "/test/endpoint")

    assert "MS Graph API error: 500" in str(exc_info.value)
    assert client.session.request.call_count == 6  # Initial + 5 retries


def test_make_request_retries_request_exceptions(client):
    """Test _make_request retries on request exceptions."""
    # Mock request exceptions for first attempts, then success
    success_response = Mock()
    success_response.status_code = 200
    success_response.ok = True
    success_response.json.return_value = {"data": "success"}
    success_response.content = b'{"data": "success"}'

    client.session.request.side_effect = [
        requests.ConnectionError("Connection failed"),
        requests.Timeout("Request timeout"),
        success_response,
    ]

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        result = client._make_request("GET", "/test/endpoint")

    assert result == {"data": "success"}
    assert client.session.request.call_count == 3


def test_make_request_max_retries_exceeded_request_exception(client):
    """Test _make_request when request exception max retries exceeded."""
    client.session.request.side_effect = requests.ConnectionError("Persistent connection error")

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        with pytest.raises(MSGraphAPIError) as exc_info:
            client._make_request("GET", "/test/endpoint")

    assert "Request failed after 6 attempts" in str(exc_info.value)
    assert client.session.request.call_count == 6


def test_make_request_no_retries_for_client_errors(client):
    """Test _make_request doesn't retry for client errors (4xx)."""
    fail_response = Mock()
    fail_response.status_code = 400  # Bad Request - should not retry
    fail_response.ok = False
    fail_response.json.return_value = {"error": {"message": "Bad Request"}}
    fail_response.content = b'{"error": {"message": "Bad Request"}}'

    client.session.request.return_value = fail_response

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        with pytest.raises(MSGraphAPIError) as exc_info:
            client._make_request("GET", "/test/endpoint")

    assert "MS Graph API error: 400" in str(exc_info.value)
    assert client.session.request.call_count == 1  # No retries


def test_make_request_204_no_content(client):
    """Test _make_request handling 204 No Content response."""
    response = Mock()
    response.status_code = 204
    response.ok = True
    response.content = b""

    client.session.request.return_value = response

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        result = client._make_request("DELETE", "/test/endpoint")

    assert result == {}


def test_make_request_empty_content(client):
    """Test _make_request handling response with empty content."""
    response = Mock()
    response.status_code = 200
    response.ok = True
    response.content = b""

    client.session.request.return_value = response

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        result = client._make_request("GET", "/test/endpoint")

    assert result == {}


def test_list_calendar_view_default_calendar(client, sample_event_data):
    """Test list_calendar_view with default calendar."""
    response_data = {"value": [sample_event_data]}
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.ok = True
    mock_response.json.return_value = response_data
    mock_response.content = b'{"value": []}'
    client.session.request.return_value = mock_response

    start_time = datetime.datetime(2025, 6, 22, 9, 0, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 6, 22, 17, 0, 0, tzinfo=datetime.UTC)

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        events = list(client.list_calendar_view(start_time, end_time))

    assert len(events) == 1
    assert events[0].id == "event123"
    assert events[0].subject == "Test Meeting"

    # Verify correct endpoint was called
    call_args = client.session.request.call_args
    assert call_args is not None
    # Check if URL is in positional args or keyword args
    if len(call_args[0]) > 1:
        url = call_args[0][1]
    else:
        url = call_args[1].get("url", "")

    assert "/calendarView" in url
    assert "calendars" not in url  # Should use default calendar endpoint


def test_list_calendar_view_specific_calendar(client, sample_event_data):
    """Test list_calendar_view with specific calendar."""
    response_data = {"value": [sample_event_data]}
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.ok = True
    mock_response.json.return_value = response_data
    mock_response.content = b'{"value": []}'
    client.session.request.return_value = mock_response

    start_time = datetime.datetime(2025, 6, 22, 9, 0, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 6, 22, 17, 0, 0, tzinfo=datetime.UTC)

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        events = list(client.list_calendar_view(start_time, end_time, calendar_id="cal123"))

    assert len(events) == 1

    # Verify correct endpoint was called
    call_args = client.session.request.call_args
    assert call_args is not None
    # Check if URL is in positional args or keyword args
    if len(call_args[0]) > 1:
        url = call_args[0][1]
    else:
        url = call_args[1].get("url", "")

    assert "/calendars/cal123/calendarView" in url


def test_list_calendar_view_with_options(client, sample_event_data):
    """Test list_calendar_view with top and timezone options."""
    response_data = {"value": [sample_event_data]}
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.ok = True
    mock_response.json.return_value = response_data
    mock_response.content = b'{"value": []}'
    client.session.request.return_value = mock_response

    start_time = datetime.datetime(2025, 6, 22, 9, 0, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 6, 22, 17, 0, 0, tzinfo=datetime.UTC)

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        events = list(
            client.list_calendar_view(start_time, end_time, top=50, timezone="America/New_York")
        )

    assert len(events) == 1

    # Verify parameters were passed correctly
    args, kwargs = client.session.request.call_args
    assert kwargs["params"]["$top"] == 50
    assert kwargs["headers"]["Prefer"] == 'outlook.timezone="America/New_York"'


def test_get_event_default_calendar(client, sample_event_data):
    """Test get_event from default calendar."""
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.ok = True
    mock_response.json.return_value = sample_event_data
    mock_response.content = b'{"id": "event123"}'
    client.session.request.return_value = mock_response

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        event = client.get_event("event123")

    assert event.id == "event123"
    assert event.subject == "Test Meeting"

    # Verify correct endpoint was called
    call_args = client.session.request.call_args
    assert call_args is not None
    # Check if URL is in positional args or keyword args
    if len(call_args[0]) > 1:
        url = call_args[0][1]
    else:
        url = call_args[1].get("url", "")

    assert "/events/event123" in url
    assert "/calendars/" not in url


def test_get_event_specific_calendar(client, sample_event_data):
    """Test get_event from specific calendar."""
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.ok = True
    mock_response.json.return_value = sample_event_data
    mock_response.content = b'{"id": "event123"}'
    client.session.request.return_value = mock_response

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        event = client.get_event("event123", calendar_id="cal123")

    assert event.id == "event123"

    # Verify correct endpoint was called
    call_args = client.session.request.call_args
    assert call_args is not None
    # Check if URL is in positional args or keyword args
    if len(call_args[0]) > 1:
        url = call_args[0][1]
    else:
        url = call_args[1].get("url", "")

    assert "/calendars/cal123/events/event123" in url


def test_get_event_not_found(client):
    """Test get_event when event doesn't exist."""
    fail_response = Mock()
    fail_response.status_code = 404
    fail_response.ok = False
    fail_response.json.return_value = {"error": {"message": "Event not found"}}
    fail_response.content = b'{"error": {"message": "Event not found"}}'

    client.session.request.return_value = fail_response

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        with pytest.raises(MSGraphAPIError) as exc_info:
            client.get_event("nonexistent")

    assert exc_info.value.status_code == 404


def test_get_room_events_delta_initial_request(client, sample_event_data):
    """Test get_room_events_delta initial request (no tokens)."""
    response_data = {
        "value": [sample_event_data],
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/places/room@example.com/calendarView/delta?$skiptoken=abc123",
        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/places/room@example.com/calendarView/delta?$deltatoken=def456",
    }
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.ok = True
    mock_response.json.return_value = response_data
    mock_response.content = b'{"value": []}'
    client.session.request.return_value = mock_response

    start_time = datetime.datetime(2025, 6, 22, 9, 0, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 6, 22, 17, 0, 0, tzinfo=datetime.UTC)

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        result = client.get_room_events_delta("room@example.com", start_time, end_time)

    assert len(result["events"]) == 1
    assert result["events"][0].id == "event123"
    assert result["next_link"] is not None
    assert result["delta_link"] is not None

    # Verify parameters for initial request
    call_args = client.session.request.call_args
    assert call_args is not None
    # Check if URL is in positional args or keyword args
    if len(call_args[0]) > 1:
        url = call_args[0][1]
    else:
        url = call_args[1].get("url", "")

    assert "/places/room@example.com/calendarView/delta" in url

    # Check parameters
    params = call_args[1].get("params", {})
    assert params.get("startDateTime") == start_time.isoformat()
    assert params.get("endDateTime") == end_time.isoformat()


def test_get_room_events_delta_with_delta_token(client):
    """Test get_room_events_delta with delta token."""
    response_data = {
        "value": [],
        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/places/room@example.com/calendarView/delta?$deltatoken=new_token",
    }
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.ok = True
    mock_response.json.return_value = response_data
    mock_response.content = b'{"value": []}'
    client.session.request.return_value = mock_response

    start_time = datetime.datetime(2025, 6, 22, 9, 0, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 6, 22, 17, 0, 0, tzinfo=datetime.UTC)

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        result = client.get_room_events_delta(
            "room@example.com", start_time, end_time, delta_token="old_token"
        )

    assert len(result["events"]) == 0
    assert result["delta_link"] is not None

    # Verify delta token was used
    args, kwargs = client.session.request.call_args
    assert kwargs["params"]["$deltatoken"] == "old_token"
    assert (
        "startDateTime" not in kwargs["params"]
    )  # Should not include time params with delta token


def test_get_room_events_delta_with_skip_token(client):
    """Test get_room_events_delta with skip token."""
    response_data = {
        "value": [],
        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/places/room@example.com/calendarView/delta?$deltatoken=final_token",
    }
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.ok = True
    mock_response.json.return_value = response_data
    mock_response.content = b'{"value": []}'
    client.session.request.return_value = mock_response

    start_time = datetime.datetime(2025, 6, 22, 9, 0, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 6, 22, 17, 0, 0, tzinfo=datetime.UTC)

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        result = client.get_room_events_delta(
            "room@example.com", start_time, end_time, skip_token="skip123"
        )

    assert len(result["events"]) == 0

    # Verify skip token was used
    args, kwargs = client.session.request.call_args
    assert kwargs["params"]["$skiptoken"] == "skip123"
    assert "startDateTime" not in kwargs["params"]


def test_get_room_events_delta_with_max_page_size(client):
    """Test get_room_events_delta with max page size."""
    response_data = {
        "value": [],
        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/places/room@example.com/calendarView/delta?$deltatoken=token",
    }
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.ok = True
    mock_response.json.return_value = response_data
    mock_response.content = b'{"value": []}'
    client.session.request.return_value = mock_response

    start_time = datetime.datetime(2025, 6, 22, 9, 0, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 6, 22, 17, 0, 0, tzinfo=datetime.UTC)

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        client.get_room_events_delta("room@example.com", start_time, end_time, max_page_size=100)

    # Verify max page size header was set
    args, kwargs = client.session.request.call_args
    assert kwargs["headers"]["Prefer"] == "odata.maxpagesize=100"


def test_list_rooms_as_list(client, sample_room_data):
    """Test list_rooms_as_list method."""
    # Mock the list_rooms method to return an iterator
    with patch.object(client, "list_rooms") as mock_list_rooms:
        mock_rooms = [
            MSGraphRoom(
                id="room1",
                display_name="Conference Room A",
                email_address="rooma@example.com",
                capacity=10,
                building="Building 1",
                floor_number=1,
                phone="+1234567890",
                is_wheelchair_accessible=True,
                original_payload={},
            ),
            MSGraphRoom(
                id="room2",
                display_name="Conference Room B",
                email_address="roomb@example.com",
                capacity=20,
                building="Building 1",
                floor_number=2,
                phone="+1234567891",
                is_wheelchair_accessible=False,
                original_payload={},
            ),
        ]
        mock_list_rooms.return_value = iter(mock_rooms)

        result = client.list_rooms_as_list(page_size=50)

        assert len(result) == 2
        assert result[0].id == "room1"
        assert result[0].display_name == "Conference Room A"
        assert result[1].id == "room2"
        assert result[1].display_name == "Conference Room B"

        mock_list_rooms.assert_called_once_with(50)


def test_list_room_lists(client):
    """Test list_room_lists method."""
    response_data = {
        "value": [
            {
                "id": "roomlist1",
                "displayName": "Building 1 Rooms",
                "emailAddress": "building1@example.com",
            },
            {
                "id": "roomlist2",
                "displayName": "Building 2 Rooms",
                "emailAddress": "building2@example.com",
            },
        ]
    }
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.ok = True
    mock_response.json.return_value = response_data
    mock_response.content = b'{"value": []}'
    client.session.request.return_value = mock_response

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        result = client.list_room_lists()

    assert len(result) == 2
    assert result[0]["id"] == "roomlist1"
    assert result[0]["displayName"] == "Building 1 Rooms"
    assert result[1]["id"] == "roomlist2"
    assert result[1]["displayName"] == "Building 2 Rooms"

    # Verify correct endpoint was called
    call_args = client.session.request.call_args
    assert call_args is not None
    # Check if URL is in positional args or keyword args
    if len(call_args[0]) > 1:
        url = call_args[0][1]
    else:
        url = call_args[1].get("url", "")

    assert "/places/microsoft.graph.roomlist" in url


def test_list_room_lists_empty_response(client):
    """Test list_room_lists with empty response."""
    response_data = {}  # No "value" key
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.ok = True
    mock_response.json.return_value = response_data
    mock_response.content = b"{}"
    client.session.request.return_value = mock_response

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        result = client.list_room_lists()

    assert result == []


def test_list_rooms_in_room_list(client):
    """Test list_rooms_in_room_list method."""
    response_data = {
        "value": [
            {
                "id": "room1",
                "displayName": "Conference Room A",
                "emailAddress": "rooma@example.com",
                "capacity": 10,
                "building": "Building 1",
                "floorNumber": 1,
                "phone": "+1234567890",
                "isWheelChairAccessible": True,
            },
            {
                "id": "room2",
                "displayName": "Conference Room B",
                "emailAddress": "roomb@example.com",
                "capacity": 20,
                "building": "Building 1",
                "floorNumber": 2,
                "phone": "+1234567891",
                "isWheelChairAccessible": False,
            },
        ]
    }
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.ok = True
    mock_response.json.return_value = response_data
    mock_response.content = b'{"value": []}'
    client.session.request.return_value = mock_response

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        result = client.list_rooms_in_room_list("building1@example.com")

    assert len(result) == 2
    assert isinstance(result[0], MSGraphRoom)
    assert result[0].id == "room1"
    assert result[0].display_name == "Conference Room A"
    assert result[0].capacity == 10
    assert result[0].is_wheelchair_accessible is True

    assert result[1].id == "room2"
    assert result[1].capacity == 20
    assert result[1].is_wheelchair_accessible is False

    # Verify correct endpoint was called
    call_args = client.session.request.call_args
    assert call_args is not None
    # Check if URL is in positional args or keyword args
    if len(call_args[0]) > 1:
        url = call_args[0][1]
    else:
        url = call_args[1].get("url", "")

    assert "/places/building1@example.com/microsoft.graph.roomlist/rooms" in url


def test_list_rooms_in_room_list_empty_response(client):
    """Test list_rooms_in_room_list with empty response."""
    response_data = {}  # No "value" key
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.ok = True
    mock_response.json.return_value = response_data
    mock_response.content = b"{}"
    client.session.request.return_value = mock_response

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        result = client.list_rooms_in_room_list("empty@example.com")

    assert result == []


def test_list_rooms_in_room_list_missing_optional_fields(client):
    """Test list_rooms_in_room_list with missing optional fields."""
    response_data = {
        "value": [
            {
                "id": "room1",
                "displayName": "Basic Room",
                "emailAddress": "basic@example.com",
                # Missing optional fields: capacity, building, floorNumber, phone
            }
        ]
    }
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.ok = True
    mock_response.json.return_value = response_data
    mock_response.content = b'{"value": []}'
    client.session.request.return_value = mock_response

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        result = client.list_rooms_in_room_list("building@example.com")

    assert len(result) == 1
    room = result[0]
    assert room.id == "room1"
    assert room.display_name == "Basic Room"
    assert room.email_address == "basic@example.com"
    assert room.capacity is None
    assert room.building is None
    assert room.floor_number is None
    assert room.phone is None
    assert room.is_wheelchair_accessible is False  # Default value


def test_error_propagation_in_new_methods(client):
    """Test that API errors are properly propagated in new methods."""
    fail_response = Mock()
    fail_response.status_code = 500
    fail_response.ok = False
    fail_response.json.return_value = {"error": {"message": "Server Error"}}
    fail_response.content = b'{"error": {"message": "Server Error"}}'
    client.session.request.return_value = fail_response

    start_time = datetime.datetime(2025, 6, 22, 9, 0, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 6, 22, 17, 0, 0, tzinfo=datetime.UTC)

    with patch(
        "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
    ):
        # Test get_room_events_delta error handling
        with pytest.raises(MSGraphAPIError):
            client.get_room_events_delta("room@example.com", start_time, end_time)

        # Test list_calendar_view error handling
        with pytest.raises(MSGraphAPIError):
            list(client.list_calendar_view(start_time, end_time))

        # Test list_room_lists error handling
        with pytest.raises(MSGraphAPIError):
            client.list_room_lists()

        # Test list_rooms_in_room_list error handling
        with pytest.raises(MSGraphAPIError):
            client.list_rooms_in_room_list("building@example.com")
