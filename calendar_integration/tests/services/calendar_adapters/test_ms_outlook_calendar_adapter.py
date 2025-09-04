import datetime
from unittest.mock import Mock, patch

from django.core.exceptions import ImproperlyConfigured

import pytest
import requests

from calendar_integration.constants import CalendarProvider
from calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter import (
    MSOutlookCalendarAdapter,
    MSOutlookCredentialTypedDict,
)
from calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client import (
    MSGraphAPIError,
    MSGraphCalendar,
    MSGraphEvent,
    MSGraphRoom,
    MSOutlookCalendarAPIClient,
)
from calendar_integration.services.dataclasses import (
    ApplicationCalendarData,
    CalendarEventAdapterInputData,
    CalendarEventData,
    CalendarResourceData,
    EventAttendeeData,
)


# Test Fixtures and Helper Data


@pytest.fixture
def mock_credentials() -> MSOutlookCredentialTypedDict:
    """Mock credentials for testing."""
    return MSOutlookCredentialTypedDict(
        token="test_access_token",
        refresh_token="test_refresh_token",
        account_id="test_account_id",
    )


@pytest.fixture
def mock_ms_event() -> MSGraphEvent:
    """Mock MSGraphEvent for testing."""
    return MSGraphEvent(
        id="test_event_id",
        calendar_id="test_calendar_id",
        subject="Test Event",
        body_content="Test event description",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        location="Test Location",
        attendees=[
            {
                "emailAddress": {"address": "test@example.com", "name": "Test User"},
                "status": {"response": "accepted"},
            }
        ],
        organizer={"emailAddress": {"address": "organizer@example.com", "name": "Organizer"}},
        is_cancelled=False,
        original_payload={"id": "test_event_id", "subject": "Test Event"},
    )


@pytest.fixture
def mock_ms_calendar() -> MSGraphCalendar:
    """Mock MSGraphCalendar for testing."""
    return MSGraphCalendar(
        id="test_calendar_id",
        name="Test Calendar",
        email_address="calendar@example.com",
        can_edit=True,
        is_default=False,
        original_payload={"id": "test_calendar_id", "name": "Test Calendar"},
    )


@pytest.fixture
def mock_ms_room() -> MSGraphRoom:
    """Mock MSGraphRoom for testing."""
    return MSGraphRoom(
        id="test_room_id",
        display_name="Test Room",
        email_address="room@example.com",
        capacity=10,
        building="Test Building",
        floor_number=1,
        phone="+1234567890",
        is_wheelchair_accessible=True,
        original_payload={"id": "test_room_id", "displayName": "Test Room"},
    )


@pytest.fixture
def sample_event_input_data() -> CalendarEventAdapterInputData:
    """Sample event input data for testing."""
    return CalendarEventAdapterInputData(
        calendar_external_id="test_calendar_id",
        title="Test Event",
        description="Test Description",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendees=[
            EventAttendeeData(
                email="attendee@example.com",
                name="Test Attendee",
                status="pending",
            )
        ],
    )


@pytest.fixture
def sample_recurring_event_input_data() -> CalendarEventAdapterInputData:
    """Sample recurring event input data for testing."""
    return CalendarEventAdapterInputData(
        calendar_external_id="test_calendar_id",
        title="Recurring Meeting",
        description="Weekly recurring meeting",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendees=[],
        recurrence_rule="RRULE:FREQ=WEEKLY;COUNT=10;BYDAY=MO",
    )


# Initialization Tests


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_adapter_initialization_success(mock_client_class, mock_settings, mock_credentials):
    """Test successful adapter initialization."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    assert adapter.client == mock_client
    assert adapter.refresh_token == "test_refresh_token"
    assert adapter.provider == "microsoft"

    mock_client_class.assert_called_once_with(access_token="test_access_token")
    mock_client.test_connection.assert_called_once()


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
def test_adapter_initialization_missing_settings(mock_settings, mock_credentials):
    """Test adapter initialization with missing settings."""
    mock_settings.MS_CLIENT_ID = None
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    with pytest.raises(ImproperlyConfigured) as exc_info:
        MSOutlookCalendarAdapter(mock_credentials)

    assert "Microsoft Calendar integration requires MS_CLIENT_ID and MS_CLIENT_SECRET" in str(
        exc_info.value
    )


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_adapter_initialization_invalid_credentials(
    mock_client_class, mock_settings, mock_credentials
):
    """Test adapter initialization with invalid credentials."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = False
    mock_client_class.return_value = mock_client

    with pytest.raises(ValueError) as exc_info:
        MSOutlookCalendarAdapter(mock_credentials)

    assert "Invalid or expired Microsoft Graph credentials" in str(exc_info.value)


# Helper Method Tests


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_convert_ms_event_to_calendar_event_data(
    mock_client_class, mock_settings, mock_credentials, mock_ms_event
):
    """Test conversion from MSGraphEvent to CalendarEventData."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    result = adapter._convert_ms_event_to_calendar_event_data(mock_ms_event, "test_calendar_id")

    assert isinstance(result, CalendarEventData)
    assert result.calendar_external_id == "test_calendar_id"
    assert result.external_id == "test_event_id"
    assert result.title == "Test Event"
    assert result.description == "Test event description"
    assert result.start_time == mock_ms_event.start_time
    assert result.end_time == mock_ms_event.end_time
    assert result.status == "confirmed"
    assert len(result.attendees) == 1
    assert result.attendees[0].email == "test@example.com"
    assert result.attendees[0].name == "Test User"
    assert result.attendees[0].status == "accepted"


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_convert_ms_event_cancelled(
    mock_client_class, mock_settings, mock_credentials, mock_ms_event
):
    """Test conversion with cancelled event."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    mock_ms_event.is_cancelled = True

    result = adapter._convert_ms_event_to_calendar_event_data(mock_ms_event, "test_calendar_id")

    assert result.status == "cancelled"


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_convert_calendar_event_input_to_ms_format(
    mock_client_class, mock_settings, mock_credentials, sample_event_input_data
):
    """Test conversion from CalendarEventInputData to MS format."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    result = adapter._convert_calendar_event_input_to_ms_format(sample_event_input_data)

    assert result["subject"] == "Test Event"
    assert result["body"]["content"] == "Test Description"
    assert result["body"]["contentType"] == "HTML"
    assert result["start"]["dateTime"] == sample_event_input_data.start_time.isoformat()
    assert result["end"]["dateTime"] == sample_event_input_data.end_time.isoformat()
    assert len(result["attendees"]) == 1
    assert result["attendees"][0]["emailAddress"]["address"] == "attendee@example.com"
    assert result["attendees"][0]["emailAddress"]["name"] == "Test Attendee"
    assert result["attendees"][0]["type"] == "required"


# Calendar Management Tests


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_create_application_calendar(
    mock_client_class, mock_settings, mock_credentials, mock_ms_calendar
):
    """Test creating an application calendar."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.create_application_calendar.return_value = mock_ms_calendar
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    result = adapter.create_application_calendar("Test Calendar")

    assert isinstance(result, ApplicationCalendarData)
    assert result.external_id == "test_calendar_id"
    assert result.name == "Test Calendar"
    assert result.email == "calendar@example.com"
    assert result.provider == CalendarProvider.MICROSOFT

    mock_client.create_application_calendar.assert_called_once_with("Test Calendar")


# Event Management Tests


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_create_event(
    mock_client_class, mock_settings, mock_credentials, sample_event_input_data, mock_ms_event
):
    """Test creating an event."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.create_event.return_value = mock_ms_event
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    result = adapter.create_event(sample_event_input_data)

    assert isinstance(result, CalendarEventData)
    assert result.external_id == "test_event_id"
    assert result.title == "Test Event"

    mock_client.create_event.assert_called_once()
    # Verify the arguments passed to create_event
    call_args = mock_client.create_event.call_args
    assert call_args[1]["calendar_id"] == "test_calendar_id"
    assert call_args[1]["subject"] == "Test Event"


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_create_event_api_error(
    mock_client_class, mock_settings, mock_credentials, sample_event_input_data
):
    """Test creating an event with API error."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.create_event.side_effect = MSGraphAPIError("API Error")
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    with pytest.raises(ValueError) as exc_info:
        adapter.create_event(sample_event_input_data)

    assert "Failed to create event" in str(exc_info.value)


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_create_recurring_event(
    mock_client_class, mock_settings, mock_credentials, sample_recurring_event_input_data
):
    """Test creating a recurring event."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    # Mock MS event with recurrence pattern
    mock_recurring_ms_event = MSGraphEvent(
        id="recurring_event_123",
        calendar_id="test_calendar_id",
        subject="Recurring Meeting",
        body_content="Weekly recurring meeting",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        location="Test Location",
        attendees=[],
        organizer={"emailAddress": {"address": "organizer@example.com", "name": "Organizer"}},
        is_cancelled=False,
        recurrence_pattern={
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
        original_payload={"id": "recurring_event_123", "subject": "Recurring Meeting"},
    )

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.create_event.return_value = mock_recurring_ms_event
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    result = adapter.create_event(sample_recurring_event_input_data)

    assert isinstance(result, CalendarEventData)
    assert result.external_id == "recurring_event_123"
    assert result.title == "Recurring Meeting"
    assert result.recurrence_rule == "RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=10"

    mock_client.create_event.assert_called_once()
    # Verify the recurrence pattern was converted and passed to the API client
    call_args = mock_client.create_event.call_args
    assert "recurrence" in call_args[1]
    recurrence = call_args[1]["recurrence"]
    assert recurrence["pattern"]["type"] == "weekly"
    assert recurrence["pattern"]["daysOfWeek"] == ["monday"]
    assert recurrence["range"]["type"] == "numbered"
    assert recurrence["range"]["numberOfOccurrences"] == 10


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_get_event(mock_client_class, mock_settings, mock_credentials, mock_ms_event):
    """Test getting a specific event."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.get_event.return_value = mock_ms_event
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    result = adapter.get_event("test_calendar_id", "test_event_id")

    assert isinstance(result, CalendarEventData)
    assert result.external_id == "test_event_id"

    mock_client.get_event.assert_called_once_with("test_event_id", "test_calendar_id")


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_update_event(mock_client_class, mock_settings, mock_credentials, mock_ms_event):
    """Test updating an event."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.update_event.return_value = mock_ms_event
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    event_data = CalendarEventData(
        calendar_external_id="test_calendar_id",
        external_id="test_event_id",
        title="Updated Event",
        description="Updated Description",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendees=[EventAttendeeData(email="test@example.com", name="Test", status="accepted")],
        status="confirmed",
        original_payload={},
    )

    result = adapter.update_event("test_calendar_id", "test_event_id", event_data)

    assert isinstance(result, CalendarEventData)
    mock_client.update_event.assert_called_once()


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_delete_event(mock_client_class, mock_settings, mock_credentials):
    """Test deleting an event."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.delete_event.return_value = None
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    adapter.delete_event("test_calendar_id", "test_event_id")

    mock_client.delete_event.assert_called_once_with("test_event_id", "test_calendar_id")


# Event Listing Tests


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_get_events_non_resource_calendar(
    mock_client_class, mock_settings, mock_credentials, mock_ms_event
):
    """Test getting events from a non-resource calendar."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.list_events.return_value = [mock_ms_event]
    mock_client.get_events_delta.return_value = {
        "events": [],
        "next_link": None,
        "delta_link": "deltatoken",
    }
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    start_date = datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC)
    end_date = datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC)

    result = adapter.get_events("test_calendar_id", False, start_date, end_date)

    assert "events" in result
    assert "next_sync_token" in result

    # Convert iterator to list to test
    events_list = list(result["events"])
    assert len(events_list) == 1
    assert events_list[0].external_id == "test_event_id"


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_get_events_resource_calendar(
    mock_client_class, mock_settings, mock_credentials, mock_ms_event
):
    """Test getting events from a resource calendar."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.get_room_events.return_value = [mock_ms_event]
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    start_date = datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC)
    end_date = datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC)

    result = adapter.get_events("room@example.com", True, start_date, end_date)

    assert "events" in result
    assert "next_sync_token" in result

    # Convert iterator to list to test
    events_list = list(result["events"])
    assert len(events_list) == 1
    assert events_list[0].external_id == "test_event_id"


# Calendar Resources Tests


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_get_calendar_resources(
    mock_client_class, mock_settings, mock_credentials, mock_ms_calendar
):
    """Test getting calendar resources."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.list_calendars.return_value = [mock_ms_calendar]
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    result = list(adapter.get_calendar_resources())

    assert len(result) == 1
    assert isinstance(result[0], CalendarResourceData)
    assert result[0].external_id == "test_calendar_id"
    assert result[0].name == "Test Calendar"
    assert result[0].email == "calendar@example.com"


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_get_calendar_resource(
    mock_client_class, mock_settings, mock_credentials, mock_ms_calendar
):
    """Test getting a specific calendar resource."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.get_calendar.return_value = mock_ms_calendar
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    result = adapter.get_calendar_resource("test_calendar_id")

    assert isinstance(result, CalendarResourceData)
    assert result.external_id == "test_calendar_id"
    assert result.name == "Test Calendar"

    mock_client.get_calendar.assert_called_once_with("test_calendar_id")


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_get_available_calendar_resources(
    mock_client_class, mock_settings, mock_credentials, mock_ms_room
):
    """Test getting available calendar resources."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.list_rooms.return_value = [mock_ms_room]
    mock_client.get_free_busy_schedule.return_value = {
        "value": [
            {
                "scheduleId": "room@example.com",
                "busyViewTimes": [],  # Empty means available
            }
        ]
    }
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    start_time = datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC)

    result = list(adapter.get_available_calendar_resources(start_time, end_time))

    assert len(result) == 1
    assert isinstance(result[0], CalendarResourceData)
    assert result[0].external_id == "test_room_id"
    assert result[0].name == "Test Room"
    assert result[0].email == "room@example.com"
    assert result[0].capacity == 10


# Account Calendar Tests


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_get_account_calendars(mock_client_class, mock_settings, mock_credentials):
    """Test retrieving account calendars."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_calendars = [
        MSGraphCalendar(
            id="primary",
            name="Primary Calendar",
            email_address="user@example.com",
            can_edit=True,
            is_default=True,
            original_payload={"id": "primary", "name": "Primary Calendar"},
        ),
        MSGraphCalendar(
            id="calendar_123",
            name="Work Calendar",
            email_address="work@example.com",
            can_edit=True,
            is_default=False,
            original_payload={"id": "calendar_123", "name": "Work Calendar"},
        ),
        MSGraphCalendar(
            id="calendar_456",
            name="Personal Calendar",
            email_address="personal@example.com",
            can_edit=False,
            is_default=False,
            original_payload={"id": "calendar_456", "name": "Personal Calendar"},
        ),
    ]

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.list_calendars.return_value = mock_calendars
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    calendars = list(adapter.get_account_calendars())

    assert len(calendars) == 3
    assert isinstance(calendars[0], CalendarResourceData)

    # Test primary calendar
    assert calendars[0].external_id == "primary"
    assert calendars[0].name == "Primary Calendar"
    assert calendars[0].description == ""
    assert calendars[0].email == "user@example.com"
    assert calendars[0].is_default is True
    assert calendars[0].provider == "microsoft"
    assert calendars[0].capacity is None

    # Test work calendar
    assert calendars[1].external_id == "calendar_123"
    assert calendars[1].name == "Work Calendar"
    assert calendars[1].description == ""
    assert calendars[1].email == "work@example.com"
    assert calendars[1].is_default is False

    # Test personal calendar
    assert calendars[2].external_id == "calendar_456"
    assert calendars[2].name == "Personal Calendar"
    assert calendars[2].description == ""
    assert calendars[2].email == "personal@example.com"
    assert calendars[2].is_default is False

    # Verify API call
    mock_client.list_calendars.assert_called_once()


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_get_account_calendars_empty_result(mock_client_class, mock_settings, mock_credentials):
    """Test get_account_calendars when no calendars exist."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.list_calendars.return_value = []
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    calendars = list(adapter.get_account_calendars())

    assert len(calendars) == 0
    mock_client.list_calendars.assert_called_once()


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_get_account_calendars_original_payload_preserved(
    mock_client_class, mock_settings, mock_credentials
):
    """Test that original payload is preserved in the result."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_calendar_data = {
        "id": "test_calendar",
        "name": "Test Calendar",
        "emailAddress": "test@example.com",
        "canEdit": True,
        "isDefault": False,
        "extra_field": "extra_value",  # Additional field that should be preserved
    }

    mock_calendar = MSGraphCalendar(
        id="test_calendar",
        name="Test Calendar",
        email_address="test@example.com",
        can_edit=True,
        is_default=False,
        original_payload=mock_calendar_data,
    )

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.list_calendars.return_value = [mock_calendar]
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    calendars = list(adapter.get_account_calendars())

    assert len(calendars) == 1
    calendar = calendars[0]
    assert calendar.original_payload == mock_calendar_data
    assert calendar.original_payload["extra_field"] == "extra_value"


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_get_account_calendars_api_error(mock_client_class, mock_settings, mock_credentials):
    """Test get_account_calendars handles API errors properly."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.list_calendars.side_effect = MSGraphAPIError("API Error")
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    # The method should propagate the MSGraphAPIError
    with pytest.raises(MSGraphAPIError):
        list(adapter.get_account_calendars())


# Webhook Subscription Tests


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_subscribe_to_calendar_events(mock_client_class, mock_settings, mock_credentials):
    """Test subscribing to calendar events."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.subscribe_to_calendar_events.return_value = {"id": "subscription_id"}
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    adapter.subscribe_to_calendar_events("test_calendar_id", "https://example.com/webhook")

    mock_client.subscribe_to_calendar_events.assert_called_once_with(
        calendar_id="test_calendar_id",
        notification_url="https://example.com/webhook",
        change_types=["created", "updated", "deleted"],
    )


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_subscribe_to_calendar_events_api_error(mock_client_class, mock_settings, mock_credentials):
    """Test subscribing to calendar events with API error."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.subscribe_to_calendar_events.side_effect = MSGraphAPIError("Subscription failed")
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    with pytest.raises(ValueError) as exc_info:
        adapter.subscribe_to_calendar_events("test_calendar_id", "https://example.com/webhook")

    assert "Failed to subscribe to calendar events" in str(exc_info.value)


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_unsubscribe_from_calendar_events(mock_client_class, mock_settings, mock_credentials):
    """Test unsubscribing from calendar events."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.list_subscriptions.return_value = [
        {
            "id": "subscription_id_1",
            "resource": "/me/calendars/test_calendar_id/events",
        },
        {
            "id": "subscription_id_2",
            "resource": "/me/events",  # Different calendar
        },
    ]
    mock_client.unsubscribe_from_calendar_events.return_value = None
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    adapter.unsubscribe_from_calendar_events("test_calendar_id")

    mock_client.list_subscriptions.assert_called_once()
    mock_client.unsubscribe_from_calendar_events.assert_called_once_with("subscription_id_1")


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_unsubscribe_from_calendar_events_primary_calendar(
    mock_client_class, mock_settings, mock_credentials
):
    """Test unsubscribing from primary calendar events."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.list_subscriptions.return_value = [
        {
            "id": "subscription_id_1",
            "resource": "/me/events",
        },
    ]
    mock_client.unsubscribe_from_calendar_events.return_value = None
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    adapter.unsubscribe_from_calendar_events("primary")

    mock_client.unsubscribe_from_calendar_events.assert_called_once_with("subscription_id_1")


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_subscribe_to_room_events(mock_client_class, mock_settings, mock_credentials):
    """Test subscribing to room events."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.subscribe_to_room_events.return_value = {"id": "room_subscription_id"}
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    adapter.subscribe_to_room_events("room@example.com", "https://example.com/webhook")

    mock_client.subscribe_to_room_events.assert_called_once_with(
        room_email="room@example.com",
        notification_url="https://example.com/webhook",
        change_types=["created", "updated", "deleted"],
    )


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_unsubscribe_from_room_events(mock_client_class, mock_settings, mock_credentials):
    """Test unsubscribing from room events."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.unsubscribe_from_room_events.return_value = None
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    adapter.unsubscribe_from_room_events("room@example.com")

    mock_client.unsubscribe_from_room_events.assert_called_once_with("room@example.com")


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_unsubscribe_from_room_events_api_error(mock_client_class, mock_settings, mock_credentials):
    """Test unsubscribing from room events with API error."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.unsubscribe_from_room_events.side_effect = MSGraphAPIError("Unsubscribe failed")
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    with pytest.raises(ValueError) as exc_info:
        adapter.unsubscribe_from_room_events("room@example.com")

    assert "Failed to unsubscribe from room events" in str(exc_info.value)


# Tests for _make_request retries and max retries exception in API Client
@patch(
    "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
)
@patch("time.sleep")
def test_make_request_retries_on_server_error(mock_sleep, mock_limiter):
    """Test _make_request retries on server errors."""

    # Create client
    client = MSOutlookCalendarAPIClient(access_token="test_token")

    # Mock responses: first 2 fail with 500, third succeeds
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

    # Mock the session and its request method
    with patch.object(
        client.session,
        "request",
        side_effect=[mock_response_fail, mock_response_fail, mock_response_success],
    ) as mock_request:
        result = client._make_request("GET", "/test/endpoint")

        assert result == {"success": True}
        assert mock_request.call_count == 3
        assert mock_sleep.call_count == 2  # Called twice for retries


@patch(
    "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
)
@patch("time.sleep")
def test_make_request_max_retries_exceeded(mock_sleep, mock_limiter):
    """Test _make_request when max retries are exceeded."""
    # Create client
    client = MSOutlookCalendarAPIClient(access_token="test_token")

    # Mock response that always fails
    mock_response_fail = Mock()
    mock_response_fail.status_code = 500
    mock_response_fail.ok = False
    mock_response_fail.json.return_value = {"error": {"message": "Server Error"}}
    mock_response_fail.content = b'{"error": {"message": "Server Error"}}'

    # Mock the session to always return the failing response
    with patch.object(client.session, "request", return_value=mock_response_fail) as mock_request:
        with pytest.raises(MSGraphAPIError) as exc_info:
            client._make_request("GET", "/test/endpoint")

        assert "MS Graph API error: 500" in str(exc_info.value)
        assert mock_request.call_count == 6  # RETRIES_ON_ERROR + 1 = 5 + 1 = 6
        assert mock_sleep.call_count == 5  # Called 5 times for retries


@patch(
    "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
)
@patch("time.sleep")
def test_make_request_request_exception_retries(mock_sleep, mock_limiter):
    """Test _make_request retries on RequestException."""
    # Create client
    client = MSOutlookCalendarAPIClient(access_token="test_token")

    # Mock request exception for first few attempts, then success
    mock_response_success = Mock()
    mock_response_success.status_code = 200
    mock_response_success.ok = True
    mock_response_success.json.return_value = {"success": True}
    mock_response_success.content = b'{"success": true}'

    # Mock the session to raise exceptions then succeed
    with patch.object(
        client.session,
        "request",
        side_effect=[
            requests.RequestException("Connection error"),
            requests.RequestException("Connection error"),
            mock_response_success,
        ],
    ) as mock_request:
        result = client._make_request("GET", "/test/endpoint")

        assert result == {"success": True}
        assert mock_request.call_count == 3
        assert mock_sleep.call_count == 2


@patch(
    "calendar_integration.services.calendar_clients.ms_outlook_calendar_api_client.quote_limiter"
)
@patch("time.sleep")
def test_make_request_request_exception_max_retries(mock_sleep, mock_limiter):
    """Test _make_request when RequestException max retries exceeded."""
    import requests

    # Create client
    client = MSOutlookCalendarAPIClient(access_token="test_token")

    # Mock request exception that always fails
    with patch.object(
        client.session, "request", side_effect=requests.RequestException("Connection error")
    ) as mock_request:
        with pytest.raises(MSGraphAPIError) as exc_info:
            client._make_request("GET", "/test/endpoint")

        assert "Request failed after 6 attempts" in str(exc_info.value)
        assert mock_request.call_count == 6  # RETRIES_ON_ERROR + 1 = 5 + 1 = 6
        assert mock_sleep.call_count == 5


def test_make_request_non_retryable_error():
    """Test non-retryable HTTP error codes."""
    client = MSOutlookCalendarAPIClient("test_token")

    with patch.object(client.session, "request") as mock_request:
        mock_response = Mock()
        mock_response.ok = False
        mock_response.status_code = 400  # Bad Request - not retryable
        mock_response.json.return_value = {"error": {"message": "Bad request"}}
        mock_response.content = b'{"error": {"message": "Bad request"}}'
        mock_request.return_value = mock_response

        with pytest.raises(MSGraphAPIError) as exc_info:
            client._make_request("GET", "/test")

        assert "MS Graph API error: 400" in str(exc_info.value)
        assert mock_request.call_count == 1  # No retries for 400


# list_calendar_view tests
def test_list_calendar_view_default_calendar():
    """Test list calendar view with default calendar."""
    client = MSOutlookCalendarAPIClient("test_token")
    client.user_id = "test_user"

    with patch.object(client, "_make_request") as mock_request:
        mock_request.return_value = {
            "value": [
                {
                    "id": "event1",
                    "subject": "Test Event 1",
                    "body": {"content": "Test Description"},
                    "start": {"dateTime": "2025-06-22T10:00:00.000Z", "timeZone": "UTC"},
                    "end": {"dateTime": "2025-06-22T11:00:00.000Z", "timeZone": "UTC"},
                    "location": {"displayName": "Test Location"},
                    "attendees": [],
                    "organizer": {},
                    "isCancelled": False,
                }
            ]
        }

        start_time = datetime.datetime(2025, 6, 22, 9, 0, tzinfo=datetime.UTC)
        end_time = datetime.datetime(2025, 6, 22, 17, 0, tzinfo=datetime.UTC)

        events = list(client.list_calendar_view(start_time, end_time))

        assert len(events) == 1
        assert events[0].id == "event1"
        assert events[0].subject == "Test Event 1"

        mock_request.assert_called_once_with(
            "GET",
            "/users/test_user/calendarView",
            params={
                "startDateTime": start_time.isoformat(),
                "endDateTime": end_time.isoformat(),
            },
            headers={},
        )


def test_list_calendar_view_specific_calendar():
    """Test list calendar view with specific calendar."""
    client = MSOutlookCalendarAPIClient("test_token")
    client.user_id = "test_user"

    with patch.object(client, "_make_request") as mock_request:
        mock_request.return_value = {"value": []}

        start_time = datetime.datetime(2025, 6, 22, 9, 0, tzinfo=datetime.UTC)
        end_time = datetime.datetime(2025, 6, 22, 17, 0, tzinfo=datetime.UTC)

        list(client.list_calendar_view(start_time, end_time, calendar_id="test_calendar"))

        mock_request.assert_called_once_with(
            "GET",
            "/users/test_user/calendars/test_calendar/calendarView",
            params={
                "startDateTime": start_time.isoformat(),
                "endDateTime": end_time.isoformat(),
            },
            headers={},
        )


def test_list_calendar_view_with_options():
    """Test list calendar view with top and timezone options."""
    client = MSOutlookCalendarAPIClient("test_token")
    client.user_id = "test_user"

    with patch.object(client, "_make_request") as mock_request:
        mock_request.return_value = {"value": []}

        start_time = datetime.datetime(2025, 6, 22, 9, 0, tzinfo=datetime.UTC)
        end_time = datetime.datetime(2025, 6, 22, 17, 0, tzinfo=datetime.UTC)

        list(client.list_calendar_view(start_time, end_time, top=50, timezone="America/New_York"))

        mock_request.assert_called_once_with(
            "GET",
            "/users/test_user/calendarView",
            params={
                "startDateTime": start_time.isoformat(),
                "endDateTime": end_time.isoformat(),
                "$top": 50,
            },
            headers={"Prefer": 'outlook.timezone="America/New_York"'},
        )


# get_event tests
def test_get_event_default_calendar():
    """Test get event from default calendar."""
    client = MSOutlookCalendarAPIClient("test_token")
    client.user_id = "test_user"

    with patch.object(client, "_make_request") as mock_request:
        mock_request.return_value = {
            "id": "event1",
            "subject": "Test Event",
            "body": {"content": "Test Description"},
            "start": {"dateTime": "2025-06-22T10:00:00.000Z", "timeZone": "UTC"},
            "end": {"dateTime": "2025-06-22T11:00:00.000Z", "timeZone": "UTC"},
            "location": {"displayName": "Test Location"},
            "attendees": [],
            "organizer": {},
            "isCancelled": False,
        }

        event = client.get_event("event1")

        assert event.id == "event1"
        assert event.subject == "Test Event"

        mock_request.assert_called_once_with("GET", "/users/test_user/events/event1")


def test_get_event_specific_calendar():
    """Test get event from specific calendar."""
    client = MSOutlookCalendarAPIClient("test_token")
    client.user_id = "test_user"

    with patch.object(client, "_make_request") as mock_request:
        mock_request.return_value = {
            "id": "event1",
            "subject": "Test Event",
            "body": {"content": "Test Description"},
            "start": {"dateTime": "2025-06-22T10:00:00.000Z", "timeZone": "UTC"},
            "end": {"dateTime": "2025-06-22T11:00:00.000Z", "timeZone": "UTC"},
            "location": {"displayName": "Test Location"},
            "attendees": [],
            "organizer": {},
            "isCancelled": False,
        }

        event = client.get_event("event1", calendar_id="test_calendar")

        assert event.id == "event1"

        mock_request.assert_called_once_with(
            "GET", "/users/test_user/calendars/test_calendar/events/event1"
        )


def test_get_event_api_error():
    """Test get event with API error."""
    client = MSOutlookCalendarAPIClient("test_token")

    with patch.object(client, "_make_request") as mock_request:
        mock_request.side_effect = MSGraphAPIError("Event not found")

        with pytest.raises(MSGraphAPIError):
            client.get_event("nonexistent_event")


# get_room_events_delta tests
def test_get_room_events_delta_initial_request():
    """Test initial room events delta request."""
    client = MSOutlookCalendarAPIClient("test_token")

    with patch.object(client, "_make_request") as mock_request:
        mock_request.return_value = {
            "value": [
                {
                    "id": "event1",
                    "subject": "Room Event",
                    "body": {"content": "Meeting"},
                    "start": {"dateTime": "2025-06-22T10:00:00.000Z", "timeZone": "UTC"},
                    "end": {"dateTime": "2025-06-22T11:00:00.000Z", "timeZone": "UTC"},
                    "location": {"displayName": "Room A"},
                    "attendees": [],
                    "organizer": {},
                    "isCancelled": False,
                }
            ],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/places/room@example.com/calendarView/delta?$skiptoken=abc123",
            "@odata.deltaLink": "https://graph.microsoft.com/v1.0/places/room@example.com/calendarView/delta?$deltatoken=def456",
        }

        start_time = datetime.datetime(2025, 6, 22, 9, 0, tzinfo=datetime.UTC)
        end_time = datetime.datetime(2025, 6, 22, 17, 0, tzinfo=datetime.UTC)

        result = client.get_room_events_delta("room@example.com", start_time, end_time)

        assert len(result["events"]) == 1
        assert result["events"][0].id == "event1"
        assert result["next_link"] is not None
        assert result["delta_link"] is not None

        mock_request.assert_called_once_with(
            "GET",
            "/places/room@example.com/calendarView/delta",
            params={
                "startDateTime": start_time.isoformat(),
                "endDateTime": end_time.isoformat(),
            },
            headers={},
        )


def test_get_room_events_delta_with_delta_token():
    """Test room events delta request with delta token."""
    client = MSOutlookCalendarAPIClient("test_token")

    with patch.object(client, "_make_request") as mock_request:
        mock_request.return_value = {
            "value": [],
            "@odata.deltaLink": "https://graph.microsoft.com/v1.0/places/room@example.com/calendarView/delta?$deltatoken=new_token",
        }

        start_time = datetime.datetime(2025, 6, 22, 9, 0, tzinfo=datetime.UTC)
        end_time = datetime.datetime(2025, 6, 22, 17, 0, tzinfo=datetime.UTC)

        result = client.get_room_events_delta(
            "room@example.com", start_time, end_time, delta_token="old_token"
        )

        assert len(result["events"]) == 0
        assert result["delta_link"] is not None

        mock_request.assert_called_once_with(
            "GET",
            "/places/room@example.com/calendarView/delta",
            params={"$deltatoken": "old_token"},
            headers={},
        )


def test_get_room_events_delta_with_skip_token():
    """Test room events delta request with skip token."""
    client = MSOutlookCalendarAPIClient("test_token")

    with patch.object(client, "_make_request") as mock_request:
        mock_request.return_value = {
            "value": [],
            "@odata.deltaLink": "https://graph.microsoft.com/v1.0/places/room@example.com/calendarView/delta?$deltatoken=final_token",
        }

        start_time = datetime.datetime(2025, 6, 22, 9, 0, tzinfo=datetime.UTC)
        end_time = datetime.datetime(2025, 6, 22, 17, 0, tzinfo=datetime.UTC)

        result = client.get_room_events_delta(
            "room@example.com", start_time, end_time, skip_token="skip123"
        )

        assert len(result["events"]) == 0

        mock_request.assert_called_once_with(
            "GET",
            "/places/room@example.com/calendarView/delta",
            params={"$skiptoken": "skip123"},
            headers={},
        )


def test_get_room_events_delta_with_max_page_size():
    """Test room events delta request with max page size."""
    client = MSOutlookCalendarAPIClient("test_token")

    with patch.object(client, "_make_request") as mock_request:
        mock_request.return_value = {
            "value": [],
            "@odata.deltaLink": "https://graph.microsoft.com/v1.0/places/room@example.com/calendarView/delta?$deltatoken=token",
        }

        start_time = datetime.datetime(2025, 6, 22, 9, 0, tzinfo=datetime.UTC)
        end_time = datetime.datetime(2025, 6, 22, 17, 0, tzinfo=datetime.UTC)

        client.get_room_events_delta("room@example.com", start_time, end_time, max_page_size=100)

        mock_request.assert_called_once_with(
            "GET",
            "/places/room@example.com/calendarView/delta",
            params={
                "startDateTime": start_time.isoformat(),
                "endDateTime": end_time.isoformat(),
            },
            headers={"Prefer": "odata.maxpagesize=100"},
        )


# Adapter get_room_events_delta coverage
@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_adapter_get_room_events_with_pagination(
    mock_client_class, mock_settings, mock_credentials, mock_ms_event
):
    """Test adapter get room events with pagination handling."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True

    # Mock pagination - first call returns next_link, second call returns delta_link
    mock_client.get_room_events_delta.side_effect = [
        {
            "events": [mock_ms_event],
            "next_link": "https://graph.microsoft.com/v1.0/places/room@example.com/calendarView/delta?$skiptoken=abc123",
            "delta_link": None,
        },
        {
            "events": [mock_ms_event],
            "next_link": None,
            "delta_link": "https://graph.microsoft.com/v1.0/places/room@example.com/calendarView/delta?$deltatoken=def456",
        },
    ]
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    start_time = datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC)

    result = adapter._get_room_events(
        "room@example.com", start_time, end_time, sync_token="$deltatoken=initial"
    )

    # Should get 2 events (one from each page)
    assert len(result) == 2
    assert all(isinstance(event, CalendarEventData) for event in result)

    # Should have called get_room_events_delta twice due to pagination
    assert mock_client.get_room_events_delta.call_count == 2


# list_rooms_as_list tests
def test_list_rooms_as_list():
    """Test list rooms as list."""
    client = MSOutlookCalendarAPIClient("test_token")

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
        assert result[1].id == "room2"
        mock_list_rooms.assert_called_once_with(50)


# list_room_lists tests
def test_list_room_lists():
    """Test list room lists."""
    client = MSOutlookCalendarAPIClient("test_token")

    with patch.object(client, "_make_request") as mock_request:
        mock_request.return_value = {
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

        result = client.list_room_lists()

        assert len(result) == 2
        assert result[0]["id"] == "roomlist1"
        assert result[1]["displayName"] == "Building 2 Rooms"

        mock_request.assert_called_once_with("GET", "/places/microsoft.graph.roomlist")


def test_list_room_lists_empty():
    """Test list room lists with empty response."""
    client = MSOutlookCalendarAPIClient("test_token")

    with patch.object(client, "_make_request") as mock_request:
        mock_request.return_value = {}

        result = client.list_room_lists()

        assert result == []


# list_rooms_in_room_list tests
def test_list_rooms_in_room_list():
    """Test list rooms in a specific room list."""
    client = MSOutlookCalendarAPIClient("test_token")

    with patch.object(client, "_make_request") as mock_request:
        mock_request.return_value = {
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

        mock_request.assert_called_once_with(
            "GET", "/places/building1@example.com/microsoft.graph.roomlist/rooms"
        )


def test_list_rooms_in_room_list_empty():
    """Test list rooms in room list with empty response."""
    client = MSOutlookCalendarAPIClient("test_token")

    with patch.object(client, "_make_request") as mock_request:
        mock_request.return_value = {}

        result = client.list_rooms_in_room_list("empty@example.com")

        assert result == []


def test_list_rooms_in_room_list_missing_optional_fields():
    """Test list rooms in room list with missing optional fields."""
    client = MSOutlookCalendarAPIClient("test_token")

    with patch.object(client, "_make_request") as mock_request:
        mock_request.return_value = {
            "value": [
                {
                    "id": "room1",
                    "displayName": "Basic Room",
                    "emailAddress": "basic@example.com",
                    # Missing optional fields: capacity, building, floorNumber, phone
                }
            ]
        }

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


# API Client unsubscribe_from_room_events tests
def test_api_client_unsubscribe_from_room_events_success():
    """Test API client unsubscribe from room events success."""
    client = MSOutlookCalendarAPIClient("test_token")

    with patch.object(client, "list_subscriptions") as mock_list:
        with patch.object(client, "delete_subscription") as mock_delete:
            mock_list.return_value = [
                {"id": "subscription1", "resource": "/users/room@example.com/events"},
                {"id": "subscription2", "resource": "/users/other@example.com/events"},
            ]

            client.unsubscribe_from_room_events("room@example.com")

            mock_list.assert_called_once()
            mock_delete.assert_called_once_with("subscription1")


def test_api_client_unsubscribe_from_room_events_no_subscriptions():
    """Test API client unsubscribe when no subscriptions exist."""
    client = MSOutlookCalendarAPIClient("test_token")

    with patch.object(client, "list_subscriptions") as mock_list:
        with patch.object(client, "delete_subscription") as mock_delete:
            mock_list.return_value = [
                {"id": "subscription1", "resource": "/users/other@example.com/events"}
            ]

            client.unsubscribe_from_room_events("room@example.com")

            mock_list.assert_called_once()
            mock_delete.assert_not_called()


def test_api_client_unsubscribe_from_room_events_api_error():
    """Test API client unsubscribe from room events with API error."""
    client = MSOutlookCalendarAPIClient("test_token")

    with patch.object(client, "list_subscriptions") as mock_list:
        mock_list.side_effect = MSGraphAPIError("Failed to list subscriptions")

        with pytest.raises(MSGraphAPIError) as exc_info:
            client.unsubscribe_from_room_events("room@example.com")

        assert "Failed to unsubscribe from room events" in str(exc_info.value)


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_subscribe_to_all_available_rooms(
    mock_client_class, mock_settings, mock_credentials, mock_ms_room
):
    """Test subscribing to all available rooms."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.list_rooms.return_value = [mock_ms_room]
    mock_client.subscribe_to_multiple_room_events.return_value = [{"id": "subscription_id"}]
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    result = adapter.subscribe_to_all_available_rooms("https://example.com/webhook")

    assert result["total_rooms"] == 1
    assert result["successful_subscriptions"] == 1
    assert len(result["subscriptions"]) == 1

    mock_client.subscribe_to_multiple_room_events.assert_called_once_with(
        room_emails=["room@example.com"],
        notification_url="https://example.com/webhook",
        change_types=["created", "updated", "deleted"],
    )


# Room Event Tests


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_get_room_events_no_sync_token(
    mock_client_class, mock_settings, mock_credentials, mock_ms_event
):
    """Test getting room events without sync token."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.get_room_events.return_value = [mock_ms_event]
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    start_time = datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC)

    result = adapter._get_room_events("room@example.com", start_time, end_time)

    assert len(result) == 1
    assert isinstance(result[0], CalendarEventData)
    assert result[0].external_id == "test_event_id"

    mock_client.get_room_events.assert_called_once_with(
        room_email="room@example.com",
        start_time=start_time,
        end_time=end_time,
    )


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_get_room_events_with_sync_token(
    mock_client_class, mock_settings, mock_credentials, mock_ms_event
):
    """Test getting room events with sync token."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.get_room_events_delta.return_value = {
        "events": [mock_ms_event],
        "next_link": None,
        "delta_link": "deltatoken",
    }
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    start_time = datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC)

    result = adapter._get_room_events(
        "room@example.com", start_time, end_time, sync_token="$deltatoken=abc123"
    )

    assert len(result) == 1
    assert isinstance(result[0], CalendarEventData)
    assert result[0].external_id == "test_event_id"

    mock_client.get_room_events_delta.assert_called_once_with(
        room_email="room@example.com",
        start_time=start_time,
        end_time=end_time,
        delta_token="$deltatoken=abc123",
        max_page_size=250,
    )


# RSVP Status Mapping Tests


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_rsvp_status_mapping(mock_client_class, mock_settings, mock_credentials):
    """Test RSVP status mapping."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    # Test all mappings
    assert adapter.RSVP_STATUS_MAPPING["none"] == "pending"
    assert adapter.RSVP_STATUS_MAPPING["organizer"] == "accepted"
    assert adapter.RSVP_STATUS_MAPPING["tentativelyAccepted"] == "pending"
    assert adapter.RSVP_STATUS_MAPPING["accepted"] == "accepted"
    assert adapter.RSVP_STATUS_MAPPING["declined"] == "declined"
    assert adapter.RSVP_STATUS_MAPPING["notResponded"] == "pending"


# Error Handling Tests


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_get_events_api_error(mock_client_class, mock_settings, mock_credentials):
    """Test get_events with API error during iteration."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.list_events.side_effect = MSGraphAPIError("API Error")
    # Mock get_events_delta to succeed so we can test the iterator error
    mock_client.get_events_delta.return_value = {
        "delta_link": "https://graph.microsoft.com/v1.0/me/events/delta?$deltatoken=test_token"
    }
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    start_date = datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC)
    end_date = datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC)

    # Get the result which contains an iterator
    result = adapter.get_events("test_calendar_id", False, start_date, end_date)

    # The error should be raised when consuming the iterator
    # Note: The iterator doesn't wrap errors in ValueError, so we expect MSGraphAPIError
    with pytest.raises(MSGraphAPIError) as exc_info:
        list(result["events"])  # Consume the iterator to trigger the API call

    assert "API Error" in str(exc_info.value)


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_get_calendar_resources_api_error(mock_client_class, mock_settings, mock_credentials):
    """Test get_calendar_resources with API error."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client.list_calendars.side_effect = MSGraphAPIError("API Error")
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    with pytest.raises(ValueError):
        list(adapter.get_calendar_resources())


# Integration and Edge Cases


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_empty_attendees_list(mock_client_class, mock_settings, mock_credentials):
    """Test handling of events with empty attendees list."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    # Create event with empty attendees
    mock_event = MSGraphEvent(
        id="test_event_id",
        calendar_id="test_calendar_id",
        subject="Test Event",
        body_content="Test Description",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        location="Test Location",
        attendees=[],  # Empty attendees
        organizer={},
        is_cancelled=False,
        original_payload={},
    )

    result = adapter._convert_ms_event_to_calendar_event_data(mock_event, "test_calendar_id")

    assert len(result.attendees) == 0


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_malformed_attendee_data(mock_client_class, mock_settings, mock_credentials):
    """Test handling of malformed attendee data."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    # Create event with malformed attendee data
    mock_event = MSGraphEvent(
        id="test_event_id",
        calendar_id="test_calendar_id",
        subject="Test Event",
        body_content="Test Description",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        location="Test Location",
        attendees=[
            {
                # Missing emailAddress key
                "status": {"response": "accepted"},
            },
            {
                "emailAddress": {},  # Empty emailAddress
                "status": {"response": "declined"},
            },
        ],
        organizer={},
        is_cancelled=False,
        original_payload={},
    )

    result = adapter._convert_ms_event_to_calendar_event_data(mock_event, "test_calendar_id")

    assert len(result.attendees) == 2
    # Both attendees should have empty email and name
    assert result.attendees[0].email == ""
    assert result.attendees[0].name == ""
    assert result.attendees[1].email == ""
    assert result.attendees[1].name == ""


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_unknown_rsvp_status(mock_client_class, mock_settings, mock_credentials):
    """Test handling of unknown RSVP status."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    # Create event with unknown RSVP status
    mock_event = MSGraphEvent(
        id="test_event_id",
        calendar_id="test_calendar_id",
        subject="Test Event",
        body_content="Test Description",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        location="Test Location",
        attendees=[
            {
                "emailAddress": {"address": "test@example.com", "name": "Test User"},
                "status": {"response": "unknownStatus"},
            }
        ],
        organizer={},
        is_cancelled=False,
        original_payload={},
    )

    result = adapter._convert_ms_event_to_calendar_event_data(mock_event, "test_calendar_id")

    # Unknown status should default to "pending"
    assert result.attendees[0].status == "pending"


# Tests for _get_events_iterator method


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_get_events_iterator(mock_client_class, mock_settings, mock_credentials, mock_ms_event):
    """Test the _get_events_iterator method yields CalendarEventData objects."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    # Create a list of mock events
    mock_events = [mock_ms_event, mock_ms_event]
    calendar_id = "test_calendar_id"

    # Get the iterator
    events_iterator = adapter._get_events_iterator(mock_events, calendar_id)

    # Convert to list to test
    events_list = list(events_iterator)

    assert len(events_list) == 2
    for event in events_list:
        assert isinstance(event, CalendarEventData)
        assert event.external_id == "test_event_id"
        assert event.calendar_external_id == calendar_id
        assert event.title == "Test Event"


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_get_events_iterator_empty_list(mock_client_class, mock_settings, mock_credentials):
    """Test _get_events_iterator with empty events list."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    # Get iterator with empty list
    events_iterator = adapter._get_events_iterator([], "test_calendar_id")
    events_list = list(events_iterator)

    assert len(events_list) == 0


# Tests for _extract_next_page_token method


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_extract_next_page_token_with_skiptoken(mock_client_class, mock_settings, mock_credentials):
    """Test _extract_next_page_token method with skiptoken in next_link."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    # Mock delta result with next_link containing skiptoken
    delta_result = {
        "events": [],
        "next_link": "https://graph.microsoft.com/v1.0/me/events?$skiptoken=abc123&other=param",
        "delta_link": None,
    }

    token = adapter._extract_next_page_token(delta_result)
    assert token == "abc123"


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_extract_next_page_token_no_next_link(mock_client_class, mock_settings, mock_credentials):
    """Test _extract_next_page_token method with no next_link."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    # Mock delta result without next_link
    delta_result = {
        "events": [],
        "next_link": None,
        "delta_link": "https://graph.microsoft.com/v1.0/me/events?$deltatoken=xyz789",
    }

    token = adapter._extract_next_page_token(delta_result)
    assert token is None


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_extract_next_page_token_no_skiptoken(mock_client_class, mock_settings, mock_credentials):
    """Test _extract_next_page_token method with next_link but no skiptoken."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    # Mock delta result with next_link but no skiptoken
    delta_result = {
        "events": [],
        "next_link": "https://graph.microsoft.com/v1.0/me/events?other=param",
        "delta_link": None,
    }

    token = adapter._extract_next_page_token(delta_result)
    assert token is None


# Tests for _create_delta_events_iterator method


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_create_delta_events_iterator_with_deltatoken(
    mock_client_class, mock_settings, mock_credentials, mock_ms_event
):
    """Test _create_delta_events_iterator with deltatoken."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True

    # Mock delta query response
    mock_client.get_events_delta.return_value = {
        "events": [mock_ms_event],
        "next_link": None,
        "delta_link": "https://graph.microsoft.com/v1.0/me/events?$deltatoken=newtoken123",
    }
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    # Test with deltatoken
    calendar_id = "test_calendar_id"
    start_date = datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC)
    end_date = datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC)
    sync_token = "$deltatoken=oldtoken456"
    max_results = 100

    events_iterator = adapter._create_delta_events_iterator(
        calendar_id, start_date, end_date, sync_token, max_results
    )

    # Convert to list to test
    events_list = list(events_iterator)

    assert len(events_list) == 1
    assert isinstance(events_list[0], CalendarEventData)
    assert events_list[0].external_id == "test_event_id"

    # Verify API was called with correct parameters
    mock_client.get_events_delta.assert_called_once_with(
        start_time=start_date,
        end_time=end_date,
        delta_token=sync_token,
        calendar_id=calendar_id,
        max_page_size=max_results,
    )

    # Check that next sync token was set
    assert adapter._next_sync_token == "newtoken123"


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_create_delta_events_iterator_with_skiptoken(
    mock_client_class, mock_settings, mock_credentials, mock_ms_event
):
    """Test _create_delta_events_iterator with skiptoken."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True

    # Mock delta query response
    mock_client.get_events_delta.return_value = {
        "events": [mock_ms_event],
        "next_link": None,
        "delta_link": "https://graph.microsoft.com/v1.0/me/events?$deltatoken=finaltoken",
    }
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    # Test with skiptoken (no $deltatoken= prefix)
    calendar_id = "test_calendar_id"
    start_date = datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC)
    end_date = datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC)
    sync_token = "skiptoken123"
    max_results = 100

    events_iterator = adapter._create_delta_events_iterator(
        calendar_id, start_date, end_date, sync_token, max_results
    )

    # Convert to list to test
    events_list = list(events_iterator)

    assert len(events_list) == 1

    # Verify API was called with skip_token parameter
    mock_client.get_events_delta.assert_called_once_with(
        start_time=start_date,
        end_time=end_date,
        skip_token=sync_token,
        calendar_id=calendar_id,
        max_page_size=max_results,
    )


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_create_delta_events_iterator_with_pagination(
    mock_client_class, mock_settings, mock_credentials, mock_ms_event
):
    """Test _create_delta_events_iterator with multiple pages."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True

    # Create second event for second page
    mock_ms_event2 = MSGraphEvent(
        id="test_event_id_2",
        calendar_id="test_calendar_id",
        subject="Test Event 2",
        body_content="Test event description 2",
        start_time=datetime.datetime(2025, 6, 22, 14, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 15, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        location="Test Location 2",
        attendees=[],
        organizer={"emailAddress": {"address": "organizer@example.com", "name": "Organizer"}},
        is_cancelled=False,
        original_payload={"id": "test_event_id_2", "subject": "Test Event 2"},
    )

    # Mock multiple delta query responses (first with next_link, second with delta_link)
    mock_client.get_events_delta.side_effect = [
        {
            "events": [mock_ms_event],
            "next_link": "https://graph.microsoft.com/v1.0/me/events?$skiptoken=page2token",
            "delta_link": None,
        },
        {
            "events": [mock_ms_event2],
            "next_link": None,
            "delta_link": "https://graph.microsoft.com/v1.0/me/events?$deltatoken=finaltoken",
        },
    ]
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    # Test with deltatoken
    calendar_id = "test_calendar_id"
    start_date = datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC)
    end_date = datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC)
    sync_token = "$deltatoken=starttoken"
    max_results = 100

    events_iterator = adapter._create_delta_events_iterator(
        calendar_id, start_date, end_date, sync_token, max_results
    )

    # Convert to list to test
    events_list = list(events_iterator)

    assert len(events_list) == 2
    assert events_list[0].external_id == "test_event_id"
    assert events_list[1].external_id == "test_event_id_2"

    # Verify API was called twice (once for initial, once for next page)
    assert mock_client.get_events_delta.call_count == 2

    # Check that final sync token was set
    assert adapter._next_sync_token == "finaltoken"


@patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
@patch(
    "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
)
def test_create_delta_events_iterator_empty_pages(
    mock_client_class, mock_settings, mock_credentials
):
    """Test _create_delta_events_iterator with empty event pages."""
    mock_settings.MS_CLIENT_ID = "test_client_id"
    mock_settings.MS_CLIENT_SECRET = "test_client_secret"

    mock_client = Mock()
    mock_client.test_connection.return_value = True

    # Mock delta query response with no events
    mock_client.get_events_delta.return_value = {
        "events": [],
        "next_link": None,
        "delta_link": "https://graph.microsoft.com/v1.0/me/events?$deltatoken=emptytoken",
    }
    mock_client_class.return_value = mock_client

    adapter = MSOutlookCalendarAdapter(mock_credentials)

    # Test with deltatoken
    calendar_id = "test_calendar_id"
    start_date = datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC)
    end_date = datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC)
    sync_token = "$deltatoken=token"
    max_results = 100

    events_iterator = adapter._create_delta_events_iterator(
        calendar_id, start_date, end_date, sync_token, max_results
    )

    # Convert to list to test
    events_list = list(events_iterator)

    assert len(events_list) == 0
    assert adapter._next_sync_token == "emptytoken"


class TestRRuleConversion:
    """Test RRULE conversion methods."""

    @patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
    @patch(
        "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
    )
    def test_convert_rrule_to_ms_format_weekly(
        self, mock_client_class, mock_settings, mock_credentials
    ):
        """Test converting weekly RRULE to MS format."""
        mock_settings.MS_CLIENT_ID = "test_client_id"
        mock_settings.MS_CLIENT_SECRET = "test_client_secret"

        mock_client = Mock()
        mock_client.test_connection.return_value = True
        mock_client_class.return_value = mock_client

        adapter = MSOutlookCalendarAdapter(mock_credentials)

        rrule = "RRULE:FREQ=WEEKLY;COUNT=10;BYDAY=MO"

        result = adapter._convert_rrule_to_ms_format(rrule)

        assert result["pattern"]["type"] == "weekly"
        assert result["pattern"]["daysOfWeek"] == ["monday"]
        assert result["range"]["type"] == "numbered"
        assert result["range"]["numberOfOccurrences"] == 10

    @patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
    @patch(
        "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
    )
    def test_convert_rrule_to_ms_format_daily(
        self, mock_client_class, mock_settings, mock_credentials
    ):
        """Test converting daily RRULE to MS format."""
        mock_settings.MS_CLIENT_ID = "test_client_id"
        mock_settings.MS_CLIENT_SECRET = "test_client_secret"

        mock_client = Mock()
        mock_client.test_connection.return_value = True
        mock_client_class.return_value = mock_client

        adapter = MSOutlookCalendarAdapter(mock_credentials)

        rrule = "RRULE:FREQ=DAILY;COUNT=5"

        result = adapter._convert_rrule_to_ms_format(rrule)

        assert result["pattern"]["type"] == "daily"
        assert result["range"]["type"] == "numbered"
        assert result["range"]["numberOfOccurrences"] == 5

    @patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
    @patch(
        "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
    )
    def test_convert_ms_recurrence_to_rrule_weekly(
        self, mock_client_class, mock_settings, mock_credentials
    ):
        """Test converting MS weekly recurrence to RRULE."""
        mock_settings.MS_CLIENT_ID = "test_client_id"
        mock_settings.MS_CLIENT_SECRET = "test_client_secret"

        mock_client = Mock()
        mock_client.test_connection.return_value = True
        mock_client_class.return_value = mock_client

        adapter = MSOutlookCalendarAdapter(mock_credentials)

        ms_recurrence = {
            "pattern": {
                "type": "weekly",
                "interval": 1,
                "daysOfWeek": ["monday"],
            },
            "range": {
                "type": "numbered",
                "numberOfOccurrences": 10,
            },
        }

        result = adapter._convert_ms_recurrence_to_rrule(ms_recurrence)

        assert result == "RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=10"

    @patch("calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.settings")
    @patch(
        "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAPIClient"
    )
    def test_convert_ms_recurrence_to_rrule_daily(
        self, mock_client_class, mock_settings, mock_credentials
    ):
        """Test converting MS daily recurrence to RRULE."""
        mock_settings.MS_CLIENT_ID = "test_client_id"
        mock_settings.MS_CLIENT_SECRET = "test_client_secret"

        mock_client = Mock()
        mock_client.test_connection.return_value = True
        mock_client_class.return_value = mock_client

        adapter = MSOutlookCalendarAdapter(mock_credentials)

        ms_recurrence = {
            "pattern": {
                "type": "daily",
                "interval": 2,
            },
            "range": {
                "type": "numbered",
                "numberOfOccurrences": 5,
            },
        }

        result = adapter._convert_ms_recurrence_to_rrule(ms_recurrence)

        assert result == "RRULE:FREQ=DAILY;INTERVAL=2;COUNT=5"
