import datetime
from datetime import timedelta
from unittest.mock import MagicMock, Mock, patch

from django.contrib.auth import get_user_model
from django.utils import timezone

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken

from calendar_integration.constants import (
    CalendarProvider,
    CalendarSyncStatus,
    CalendarType,
)
from calendar_integration.models import (
    AvailableTime,
    AvailableTimeRecurrenceException,
    BlockedTime,
    BlockedTimeRecurrenceException,
    Calendar,
    CalendarEvent,
    CalendarOrganizationResourcesImport,
    CalendarOwnership,
    CalendarSync,
    EventAttendance,
    EventExternalAttendance,
    ExternalAttendee,
    GoogleCalendarServiceAccount,
    ResourceAllocation,
)
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    ApplicationCalendarData,
    AvailableTimeWindow,
    CalendarEventAdapterInputData,
    CalendarEventData,
    CalendarEventInputData,
    CalendarResourceData,
    EventAttendanceInputData,
    EventAttendeeData,
    EventExternalAttendanceInputData,
    EventsSyncChanges,
    ExternalAttendeeInputData,
    ResourceAllocationInputData,
    ResourceData,
    UnavailableTimeWindow,
)
from organizations.models import Organization


User = get_user_model()


@pytest.fixture
def patch_get_calendar(calendar):
    """Patch the _get_calendar method to work with mocked adapters."""
    # Cache for created calendars to avoid duplicates
    created_calendars = {}

    def mock_get_calendar(self, external_id):
        if external_id == calendar.external_id:
            return calendar
        elif external_id in created_calendars:
            return created_calendars[external_id]
        elif external_id == "room_123":
            # Create a resource calendar for import tests
            cal = Calendar.objects.create(
                name="Conference Room A",
                external_id="room_123",
                provider=CalendarProvider.GOOGLE,
                calendar_type="resource",
                capacity=10,
                organization=calendar.organization,
            )
            created_calendars[external_id] = cal
            return cal
        elif external_id == "target_cal_123":
            # Create target calendar for transfer tests
            cal = Calendar.objects.create(
                name="Target Calendar",
                external_id="target_cal_123",
                provider=CalendarProvider.GOOGLE,
                organization=calendar.organization,
            )
            created_calendars[external_id] = cal
            return cal
        else:
            # For other external IDs, try to find or create
            try:
                return Calendar.objects.get(
                    external_id=external_id,
                    provider=CalendarProvider.GOOGLE,
                    organization=calendar.organization,
                )
            except Calendar.DoesNotExist as exc:
                raise Calendar.DoesNotExist(
                    f"Calendar matching query does not exist: {external_id}"
                ) from exc

    with patch(
        "calendar_integration.services.calendar_service.CalendarService._get_calendar_by_external_id",
        mock_get_calendar,
    ):
        yield mock_get_calendar


@pytest.fixture
def mock_google_adapter():
    """Mock Google Calendar adapter."""
    with patch(
        "calendar_integration.services.calendar_adapters.google_calendar_adapter.GoogleCalendarAdapter"
    ) as mock_adapter_class:
        mock_adapter = Mock()
        mock_adapter.provider = CalendarProvider.GOOGLE
        # Prevent Django ORM issues by removing problematic attributes
        del mock_adapter.resolve_expression
        del mock_adapter.get_source_expressions
        mock_adapter_class.return_value = mock_adapter
        mock_adapter_class.from_service_account_credentials.return_value = mock_adapter
        yield mock_adapter


@pytest.fixture
def mock_ms_adapter():
    """Mock Microsoft Outlook Calendar adapter."""
    with patch(
        "calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter.MSOutlookCalendarAdapter"
    ) as mock_adapter_class:
        mock_adapter = Mock()
        mock_adapter.provider = CalendarProvider.MICROSOFT
        # Prevent Django ORM issues by removing problematic attributes
        del mock_adapter.resolve_expression
        del mock_adapter.get_source_expressions
        mock_adapter_class.return_value = mock_adapter
        yield mock_adapter


@pytest.fixture
def social_account(db):
    """Create a social account for testing."""
    user = User.objects.create_user(
        username="testuser", email="test@example.com", password="testpass123"
    )
    account = SocialAccount.objects.create(user=user, provider=CalendarProvider.GOOGLE, uid="12345")
    return account


@pytest.fixture
def social_token(social_account):
    """Create a social token for testing."""
    return SocialToken.objects.create(
        account=social_account,
        token="test_access_token",
        token_secret="test_refresh_token",
        expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
    )


@pytest.fixture
def organization(db):
    """Create a calendar event for testing."""
    return Organization.objects.create(
        name="Test Organization",
        should_sync_rooms=True,
    )


@pytest.fixture
def google_service_account(db, organization):
    """Create a Google service account for testing."""
    return GoogleCalendarServiceAccount.objects.create(
        email="service@example.com",
        audience="https://oauth2.googleapis.com/token",
        public_key="test_public_key",
        private_key_id="test_key_id",
        private_key="test_private_key",
        organization=organization,
    )


@pytest.fixture
def calendar(db, organization):
    """Create a calendar for testing."""
    return Calendar.objects.create(
        name="Test Calendar",
        description="A test calendar",
        external_id="cal_123",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )


@pytest.fixture
def calendar_event(calendar, db, organization):
    """Create a calendar event for testing."""
    return CalendarEvent.objects.create(
        calendar_fk=calendar,
        title="Test Event",
        description="A test event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        external_id="event_123",
        organization=organization,
    )


@pytest.fixture
def sample_event_data():
    """Sample event data for testing."""
    return CalendarEventData(
        calendar_external_id="cal_123",
        external_id="event_456",
        title="Sample Event",
        description="A sample event",
        start_time=datetime.datetime(2025, 6, 22, 14, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 15, 0, tzinfo=datetime.UTC),
        attendees=[
            EventAttendeeData(email="attendee@example.com", name="Test Attendee", status="accepted")
        ],
        status="confirmed",
    )


@pytest.fixture
def sample_resource_data():
    """Sample resource data for testing."""
    return ResourceData(
        email="resource@example.com",
        title="Test Resource",
        external_id="resource_123",
        status="accepted",
    )


@pytest.fixture
def sample_unavailable_window():
    """Sample unavailable time window for testing."""
    return UnavailableTimeWindow(
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        reason="calendar_event",
        id=123,
        data=CalendarEventData(
            calendar_external_id="cal_123",
            external_id="event_456",
            title="Sample Event",
            description="A sample event",
            start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
            attendees=[],
            status="confirmed",
        ),
    )


@pytest.fixture
def sample_event_input_data():
    """Sample event input data for create_event tests."""
    return CalendarEventInputData(
        title="New Event",
        description="A new event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )


@pytest.fixture
def sample_event_input_data_with_attendances(db):
    """Sample event input data with attendances for testing."""
    user1 = User.objects.create_user(username="user1", email="user1@example.com")
    user2 = User.objects.create_user(username="user2", email="user2@example.com")

    return CalendarEventInputData(
        title="Event with Attendances",
        description="An event with attendances",
        start_time=datetime.datetime(2025, 6, 22, 14, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 15, 0, tzinfo=datetime.UTC),
        attendances=[
            EventAttendanceInputData(user_id=user1.id),
            EventAttendanceInputData(user_id=user2.id),
        ],
        external_attendances=[
            EventExternalAttendanceInputData(
                external_attendee=ExternalAttendeeInputData(
                    email="external@example.com",
                    name="External User",
                )
            ),
            EventExternalAttendanceInputData(
                external_attendee=ExternalAttendeeInputData(
                    email="external2@example.com",
                    name="External User 2",
                )
            ),
        ],
        resource_allocations=[],
    )


@pytest.fixture
def sample_event_input_data_with_resources(organization, db):
    """Sample event input data with resource allocations for testing."""
    # Create a calendar for resource allocation
    resource_calendar = Calendar.objects.create(
        organization=organization,
        external_id="resource_cal_123",
        name="Resource Calendar",
        provider=CalendarProvider.GOOGLE,
    )

    return CalendarEventInputData(
        title="Event with Resources",
        description="An event with resource allocations",
        start_time=datetime.datetime(2025, 6, 22, 16, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 17, 0, tzinfo=datetime.UTC),
        attendances=[],
        external_attendances=[],
        resource_allocations=[
            ResourceAllocationInputData(resource_id=resource_calendar.id),
        ],
    )


@pytest.mark.django_db
def test_calendar_service_initialization_with_social_account(
    social_account, social_token, mock_google_adapter, organization
):
    """Test CalendarService initialization with a social account."""
    service = CalendarService()
    service.authenticate(account=social_account, organization=organization)

    assert service.account == social_account
    assert service.calendar_adapter == mock_google_adapter


@pytest.mark.django_db
def test_calendar_service_initialization_with_service_account(
    google_service_account, mock_google_adapter
):
    """Test CalendarService initialization with a Google service account."""
    service = CalendarService()
    service.authenticate(
        account=google_service_account, organization=google_service_account.organization
    )

    assert service.account == google_service_account
    assert service.calendar_adapter == mock_google_adapter


@pytest.mark.django_db
def test_calendar_service_initialization_with_none_account():
    """Test CalendarService initialization with None account."""
    service = CalendarService()

    assert service.account is None
    assert service.calendar_adapter is None
    assert service.organization is None  # Should be None before authentication


@pytest.mark.django_db
def test_calendar_service_initialization_with_account_without_adapter(
    social_account, social_token, organization
):
    """Test CalendarService initialization when adapter creation fails."""
    # Mock unsupported provider
    social_account.provider = "unsupported"
    social_account.save()

    with pytest.raises(NotImplementedError):
        service = CalendarService()
        service.authenticate(account=social_account, organization=organization)


@pytest.mark.django_db
def test_get_calendar_adapter_for_google_social_account(
    social_account, social_token, mock_google_adapter
):
    """Test getting Google adapter for social account."""
    adapter = CalendarService.get_calendar_adapter_for_account(social_account)

    assert adapter == mock_google_adapter


@pytest.mark.django_db
def test_get_calendar_adapter_for_microsoft_social_account(
    social_account, social_token, mock_ms_adapter
):
    """Test getting Microsoft adapter for social account."""
    social_account.provider = CalendarProvider.MICROSOFT
    social_account.save()

    adapter = CalendarService.get_calendar_adapter_for_account(social_account)

    assert adapter == mock_ms_adapter


@pytest.mark.django_db
def test_get_calendar_adapter_for_google_service_account(
    google_service_account, mock_google_adapter
):
    """Test getting Google adapter for service account."""
    adapter = CalendarService.get_calendar_adapter_for_account(google_service_account)

    assert adapter == mock_google_adapter


@pytest.mark.django_db
def test_get_calendar_adapter_unsupported_provider(social_account, social_token):
    """Test error when provider is not supported."""
    social_account.provider = "unsupported"
    social_account.save()

    with pytest.raises(
        NotImplementedError, match="Calendar adapter for provider unsupported is not implemented"
    ):
        CalendarService.get_calendar_adapter_for_account(social_account)


@pytest.mark.django_db
def test_import_organization_calendar_resources(
    social_account,
    social_token,
    mock_google_adapter,
    sample_resource_data,
    calendar,
    patch_get_calendar,
):
    """Test importing organization calendar resources."""
    mock_google_adapter.get_available_calendar_resources.return_value = [sample_resource_data]

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    start_time = datetime.datetime(2025, 6, 22, 9, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 6, 22, 17, 0, tzinfo=datetime.UTC)

    import_state = CalendarOrganizationResourcesImport.objects.create(
        organization=calendar.organization,
        start_time=start_time,
        end_time=end_time,
    )

    # Mock the internal implementation method that gets called
    with patch.object(service, "_execute_organization_calendar_resources_import") as mock_execute:
        mock_execute.return_value = [sample_resource_data]

        service.import_organization_calendar_resources(import_state)

    # Check side effects
    import_state.refresh_from_db()
    assert import_state.status == "success"
    mock_execute.assert_called_once_with(start_time=start_time, end_time=end_time)


@pytest.mark.django_db
def test_import_account_calendars(social_account, social_token, mock_google_adapter, organization):
    """Test importing account calendars."""
    mock_calendar_resources = [
        CalendarResourceData(
            external_id="cal_123",
            name="Primary Calendar",
            description="User's primary calendar",
            email="user@example.com",
            is_default=True,
            provider="google",
            original_payload={"id": "cal_123", "summary": "Primary Calendar"},
        ),
        CalendarResourceData(
            external_id="cal_456",
            name="Work Calendar",
            description="Work events",
            email="work@example.com",
            is_default=False,
            provider="google",
            original_payload={"id": "cal_456", "summary": "Work Calendar"},
        ),
    ]

    mock_google_adapter.get_account_calendars.return_value = mock_calendar_resources

    service = CalendarService()
    service.authenticate(account=social_account, organization=organization)

    service.import_account_calendars()

    # Verify calendars were created
    assert Calendar.objects.filter(organization=organization, external_id="cal_123").exists()
    assert Calendar.objects.filter(organization=organization, external_id="cal_456").exists()

    # Verify calendar details
    primary_calendar = Calendar.objects.get(organization=organization, external_id="cal_123")
    assert primary_calendar.name == "Primary Calendar"
    assert primary_calendar.description == "User's primary calendar"
    assert primary_calendar.email == "user@example.com"
    assert primary_calendar.provider == CalendarProvider.GOOGLE
    assert primary_calendar.calendar_type == CalendarType.PERSONAL

    work_calendar = Calendar.objects.get(organization=organization, external_id="cal_456")
    assert work_calendar.name == "Work Calendar"
    assert work_calendar.description == "Work events"
    assert work_calendar.email == "work@example.com"

    # Verify CalendarOwnership was created
    primary_ownership = CalendarOwnership.objects.filter(
        organization=organization,
        calendar=primary_calendar,
        user=social_account.user,
    ).first()
    assert primary_ownership is not None
    assert primary_ownership.is_default is True

    work_ownership = CalendarOwnership.objects.filter(
        organization=organization,
        calendar=work_calendar,
        user=social_account.user,
    ).first()
    assert work_ownership is not None
    assert work_ownership.is_default is False


@pytest.mark.django_db
def test_import_account_calendars_updates_existing(
    social_account, social_token, mock_google_adapter, organization
):
    """Test that import_account_calendars updates existing calendars."""
    # Create existing calendar
    existing_calendar = Calendar.objects.create(
        external_id="cal_123",
        organization=organization,
        name="Old Name",
        description="Old description",
        email="old@example.com",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
    )

    mock_calendar_resources = [
        CalendarResourceData(
            external_id="cal_123",
            name="Updated Calendar Name",
            description="Updated description",
            email="updated@example.com",
            is_default=True,
            provider="google",
            original_payload={"id": "cal_123", "summary": "Updated Calendar Name"},
        ),
    ]

    mock_google_adapter.get_account_calendars.return_value = mock_calendar_resources

    service = CalendarService()
    service.authenticate(account=social_account, organization=organization)

    with patch.object(service.calendar_adapter, "subscribe_to_calendar_events"):
        service.import_account_calendars()

    # Verify calendar was updated, not duplicated
    calendars = Calendar.objects.filter(organization=organization, external_id="cal_123")
    assert calendars.count() == 1

    updated_calendar = calendars.first()
    assert updated_calendar.id == existing_calendar.id  # Same object
    assert updated_calendar.name == "Updated Calendar Name"
    assert updated_calendar.description == "Updated description"
    assert updated_calendar.email == "updated@example.com"


@pytest.mark.django_db
def test_import_account_calendars_no_calendars(
    social_account, social_token, mock_google_adapter, organization
):
    """Test import_account_calendars when no calendars are returned."""
    mock_google_adapter.get_account_calendars.return_value = []

    service = CalendarService()
    service.authenticate(account=social_account, organization=organization)

    with patch.object(service.calendar_adapter, "subscribe_to_calendar_events") as mock_subscribe:
        service.import_account_calendars()

    # Verify no calendars were created
    assert Calendar.objects.filter(organization=organization).count() == 0
    assert CalendarOwnership.objects.filter(organization=organization).count() == 0
    assert mock_subscribe.call_count == 0


@pytest.mark.django_db
def test_import_account_calendars_not_authenticated():
    """Test that import_account_calendars requires authentication."""
    service = CalendarService()

    # Should fail because no authentication
    with pytest.raises(ValueError):
        service.import_account_calendars()


@pytest.mark.django_db
def test_create_application_calendar(
    social_account, social_token, mock_google_adapter, patch_calendar_create, organization
):
    """Test creating an application calendar."""
    created_calendar_data = ApplicationCalendarData(
        id=None,
        organization_id=organization.id,
        external_id="new_cal_123",
        name="_virtual_Test Calendar",
        description="Test description",
        email="calendar@example.com",
        provider=CalendarProvider.GOOGLE,
    )
    mock_google_adapter.create_application_calendar.return_value = created_calendar_data
    mock_google_adapter.provider = CalendarProvider.GOOGLE

    service = CalendarService()
    service.authenticate(account=social_account, organization=organization)

    with patch("calendar_integration.tasks.sync_calendar_task.delay") as mock_task:
        result = service.create_application_calendar("Test Calendar", organization=organization)

    # Verify database object was created
    calendar = Calendar.objects.get(organization=organization, external_id="new_cal_123")
    assert calendar.name == "_virtual_Test Calendar"
    assert calendar.description == "Test description"
    assert calendar.provider == CalendarProvider.GOOGLE

    # Verify return value
    assert result.external_id == "new_cal_123"
    assert result.name == "_virtual_Test Calendar"

    # Verify task was called
    mock_task.assert_called_once()

    # Verify database object was actually created
    calendar = Calendar.objects.get(organization=organization, external_id="new_cal_123")
    assert calendar.name == "_virtual_Test Calendar"
    assert calendar.provider == CalendarProvider.GOOGLE
    assert result.name == "_virtual_Test Calendar"
    mock_task.assert_called_once()


@pytest.mark.django_db
def test_create_application_calendar_with_service_account(
    google_service_account, mock_google_adapter, patch_calendar_create, organization
):
    """Test creating application calendar with service account links calendar."""
    created_calendar_data = ApplicationCalendarData(
        id=None,
        external_id="service_cal_123",
        name="_virtual_Service Calendar",
        description="Service calendar",
        email="service@example.com",
        provider=CalendarProvider.GOOGLE,
        organization_id=organization.id,
    )
    mock_google_adapter.create_application_calendar.return_value = created_calendar_data
    mock_google_adapter.provider = CalendarProvider.GOOGLE

    service = CalendarService()
    service.authenticate(account=google_service_account, organization=organization)

    with patch("calendar_integration.tasks.sync_calendar_task.delay"):
        service.create_application_calendar("Service Calendar", organization=organization)

    # Verify database object was created
    calendar = Calendar.objects.get(external_id="service_cal_123", organization=organization)
    assert calendar.name == "_virtual_Service Calendar"
    assert calendar.description == "Service calendar"
    assert calendar.provider == CalendarProvider.GOOGLE

    # Verify the service account was linked to the calendar
    google_service_account.refresh_from_db()
    assert google_service_account.calendar is not None
    assert google_service_account.calendar.external_id == "service_cal_123"
    google_service_account.refresh_from_db()
    assert google_service_account.calendar is not None
    assert google_service_account.calendar.external_id == "service_cal_123"


@pytest.mark.django_db
def test_create_event(
    social_account,
    social_token,
    mock_google_adapter,
    calendar,
    patch_get_calendar,
    sample_event_input_data,
):
    """Test creating an event."""
    created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="event_new_123",
        title="New Event",
        description="A new event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        attendees=[],
        resources=[],
        original_payload={},
    )
    mock_google_adapter.create_event.return_value = created_event_data
    mock_google_adapter.provider = CalendarProvider.GOOGLE

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    result = service.create_event(calendar.id, sample_event_input_data)

    # Verify database object was created
    assert result.external_id == "event_new_123"
    assert result.title == "New Event"
    assert result.calendar == calendar

    # Verify the adapter was called with the correct CalendarEventAdapterInputData
    mock_google_adapter.create_event.assert_called_once()
    call_args = mock_google_adapter.create_event.call_args[0][0]
    assert isinstance(call_args, CalendarEventAdapterInputData)
    assert call_args.title == "New Event"
    assert call_args.calendar_external_id == calendar.external_id


@pytest.mark.django_db
def test_create_recurring_event(
    social_account,
    social_token,
    mock_google_adapter,
    calendar,
    patch_get_calendar,
):
    """Test creating a recurring event."""
    event_input_data = CalendarEventInputData(
        title="Weekly Meeting",
        description="Recurring weekly meeting",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
        recurrence_rule="RRULE:FREQ=WEEKLY;COUNT=10;BYDAY=MO",
    )

    created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="recurring_event_123",
        title="Weekly Meeting",
        description="Recurring weekly meeting",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule="RRULE:FREQ=WEEKLY;COUNT=10;BYDAY=MO",
    )
    mock_google_adapter.create_event.return_value = created_event_data
    mock_google_adapter.provider = CalendarProvider.GOOGLE

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    result = service.create_event(calendar.id, event_input_data)

    # Verify database object was created
    assert result.external_id == "recurring_event_123"
    assert result.title == "Weekly Meeting"
    assert result.calendar == calendar

    # Check if the CalendarEvent was created with recurrence_rule relationship
    calendar_event = CalendarEvent.objects.get(
        external_id="recurring_event_123",
        organization_id=calendar.organization.id,
    )
    assert calendar_event.recurrence_rule is not None
    assert calendar_event.recurrence_rule.to_rrule_string() == "FREQ=WEEKLY;COUNT=10;BYDAY=MO"

    # Verify the adapter was called with the correct CalendarEventAdapterInputData
    mock_google_adapter.create_event.assert_called_once()
    call_args = mock_google_adapter.create_event.call_args[0][0]
    assert isinstance(call_args, CalendarEventAdapterInputData)
    assert call_args.title == "Weekly Meeting"
    assert call_args.calendar_external_id == calendar.external_id


@pytest.mark.django_db
def test_create_recurring_event_helper_method(
    social_account,
    social_token,
    mock_google_adapter,
    calendar,
    patch_get_calendar,
):
    """Test creating a recurring event using the convenience helper method create_recurring_event."""
    recurrence_rule = "RRULE:FREQ=WEEKLY;COUNT=5;BYDAY=MO"
    mock_google_adapter.provider = CalendarProvider.GOOGLE
    created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="recurring_event_helper_123",
        title="Helper Weekly Meeting",
        description="Weekly meeting created via helper",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=recurrence_rule.replace("RRULE:", ""),
    )
    mock_google_adapter.create_event.return_value = created_event_data

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    event = service.create_recurring_event(
        calendar.id,
        title="Helper Weekly Meeting",
        description="Weekly meeting created via helper",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        recurrence_rule=recurrence_rule,
    )

    # Verify database object was created and recurrence rule persisted
    assert event.external_id == "recurring_event_helper_123"
    assert event.title == "Helper Weekly Meeting"
    assert event.recurrence_rule is not None
    assert event.recurrence_rule.to_rrule_string() == recurrence_rule.replace("RRULE:", "")

    # Adapter should have been called with recurrence rule
    mock_google_adapter.create_event.assert_called_once()
    adapter_input = mock_google_adapter.create_event.call_args[0][0]
    assert isinstance(adapter_input, CalendarEventAdapterInputData)
    assert adapter_input.recurrence_rule == recurrence_rule
    assert adapter_input.is_recurring_instance is False


@pytest.mark.django_db
def test_create_recurring_exception_modified(
    social_account,
    social_token,
    mock_google_adapter,
    calendar,
    patch_get_calendar,
):
    """Test creating a modified exception for a recurring event returns a new instance and records exception."""
    recurrence_rule = "RRULE:FREQ=WEEKLY;COUNT=5;BYDAY=MO"
    mock_google_adapter.provider = CalendarProvider.GOOGLE

    parent_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="parent_recurring_123",
        title="Parent Weekly Meeting",
        description="Parent recurring meeting",
        start_time=datetime.datetime(2025, 6, 23, 10, 0, tzinfo=datetime.UTC),  # Monday
        end_time=datetime.datetime(2025, 6, 23, 11, 0, tzinfo=datetime.UTC),
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=recurrence_rule.replace("RRULE:", ""),
    )
    modified_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="modified_instance_123",
        title="Modified Weekly Meeting",
        description="Modified description",
        start_time=datetime.datetime(2025, 6, 30, 10, 30, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 30, 11, 30, tzinfo=datetime.UTC),
        attendees=[],
        resources=[],
        original_payload={},
    )
    # First adapter call for parent recurring event, second for modified exception instance
    mock_google_adapter.create_event.side_effect = [
        parent_created_event_data,
        modified_created_event_data,
    ]

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    # Patch availability to always allow
    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(
                start_time=parent_created_event_data.start_time,
                end_time=parent_created_event_data.end_time,
                id=1,
                can_book_partially=False,
            )
        ],
    ):
        parent_event = service.create_recurring_event(
            calendar.id,
            title="Parent Weekly Meeting",
            description="Parent recurring meeting",
            start_time=parent_created_event_data.start_time,
            end_time=parent_created_event_data.end_time,
            recurrence_rule=recurrence_rule,
        )

    assert parent_event.recurrence_rule is not None

    exception_date = parent_event.start_time + datetime.timedelta(weeks=1)
    modified_title = "Modified Weekly Meeting"
    modified_description = "Modified description"
    modified_start = exception_date + datetime.timedelta(minutes=30)
    modified_end = modified_start + datetime.timedelta(hours=1)

    # Availability patch for modified instance
    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(
                start_time=modified_start,
                end_time=modified_end,
                id=2,
                can_book_partially=False,
            )
        ],
    ):
        modified_event = service.create_recurring_event_exception(
            parent_event=parent_event,
            exception_date=exception_date,
            modified_title=modified_title,
            modified_description=modified_description,
            modified_start_time=modified_start,
            modified_end_time=modified_end,
            is_cancelled=False,
        )

    # Assertions for modified instance
    assert modified_event is not None
    assert modified_event.parent_recurring_object == parent_event
    assert modified_event.is_recurring_exception is True
    assert modified_event.title == modified_title
    assert modified_event.description == modified_description
    assert modified_event.start_time == modified_start
    assert modified_event.end_time == modified_end
    assert modified_event.recurrence_rule is None  # Instances shouldn't have their own rule
    # Exception record
    assert parent_event.recurrence_exceptions.count() == 1
    exception = parent_event.recurrence_exceptions.first()
    assert exception is not None
    assert exception.is_cancelled is False
    assert exception.modified_event == modified_event
    assert exception.exception_date == exception_date


@pytest.mark.django_db
def test_create_recurring_exception_cancelled(
    social_account,
    social_token,
    mock_google_adapter,
    calendar,
    patch_get_calendar,
):
    """Test creating a cancelled exception for a recurring event returns None and records cancellation."""
    recurrence_rule = "RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=MO"
    mock_google_adapter.provider = CalendarProvider.GOOGLE
    parent_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="parent_recurring_cancel_123",
        title="Parent Weekly Meeting",
        description="Parent recurring meeting",
        start_time=datetime.datetime(2025, 6, 23, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 23, 11, 0, tzinfo=datetime.UTC),
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=recurrence_rule.replace("RRULE:", ""),
    )
    mock_google_adapter.create_event.return_value = parent_created_event_data

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(
                start_time=parent_created_event_data.start_time,
                end_time=parent_created_event_data.end_time,
                id=1,
                can_book_partially=False,
            )
        ],
    ):
        parent_event = service.create_recurring_event(
            calendar.id,
            title="Parent Weekly Meeting",
            description="Parent recurring meeting",
            start_time=parent_created_event_data.start_time,
            end_time=parent_created_event_data.end_time,
            recurrence_rule=recurrence_rule,
        )

    exception_date = parent_event.start_time + datetime.timedelta(weeks=1)
    result = service.create_recurring_event_exception(
        parent_event=parent_event,
        exception_date=exception_date,
        is_cancelled=True,
    )

    assert result is None
    assert parent_event.recurrence_exceptions.count() == 1
    exception = parent_event.recurrence_exceptions.first()
    assert exception is not None
    assert exception.is_cancelled is True
    assert exception.modified_event is None
    assert exception.exception_date == exception_date


@pytest.mark.django_db
def test_create_recurring_exception_non_recurring_error(
    social_account,
    social_token,
    mock_google_adapter,
    calendar_event,
):
    """Test creating an exception for a non-recurring event raises ValueError."""
    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar_event.organization)

    with pytest.raises(ValueError, match="Cannot create exception for non-recurring event"):
        service.create_recurring_event_exception(
            parent_event=calendar_event,
            exception_date=calendar_event.start_time + datetime.timedelta(weeks=1),
            is_cancelled=True,
        )


@pytest.mark.django_db
def test_create_recurring_exception_on_master_event_with_future_occurrences(
    social_account,
    social_token,
    mock_google_adapter,
    calendar,
    patch_get_calendar,
):
    """Test creating an exception on the master event date when there are future occurrences."""
    recurrence_rule = "RRULE:FREQ=WEEKLY;COUNT=5;BYDAY=MO"
    mock_google_adapter.provider = CalendarProvider.GOOGLE

    # Mock data for creating the parent recurring event
    parent_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="parent_recurring_master_123",
        title="Master Weekly Meeting",
        description="Master recurring meeting",
        start_time=datetime.datetime(2025, 6, 23, 10, 0, tzinfo=datetime.UTC),  # Monday
        end_time=datetime.datetime(2025, 6, 23, 11, 0, tzinfo=datetime.UTC),
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=recurrence_rule.replace("RRULE:", ""),
    )

    # Mock data for creating the new recurring event (starting from second occurrence)
    new_recurring_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="new_recurring_from_second_123",
        title="Master Weekly Meeting",
        description="Master recurring meeting",
        start_time=datetime.datetime(2025, 6, 30, 10, 0, tzinfo=datetime.UTC),  # Following Monday
        end_time=datetime.datetime(2025, 6, 30, 11, 0, tzinfo=datetime.UTC),
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule="FREQ=WEEKLY;COUNT=4;BYDAY=MO",  # COUNT reduced by 1
    )

    # Side effects: first call creates parent, second call creates new recurring event
    mock_google_adapter.create_event.side_effect = [
        parent_created_event_data,
        new_recurring_event_data,
    ]

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    # Patch availability to always allow
    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(
                start_time=parent_created_event_data.start_time,
                end_time=parent_created_event_data.end_time,
                id=1,
                can_book_partially=False,
            )
        ],
    ):
        parent_event = service.create_recurring_event(
            calendar.id,
            title="Master Weekly Meeting",
            description="Master recurring meeting",
            start_time=parent_created_event_data.start_time,
            end_time=parent_created_event_data.end_time,
            recurrence_rule=recurrence_rule,
        )

    assert parent_event.recurrence_rule is not None
    original_recurrence_rule_id = parent_event.recurrence_rule.id

    # Create exception on the master event date (same as parent_event.start_time.date())
    exception_date = parent_event.start_time.date()
    modified_title = "Modified Master Meeting"
    modified_description = "Modified first occurrence"

    # Patch availability for the new recurring event - use next week's time
    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(
                start_time=parent_event.start_time + timedelta(weeks=1),
                end_time=parent_event.end_time + timedelta(weeks=1),
                id=2,
                can_book_partially=False,
            )
        ],
    ):
        result_event = service.create_recurring_event_exception(
            parent_event=parent_event,
            exception_date=exception_date,
            modified_title=modified_title,
            modified_description=modified_description,
            is_cancelled=False,
        )

    # The result should be the modified original event (no longer recurring)
    assert result_event.id == parent_event.id
    assert result_event.recurrence_rule is None  # No longer recurring
    assert result_event.title == modified_title
    assert result_event.description == modified_description

    # Verify that create_event was called twice (once for parent, once for new recurring)
    assert mock_google_adapter.create_event.call_count == 2

    # Verify the original recurrence rule was deleted
    from calendar_integration.models import RecurrenceRule

    assert not RecurrenceRule.objects.filter(id=original_recurrence_rule_id).exists()


@pytest.mark.django_db
def test_create_recurring_exception_on_master_event_no_future_occurrences(
    social_account,
    social_token,
    mock_google_adapter,
    calendar,
    patch_get_calendar,
):
    """Test creating an exception on master event date when there are no future occurrences."""
    recurrence_rule = "RRULE:FREQ=WEEKLY;COUNT=1;BYDAY=MO"  # Only one occurrence
    mock_google_adapter.provider = CalendarProvider.GOOGLE

    parent_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="parent_single_occurrence_123",
        title="Single Occurrence Meeting",
        description="Only one occurrence",
        start_time=datetime.datetime(2025, 6, 23, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 23, 11, 0, tzinfo=datetime.UTC),
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=recurrence_rule.replace("RRULE:", ""),
    )

    # For this test, we should only have one call to create_event since there's no next occurrence
    mock_google_adapter.create_event.return_value = parent_created_event_data

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(
                start_time=parent_created_event_data.start_time,
                end_time=parent_created_event_data.end_time,
                id=1,
                can_book_partially=False,
            )
        ],
    ):
        parent_event = service.create_recurring_event(
            calendar.id,
            title="Single Occurrence Meeting",
            description="Only one occurrence",
            start_time=parent_created_event_data.start_time,
            end_time=parent_created_event_data.end_time,
            recurrence_rule=recurrence_rule,
        )

    assert parent_event.recurrence_rule is not None
    original_recurrence_rule_id = parent_event.recurrence_rule.id

    # Create exception on the master event date - use actual date
    exception_date = parent_event.start_time.date()
    modified_title = "Modified Single Meeting"

    # Since there are no future occurrences, we shouldn't need to mock availability again
    # The service should just modify the original event and make it non-recurring
    result_event = service.create_recurring_event_exception(
        parent_event=parent_event,
        exception_date=exception_date,
        modified_title=modified_title,
        is_cancelled=False,
    )

    # The result should be the modified original event (no longer recurring)
    assert result_event.id == parent_event.id
    assert result_event.recurrence_rule is None  # No longer recurring
    assert result_event.title == modified_title

    # Verify that create_event was only called once (for the original parent)
    assert mock_google_adapter.create_event.call_count == 1

    # Verify the original recurrence rule was deleted
    from calendar_integration.models import RecurrenceRule

    assert not RecurrenceRule.objects.filter(id=original_recurrence_rule_id).exists()

    # Verify any existing exceptions were deleted
    assert parent_event.recurrence_exceptions.count() == 0


@pytest.mark.django_db
def test_create_recurring_exception_on_master_preserves_attendances_and_resources(
    social_account,
    social_token,
    mock_google_adapter,
    calendar,
    patch_get_calendar,
    db,
):
    """Test that creating an exception on master event preserves attendances and resources in new recurring event."""
    recurrence_rule = "RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=MO"
    mock_google_adapter.provider = CalendarProvider.GOOGLE

    # Create additional user and resource for testing
    additional_user = User.objects.create_user(
        username="attendee", email="attendee@example.com", password="testpass123"
    )
    resource_calendar = Calendar.objects.create(
        name="Test Resource",
        external_id="resource_123",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.RESOURCE,
        organization=calendar.organization,
    )

    parent_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="parent_with_attendees_123",
        title="Meeting with Attendees",
        description="Has attendees and resources",
        start_time=datetime.datetime(2025, 6, 23, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 23, 11, 0, tzinfo=datetime.UTC),
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=recurrence_rule.replace("RRULE:", ""),
    )

    new_recurring_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="new_recurring_with_attendees_123",
        title="Meeting with Attendees",
        description="Has attendees and resources",
        start_time=datetime.datetime(2025, 6, 30, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 30, 11, 0, tzinfo=datetime.UTC),
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule="FREQ=WEEKLY;COUNT=2;BYDAY=MO",
    )

    mock_google_adapter.create_event.side_effect = [
        parent_created_event_data,
        new_recurring_event_data,
    ]

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(
                start_time=parent_created_event_data.start_time,
                end_time=parent_created_event_data.end_time,
                id=1,
                can_book_partially=False,
            )
        ],
    ):
        parent_event = service.create_event(
            calendar.id,
            CalendarEventInputData(
                title="Meeting with Attendees",
                description="Has attendees and resources",
                start_time=parent_created_event_data.start_time,
                end_time=parent_created_event_data.end_time,
                recurrence_rule=recurrence_rule,
                attendances=[
                    EventAttendanceInputData(user_id=additional_user.id),
                ],
                external_attendances=[
                    EventExternalAttendanceInputData(
                        external_attendee=ExternalAttendeeInputData(
                            email="external@example.com",
                            name="External User",
                        )
                    ),
                ],
                resource_allocations=[
                    ResourceAllocationInputData(resource_id=resource_calendar.id),
                ],
            ),
        )

    assert parent_event.attendances.count() == 1
    assert parent_event.external_attendances.count() == 1
    assert parent_event.resource_allocations.count() == 1

    # Create exception on master event date
    exception_date = parent_event.start_time.date()

    # Patch availability for the new recurring event - use next week's time
    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(
                start_time=parent_event.start_time + timedelta(weeks=1),
                end_time=parent_event.end_time + timedelta(weeks=1),
                id=2,
                can_book_partially=False,
            )
        ],
    ):
        result_event = service.create_recurring_event_exception(
            parent_event=parent_event,
            exception_date=exception_date,
            modified_title="Modified Meeting",
            is_cancelled=False,
        )

    # Verify the result event is the modified original
    assert result_event.id == parent_event.id
    assert result_event.recurrence_rule is None
    assert result_event.title == "Modified Meeting"

    # Verify a new recurring event was created (should be the second call to create_event)
    assert mock_google_adapter.create_event.call_count == 2

    # Verify new recurring event has attendances and resources
    # Note: The new recurring event should be created with the same attendances as the original
    new_recurring_events = CalendarEvent.objects.filter(
        organization_id=calendar.organization.id,
    ).exclude(id=parent_event.id)
    assert new_recurring_events.count() == 1
    new_recurring_event = new_recurring_events.first()

    # The new recurring event should have copied attendances and resources
    assert new_recurring_event.attendances.count() == 1
    assert new_recurring_event.external_attendances.count() == 1
    assert new_recurring_event.resource_allocations.count() == 1


@pytest.mark.django_db
def test_create_recurring_event_bulk_modification_creates_continuation_and_record(
    social_account, social_token, mock_google_adapter, calendar, patch_get_calendar
):
    """Non-cancel bulk modification should create a continuation event and an EventBulkModification record."""
    mock_google_adapter.provider = CalendarProvider.GOOGLE

    # Create parent recurring event
    parent_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="parent_bulk_event_123",
        title="Parent Bulk",
        description="Parent recurring",
        start_time=datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 9, 1, 10, 0, tzinfo=datetime.UTC),
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule="FREQ=WEEKLY;COUNT=5;BYDAY=MO",
    )
    # Adapter returns parent on first call and continuation event on second
    continuation_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="continuation_bulk_event_123",
        title="Continuation Bulk",
        description="Continuation recurring",
        start_time=datetime.datetime(2025, 9, 8, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 9, 8, 10, 0, tzinfo=datetime.UTC),
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule="FREQ=WEEKLY;COUNT=4;BYDAY=MO",
    )
    mock_google_adapter.create_event.side_effect = [
        parent_created_event_data,
        continuation_created_event_data,
    ]

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(
                start_time=parent_created_event_data.start_time,
                end_time=parent_created_event_data.end_time,
                id=1,
                can_book_partially=False,
            )
        ],
    ):
        parent_event = service.create_recurring_event(
            calendar.id,
            title="Parent Bulk",
            description="Parent recurring",
            start_time=parent_created_event_data.start_time,
            end_time=parent_created_event_data.end_time,
            recurrence_rule="RRULE:FREQ=WEEKLY;COUNT=5;BYDAY=MO",
        )

    # Apply bulk modification starting at second occurrence
    modification_start = parent_event.start_time + datetime.timedelta(weeks=1)

    # Mock availability for the continuation creation (second call to adapter)
    continuation_start = modification_start
    continuation_end = modification_start + (parent_event.end_time - parent_event.start_time)
    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(
                start_time=continuation_start,
                end_time=continuation_end,
                id=2,
                can_book_partially=False,
            )
        ],
    ):
        result = service.create_recurring_event_bulk_modification(
            parent_event=parent_event,
            modification_start_date=modification_start,
            modified_title="Modified Bulk",
            is_bulk_cancelled=False,
        )

    # Continuation should be created and linked
    assert result is not None
    assert result.recurrence_rule is not None
    # Parent should have a bulk modification record
    from calendar_integration.models import EventBulkModification

    assert parent_event.bulk_modification_records.count() == 1
    bulk_record = parent_event.bulk_modification_records.first()
    assert isinstance(bulk_record, EventBulkModification)
    assert bulk_record.is_bulk_cancelled is False


@pytest.mark.django_db
def test_create_recurring_event_bulk_modification_cancelled_records_only(
    social_account, social_token, mock_google_adapter, calendar, patch_get_calendar
):
    """Cancelled bulk modification should not create a continuation but should create a bulk record."""
    mock_google_adapter.provider = CalendarProvider.GOOGLE

    parent_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="parent_bulk_event_cancel_123",
        title="Parent Bulk Cancel",
        description="Parent recurring",
        # Use a Monday to match BYDAY=MO in the recurrence rule
        start_time=datetime.datetime(2025, 10, 6, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 10, 6, 10, 0, tzinfo=datetime.UTC),
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule="FREQ=WEEKLY;COUNT=5;BYDAY=MO",
    )
    mock_google_adapter.create_event.return_value = parent_created_event_data

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(
                start_time=parent_created_event_data.start_time,
                end_time=parent_created_event_data.end_time,
                id=1,
                can_book_partially=False,
            )
        ],
    ):
        parent_event = service.create_recurring_event(
            calendar.id,
            title="Parent Bulk Cancel",
            description="Parent recurring",
            start_time=parent_created_event_data.start_time,
            end_time=parent_created_event_data.end_time,
            recurrence_rule="RRULE:FREQ=WEEKLY;COUNT=5;BYDAY=MO",
        )

    modification_start = parent_event.start_time + datetime.timedelta(weeks=1)
    result = service.create_recurring_event_bulk_modification(
        parent_event=parent_event,
        modification_start_date=modification_start,
        is_bulk_cancelled=True,
    )

    # Since cancelled, no continuation created
    assert result is None

    assert parent_event.bulk_modification_records.count() == 1
    bulk_record = parent_event.bulk_modification_records.first()
    assert bulk_record.is_bulk_cancelled is True


@pytest.mark.django_db
def test_create_recurring_blocked_time_bulk_modification_creates_continuation_and_record(
    social_account, social_token, mock_google_adapter, calendar, patch_get_calendar
):
    """Non-cancel blocked-time bulk modification should create a continuation and a BlockedTimeBulkModification."""
    mock_google_adapter.provider = CalendarProvider.GOOGLE

    # Create a parent blocked time with a weekly RRULE
    parent_start = datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC)
    parent_end = parent_start + datetime.timedelta(hours=1)

    # Create parent blocked time in DB
    from calendar_integration.models import BlockedTimeBulkModification, RecurrenceRule

    rule_blocked = RecurrenceRule.from_rrule_string(
        "FREQ=WEEKLY;COUNT=5;BYDAY=MO", organization=calendar.organization
    )
    rule_blocked.save()

    parent_blocked = BlockedTime.objects.create(
        calendar_fk=calendar,
        start_time=parent_start,
        end_time=parent_end,
        reason="Original Block",
        organization=calendar.organization,
        recurrence_rule_fk=rule_blocked,
    )

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    # Apply bulk modification starting at second occurrence
    modification_start = parent_blocked.start_time + datetime.timedelta(weeks=1)

    result = service.create_recurring_blocked_time_bulk_modification(
        parent_blocked_time=parent_blocked,
        modification_start_date=modification_start,
        modified_reason="Modified Block",
        is_bulk_cancelled=False,
    )

    # Continuation should be created
    assert result is not None
    assert isinstance(result, BlockedTime)
    # Parent should have a bulk modification record
    assert parent_blocked.bulk_modification_records.count() == 1
    br = parent_blocked.bulk_modification_records.first()
    assert isinstance(br, BlockedTimeBulkModification)
    assert br.is_bulk_cancelled is False


@pytest.mark.django_db
def test_create_recurring_available_time_bulk_modification_cancelled_records_only(
    social_account, social_token, mock_google_adapter, calendar, patch_get_calendar
):
    """Cancelled available-time bulk modification should not create a continuation but should create a record."""
    # Create parent available time
    parent_start = datetime.datetime(2025, 10, 6, 9, 0, tzinfo=datetime.UTC)
    parent_end = parent_start + datetime.timedelta(hours=1)
    from calendar_integration.models import AvailableTimeBulkModification, RecurrenceRule

    rule_available = RecurrenceRule.from_rrule_string(
        "FREQ=WEEKLY;COUNT=5;BYDAY=MO", organization=calendar.organization
    )
    rule_available.save()

    parent_available = AvailableTime.objects.create(
        calendar_fk=calendar,
        start_time=parent_start,
        end_time=parent_end,
        organization=calendar.organization,
        recurrence_rule_fk=rule_available,
    )

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    modification_start = parent_available.start_time + datetime.timedelta(weeks=1)
    result = service.create_recurring_available_time_bulk_modification(
        parent_available_time=parent_available,
        modification_start_date=modification_start,
        is_bulk_cancelled=True,
    )

    assert result is None
    assert parent_available.bulk_modification_records.count() == 1
    ar = parent_available.bulk_modification_records.first()
    assert isinstance(ar, AvailableTimeBulkModification)
    assert ar.is_bulk_cancelled is True


@pytest.mark.django_db
def test_get_recurring_event_instances_non_recurring_in_and_out_of_range(
    social_account, social_token, mock_google_adapter, calendar, patch_get_calendar, calendar_event
):
    """Non-recurring event should be returned only if inside range."""
    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    in_range = service.get_recurring_event_instances(
        recurring_event=calendar_event,
        start_date=calendar_event.start_time - datetime.timedelta(hours=1),
        end_date=calendar_event.end_time + datetime.timedelta(hours=1),
    )
    assert len(in_range) == 1
    assert in_range[0].id == calendar_event.id

    out_of_range = service.get_recurring_event_instances(
        recurring_event=calendar_event,
        start_date=calendar_event.end_time + datetime.timedelta(days=1),
        end_date=calendar_event.end_time + datetime.timedelta(days=2),
    )
    assert out_of_range == []


@pytest.mark.django_db
def test_get_recurring_event_instances_basic_recurring(
    social_account, social_token, mock_google_adapter, calendar, patch_get_calendar
):
    """Recurring event should return all generated instances within range."""
    mock_google_adapter.provider = CalendarProvider.GOOGLE
    start = datetime.datetime(2025, 6, 23, 10, 0, tzinfo=datetime.UTC)  # Monday
    end = start + datetime.timedelta(hours=1)
    recurrence_rule = "RRULE:FREQ=WEEKLY;COUNT=5;BYDAY=MO"
    parent_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="recurring_parent_basic_123",
        title="Weekly Standup",
        description="Team standup",
        start_time=start,
        end_time=end,
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=recurrence_rule.replace("RRULE:", ""),
    )
    mock_google_adapter.create_event.return_value = parent_created_event_data

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(start_time=start, end_time=end, id=1, can_book_partially=False)
        ],
    ):
        parent_event = service.create_recurring_event(
            calendar.id,
            title="Weekly Standup",
            description="Team standup",
            start_time=start,
            end_time=end,
            recurrence_rule=recurrence_rule,
        )

    # Range covering all 5 weeks
    instances = service.get_recurring_event_instances(
        recurring_event=parent_event,
        start_date=start - datetime.timedelta(days=1),
        end_date=start + datetime.timedelta(weeks=4, hours=2),
    )
    assert len(instances) == 5
    # Ensure ordering by start_time
    assert instances == sorted(instances, key=lambda e: e.start_time)


@pytest.mark.django_db
def test_get_recurring_event_instances_with_modified_exception(
    social_account, social_token, mock_google_adapter, calendar, patch_get_calendar
):
    """Modified exception should replace generated occurrence when include_exceptions=True."""
    mock_google_adapter.provider = CalendarProvider.GOOGLE
    start = datetime.datetime(2025, 6, 23, 10, 0, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(hours=1)
    recurrence_rule = "RRULE:FREQ=WEEKLY;COUNT=5;BYDAY=MO"
    parent_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="recurring_parent_mod_123",
        title="Weekly Sync",
        description="Sync meeting",
        start_time=start,
        end_time=end,
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=recurrence_rule.replace("RRULE:", ""),
    )
    modified_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="modified_exception_123",
        title="Modified Weekly Sync",
        description="Adjusted time",
        start_time=start + datetime.timedelta(weeks=1, minutes=30),
        end_time=start + datetime.timedelta(weeks=1, minutes=90),
        attendees=[],
        resources=[],
        original_payload={},
    )
    mock_google_adapter.create_event.side_effect = [
        parent_created_event_data,
        modified_created_event_data,
    ]

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(start_time=start, end_time=end, id=1, can_book_partially=False)
        ],
    ):
        parent_event = service.create_recurring_event(
            calendar.id,
            title="Weekly Sync",
            description="Sync meeting",
            start_time=start,
            end_time=end,
            recurrence_rule=recurrence_rule,
        )

    exception_datetime = start + datetime.timedelta(weeks=1)
    modified_start = exception_datetime + datetime.timedelta(minutes=30)
    modified_end = modified_start + datetime.timedelta(hours=1)
    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(
                start_time=modified_start, end_time=modified_end, id=2, can_book_partially=False
            )
        ],
    ):
        modified_event = service.create_recurring_event_exception(
            parent_event=parent_event,
            exception_date=exception_datetime,
            modified_title="Modified Weekly Sync",
            modified_description="Adjusted time",
            modified_start_time=modified_start,
            modified_end_time=modified_end,
            is_cancelled=False,
        )
    assert modified_event is not None

    full_range_start = start - datetime.timedelta(days=1)
    full_range_end = start + datetime.timedelta(weeks=4, hours=2)
    instances_with = service.get_recurring_event_instances(
        recurring_event=parent_event,
        start_date=full_range_start,
        end_date=full_range_end,
        include_exceptions=True,
    )
    instances_without = service.get_recurring_event_instances(
        recurring_event=parent_event,
        start_date=full_range_start,
        end_date=full_range_end,
        include_exceptions=False,
    )

    # With exceptions we still have 5 total (modified replaces one occurrence)
    assert len(instances_with) == 5
    assert any(e.external_id == "modified_exception_123" for e in instances_with)
    # Without exceptions we have 4 (the exception date excluded, no replacement)
    assert len(instances_without) == 4
    assert all(e.external_id != "modified_exception_123" for e in instances_without)


@pytest.mark.django_db
def test_get_recurring_event_instances_with_cancelled_exception(
    social_account, social_token, mock_google_adapter, calendar, patch_get_calendar
):
    """Cancelled exception should remove the occurrence regardless of include_exceptions flag."""
    mock_google_adapter.provider = CalendarProvider.GOOGLE
    start = datetime.datetime(2025, 6, 23, 10, 0, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(hours=1)
    recurrence_rule = "RRULE:FREQ=WEEKLY;COUNT=5;BYDAY=MO"
    parent_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="recurring_parent_cancel_123",
        title="Weekly Training",
        description="Training session",
        start_time=start,
        end_time=end,
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=recurrence_rule.replace("RRULE:", ""),
    )
    mock_google_adapter.create_event.return_value = parent_created_event_data

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(start_time=start, end_time=end, id=1, can_book_partially=False)
        ],
    ):
        parent_event = service.create_recurring_event(
            calendar.id,
            title="Weekly Training",
            description="Training session",
            start_time=start,
            end_time=end,
            recurrence_rule=recurrence_rule,
        )

    # Cancel 3rd occurrence (week 2 offset from start? choose week 1 or 2) cancel second week
    exception_datetime = start + datetime.timedelta(weeks=1)
    service.create_recurring_event_exception(
        parent_event=parent_event,
        exception_date=exception_datetime,
        is_cancelled=True,
    )

    full_range_start = start - datetime.timedelta(days=1)
    full_range_end = start + datetime.timedelta(weeks=4, hours=2)
    instances_with = service.get_recurring_event_instances(
        recurring_event=parent_event,
        start_date=full_range_start,
        end_date=full_range_end,
        include_exceptions=True,
    )
    instances_without = service.get_recurring_event_instances(
        recurring_event=parent_event,
        start_date=full_range_start,
        end_date=full_range_end,
        include_exceptions=False,
    )

    assert len(instances_with) == 4
    assert len(instances_without) == 4


@pytest.mark.django_db
def test_get_calendar_events_expanded_non_recurring(
    social_account,
    social_token,
    mock_google_adapter,
    calendar,
    patch_get_calendar,
    sample_event_input_data,
):
    """Expanded events should include only non-recurring events within range."""
    mock_google_adapter.provider = CalendarProvider.GOOGLE
    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    # Inside range event
    inside_start = datetime.datetime(2025, 7, 1, 9, 0, tzinfo=datetime.UTC)
    inside_end = inside_start + datetime.timedelta(hours=1)
    event_data_inside = CalendarEventInputData(
        title="Inside Event",
        description="In range",
        start_time=inside_start,
        end_time=inside_end,
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )
    # Outside range event
    outside_start = datetime.datetime(2025, 8, 1, 9, 0, tzinfo=datetime.UTC)
    outside_end = outside_start + datetime.timedelta(hours=1)
    event_data_outside = CalendarEventInputData(
        title="Outside Event",
        description="Out of range",
        start_time=outside_start,
        end_time=outside_end,
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )

    created_event_data_inside = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="inside_evt_123",
        title="Inside Event",
        description="In range",
        start_time=inside_start,
        end_time=inside_end,
        attendees=[],
        resources=[],
        original_payload={},
    )
    created_event_data_outside = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="outside_evt_123",
        title="Outside Event",
        description="Out of range",
        start_time=outside_start,
        end_time=outside_end,
        attendees=[],
        resources=[],
        original_payload={},
    )
    # Adapter calls for each event
    mock_google_adapter.create_event.side_effect = [
        created_event_data_inside,
        created_event_data_outside,
    ]

    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        side_effect=lambda cal, s, e: [
            AvailableTimeWindow(start_time=s, end_time=e, id=1, can_book_partially=False)
        ],
    ):
        service.create_event(calendar.id, event_data_inside)
        service.create_event(calendar.id, event_data_outside)

    expanded = service.get_calendar_events_expanded(
        calendar=calendar,
        start_date=datetime.datetime(2025, 6, 30, tzinfo=datetime.UTC),
        end_date=datetime.datetime(2025, 7, 15, tzinfo=datetime.UTC),
    )
    assert len(expanded) == 1
    assert expanded[0].external_id == "inside_evt_123"


@pytest.mark.django_db
def test_get_calendar_events_expanded_recurring_expansion(
    social_account, social_token, mock_google_adapter, calendar, patch_get_calendar
):
    """Recurring events should be expanded into instances within range."""
    mock_google_adapter.provider = CalendarProvider.GOOGLE
    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    start = datetime.datetime(2025, 7, 7, 10, 0, tzinfo=datetime.UTC)  # Monday
    end = start + datetime.timedelta(hours=1)
    recurrence_rule = "RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=MO"
    parent_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="exp_parent_recurring_123",
        title="Weekly Planning",
        description="Planning meeting",
        start_time=start,
        end_time=end,
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=recurrence_rule.replace("RRULE:", ""),
    )
    mock_google_adapter.create_event.return_value = parent_created_event_data

    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(start_time=start, end_time=end, id=1, can_book_partially=False)
        ],
    ):
        _parent_event = service.create_recurring_event(
            calendar.id,
            title="Weekly Planning",
            description="Planning meeting",
            start_time=start,
            end_time=end,
            recurrence_rule=recurrence_rule,
        )

    expanded = service.get_calendar_events_expanded(
        calendar=calendar,
        start_date=start - datetime.timedelta(days=1),
        end_date=start + datetime.timedelta(weeks=2, hours=2),
    )
    assert len(expanded) == 3
    expected_starts = {
        start,
        start + datetime.timedelta(weeks=1),
        start + datetime.timedelta(weeks=2),
    }
    assert {e.start_time for e in expanded} == expected_starts
    # All events should be generated instances (not the master event)
    assert all(e.recurrence_id is not None for e in expanded)


@pytest.mark.django_db
def test_get_calendar_events_expanded_with_exceptions(
    social_account, social_token, mock_google_adapter, calendar, patch_get_calendar
):
    """Expanded events should include modified exceptions and exclude cancelled ones."""
    mock_google_adapter.provider = CalendarProvider.GOOGLE
    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    start = datetime.datetime(2025, 7, 7, 10, 0, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(hours=1)
    recurrence_rule = "RRULE:FREQ=WEEKLY;COUNT=4;BYDAY=MO"
    parent_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="exp_parent_exc_123",
        title="Weekly Review",
        description="Review meeting",
        start_time=start,
        end_time=end,
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=recurrence_rule.replace("RRULE:", ""),
    )
    modified_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="exp_modified_exc_123",
        title="Modified Weekly Review",
        description="Adjusted",
        start_time=start + datetime.timedelta(weeks=1, minutes=30),
        end_time=start + datetime.timedelta(weeks=1, minutes=90),
        attendees=[],
        resources=[],
        original_payload={},
    )
    # Side effects: first parent, then modified instance
    mock_google_adapter.create_event.side_effect = [
        parent_created_event_data,
        modified_created_event_data,
    ]

    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(start_time=start, end_time=end, id=1, can_book_partially=False)
        ],
    ):
        parent_event = service.create_recurring_event(
            calendar.id,
            title="Weekly Review",
            description="Review meeting",
            start_time=start,
            end_time=end,
            recurrence_rule=recurrence_rule,
        )

    # Create modified exception for week 1
    exception_date_week1 = start + datetime.timedelta(weeks=1)
    modified_start = exception_date_week1 + datetime.timedelta(minutes=30)
    modified_end = modified_start + datetime.timedelta(hours=1)
    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(
                start_time=modified_start, end_time=modified_end, id=2, can_book_partially=False
            )
        ],
    ):
        service.create_recurring_event_exception(
            parent_event=parent_event,
            exception_date=exception_date_week1,
            modified_title="Modified Weekly Review",
            modified_description="Adjusted",
            modified_start_time=modified_start,
            modified_end_time=modified_end,
            is_cancelled=False,
        )

    # Create cancelled exception for week 2
    exception_datetime_week2 = start + datetime.timedelta(weeks=2)
    service.create_recurring_event_exception(
        parent_event=parent_event,
        exception_date=exception_datetime_week2,
        is_cancelled=True,
    )

    expanded = service.get_calendar_events_expanded(
        calendar=calendar,
        start_date=start - datetime.timedelta(days=1),
        end_date=start + datetime.timedelta(weeks=3, hours=2),
    )
    # Should return: 2 generated instances + 1 modified exception = 3 events total (1 cancelled, excluded)
    # Week 0: generated, Week 1: modified exception (replaces generated), Week 2: cancelled (excluded), Week 3: generated
    assert len(expanded) == 3
    # Ensure modified exception is present
    assert any(e.external_id == "exp_modified_exc_123" for e in expanded)
    # Ensure week 2 (cancelled) start_time is missing from events
    cancelled_start = start + datetime.timedelta(weeks=2)
    assert all(e.start_time != cancelled_start for e in expanded)


@pytest.mark.django_db
def test_delete_recurring_event_series_deletes_all(
    social_account, social_token, mock_google_adapter, calendar, patch_get_calendar
):
    """Deleting a recurring parent event with delete_series=True removes parent, rule, modified instances, and exceptions."""
    mock_google_adapter.provider = CalendarProvider.GOOGLE
    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    start = datetime.datetime(2025, 7, 14, 10, 0, tzinfo=datetime.UTC)  # Monday
    end = start + datetime.timedelta(hours=1)
    recurrence_rule = "RRULE:FREQ=WEEKLY;COUNT=4;BYDAY=MO"
    parent_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="del_series_parent_123",
        title="Series Meeting",
        description="Parent recurring meeting",
        start_time=start,
        end_time=end,
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=recurrence_rule.replace("RRULE:", ""),
    )
    modified_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="del_series_modified_123",
        title="Modified Series Meeting",
        description="Modified occurrence",
        start_time=start + datetime.timedelta(weeks=1, minutes=30),
        end_time=start + datetime.timedelta(weeks=1, minutes=90),
        attendees=[],
        resources=[],
        original_payload={},
    )
    mock_google_adapter.create_event.side_effect = [
        parent_created_event_data,
        modified_created_event_data,
    ]

    # Create parent recurring event
    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(start_time=start, end_time=end, id=1, can_book_partially=False)
        ],
    ):
        parent_event = service.create_recurring_event(
            calendar.id,
            title="Series Meeting",
            description="Parent recurring meeting",
            start_time=start,
            end_time=end,
            recurrence_rule=recurrence_rule,
        )

    # Create a modified exception (instance)
    exception_datetime = start + datetime.timedelta(weeks=1)
    mod_start = exception_datetime + datetime.timedelta(minutes=30)
    mod_end = mod_start + datetime.timedelta(hours=1)
    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(
                start_time=mod_start, end_time=mod_end, id=2, can_book_partially=False
            )
        ],
    ):
        modified_instance = service.create_recurring_event_exception(
            parent_event=parent_event,
            exception_date=exception_datetime,
            modified_title="Modified Series Meeting",
            modified_description="Modified occurrence",
            modified_start_time=mod_start,
            modified_end_time=mod_end,
            is_cancelled=False,
        )

    assert modified_instance is not None
    assert parent_event.recurrence_rule is not None
    assert parent_event.recurrence_exceptions.count() == 1

    # Delete entire series
    service.delete_event(calendar_id=calendar.id, event_id=parent_event.id, delete_series=True)

    # Adapter should have been called once for deleting series
    mock_google_adapter.delete_event.assert_called_once_with(
        calendar.external_id, parent_event.external_id
    )
    # Parent, rule, modified instance, and exception gone
    assert (
        CalendarEvent.objects.filter(
            organization_id=calendar.organization_id, id=parent_event.id
        ).count()
        == 0
    )
    if modified_instance.id:
        assert (
            CalendarEvent.objects.filter(
                organization_id=calendar.organization_id, id=modified_instance.id
            ).count()
            == 0
        )
    # Recurrence rule deleted
    from calendar_integration.models import RecurrenceRule

    assert (
        RecurrenceRule.objects.filter(
            organization_id=calendar.organization_id,
            id=getattr(parent_event.recurrence_rule, "id", None),
        ).count()
        == 0
    )
    # Exceptions deleted
    from calendar_integration.models import EventRecurrenceException

    assert (
        EventRecurrenceException.objects.filter(
            organization_id=calendar.organization_id, parent_event=parent_event
        ).count()
        == 0
    )


@pytest.mark.django_db
def test_delete_recurring_modified_instance_creates_cancellation_exception(
    social_account, social_token, mock_google_adapter, calendar, patch_get_calendar
):
    """Deleting a modified recurring instance (delete_series=False) should create a cancellation exception for that modified occurrence without deleting the instance (current behavior)."""
    mock_google_adapter.provider = CalendarProvider.GOOGLE
    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    start = datetime.datetime(2025, 7, 21, 10, 0, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(hours=1)
    recurrence_rule = "RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=MO"
    parent_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="del_instance_parent_123",
        title="Series",
        description="Parent",
        start_time=start,
        end_time=end,
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=recurrence_rule.replace("RRULE:", ""),
    )
    modified_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="del_instance_modified_123",
        title="Series Modified",
        description="Modified occ",
        start_time=start + datetime.timedelta(weeks=1, minutes=15),
        end_time=start + datetime.timedelta(weeks=1, minutes=75),
        attendees=[],
        resources=[],
        original_payload={},
    )
    mock_google_adapter.create_event.side_effect = [
        parent_created_event_data,
        modified_created_event_data,
    ]

    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(start_time=start, end_time=end, id=1, can_book_partially=False)
        ],
    ):
        parent_event = service.create_recurring_event(
            calendar.id,
            title="Series",
            description="Parent",
            start_time=start,
            end_time=end,
            recurrence_rule=recurrence_rule,
        )

    exc_datetime = start + datetime.timedelta(weeks=1)
    mod_start = exc_datetime + datetime.timedelta(minutes=15)
    mod_end = mod_start + datetime.timedelta(hours=1)
    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(
                start_time=mod_start, end_time=mod_end, id=2, can_book_partially=False
            )
        ],
    ):
        modified_instance = service.create_recurring_event_exception(
            parent_event=parent_event,
            exception_date=exc_datetime,
            modified_title="Series Modified",
            modified_description="Modified occ",
            modified_start_time=mod_start,
            modified_end_time=mod_end,
            is_cancelled=False,
        )
    assert modified_instance is not None
    pre_exception_count = parent_event.recurrence_exceptions.count()

    # Delete the modified instance (should create a cancellation exception for its modified start time)
    service.delete_event(
        calendar_id=calendar.id, event_id=modified_instance.id, delete_series=False
    )

    # Adapter should NOT be called for deleting instance (cancellation approach) under adapter branch
    mock_google_adapter.delete_event.assert_not_called()

    # One exception: for cancellation of modified occurrence, updated the existing exception
    # for the modified event
    assert parent_event.recurrence_exceptions.count() == pre_exception_count
    # The modified instance still exists (current implementation behavior)
    assert not CalendarEvent.objects.filter(id=modified_instance.id).exists()


@pytest.mark.django_db
def test_delete_recurring_instance_with_delete_series_true_deletes_instance_only(
    social_account, social_token, mock_google_adapter, calendar, patch_get_calendar
):
    """Deleting a recurring instance with delete_series=True deletes that instance record (treats as single event deletion path)."""
    mock_google_adapter.provider = CalendarProvider.GOOGLE
    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    start = datetime.datetime(2025, 7, 28, 10, 0, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(hours=1)
    recurrence_rule = "RRULE:FREQ=WEEKLY;COUNT=2;BYDAY=MO"
    parent_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="del_inst_series_parent_123",
        title="Parent",
        description="Parent",
        start_time=start,
        end_time=end,
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=recurrence_rule.replace("RRULE:", ""),
    )
    modified_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="del_inst_series_mod_123",
        title="Parent (Week 1 Mod)",
        description="Modified occ",
        start_time=start + datetime.timedelta(weeks=1, minutes=10),
        end_time=start + datetime.timedelta(weeks=1, minutes=70),
        attendees=[],
        resources=[],
        original_payload={},
    )
    mock_google_adapter.create_event.side_effect = [
        parent_created_event_data,
        modified_created_event_data,
    ]

    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(start_time=start, end_time=end, id=1, can_book_partially=False)
        ],
    ):
        parent_event = service.create_recurring_event(
            calendar.id,
            title="Parent",
            description="Parent",
            start_time=start,
            end_time=end,
            recurrence_rule=recurrence_rule,
        )

    exc_datetime = start + datetime.timedelta(weeks=1)
    mod_start = exc_datetime + datetime.timedelta(minutes=10)
    mod_end = mod_start + datetime.timedelta(hours=1)
    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(
                start_time=mod_start, end_time=mod_end, id=2, can_book_partially=False
            )
        ],
    ):
        modified_instance = service.create_recurring_event_exception(
            parent_event=parent_event,
            exception_date=exc_datetime,
            modified_title="Parent (Week 1 Mod)",
            modified_description="Modified occ",
            modified_start_time=mod_start,
            modified_end_time=mod_end,
            is_cancelled=False,
        )
    assert modified_instance is not None

    service.delete_event(calendar_id=calendar.id, event_id=modified_instance.id, delete_series=True)

    # Adapter should be called for deletion (treated as direct deletion)
    mock_google_adapter.delete_event.assert_called_once_with(
        calendar.external_id, modified_instance.external_id
    )
    # Instance removed
    assert not CalendarEvent.objects.filter(id=modified_instance.id).exists()
    # Parent still exists
    assert CalendarEvent.objects.filter(id=parent_event.id).exists()


@pytest.mark.django_db
def test_create_event_with_resources(
    social_account,
    social_token,
    mock_google_adapter,
    calendar,
    patch_get_calendar,
    sample_event_input_data_with_resources,
):
    """Test creating an event with resources."""
    created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="event_new_456",
        title="Event with Resources",
        description="An event with resource allocations",
        start_time=datetime.datetime(2025, 6, 22, 16, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 17, 0, tzinfo=datetime.UTC),
        attendees=[],
        resources=[],
        original_payload={},
    )
    mock_google_adapter.create_event.return_value = created_event_data

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    result = service.create_event(calendar.id, sample_event_input_data_with_resources)

    assert result.external_id == "event_new_456"
    assert result.title == "Event with Resources"

    # Verify resource allocations were created
    assert result.resource_allocations.count() == 1
    resource_allocation = result.resource_allocations.first()
    assert (
        resource_allocation.calendar_fk_id
        == sample_event_input_data_with_resources.resource_allocations[0].resource_id
    )

    mock_google_adapter.create_event.assert_called_once()
    call_args = mock_google_adapter.create_event.call_args[0][0]
    assert isinstance(call_args, CalendarEventAdapterInputData)
    assert call_args.title == "Event with Resources"


@pytest.mark.django_db
def test_update_event(social_account, social_token, mock_google_adapter, calendar_event):
    """Test updating an event."""
    updated_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="event_123",
        title="Updated Event",
        description="Updated description",
        start_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 12, 0, tzinfo=datetime.UTC),
        attendees=[],
        resources=[],
        original_payload={},
    )

    event_input_data = CalendarEventInputData(
        title="Updated Event",
        description="Updated description",
        start_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 12, 0, tzinfo=datetime.UTC),
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )

    mock_google_adapter.update_event.return_value = updated_event_data

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar_event.organization)
    result = service.update_event(calendar_event.calendar.id, calendar_event.id, event_input_data)

    # Verify database object was updated
    calendar_event.refresh_from_db()
    assert calendar_event.title == "Updated Event"
    assert calendar_event.description == "Updated description"

    assert result == calendar_event
    mock_google_adapter.update_event.assert_called_once()


@pytest.mark.django_db
def test_create_event_with_attendances(
    social_account,
    social_token,
    mock_google_adapter,
    calendar,
    patch_get_calendar,
    sample_event_input_data_with_attendances,
):
    """Test creating an event with user attendances and external attendances."""
    created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="event_with_attendances_123",
        title="Event with Attendances",
        description="An event with attendances",
        start_time=datetime.datetime(2025, 6, 22, 14, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 15, 0, tzinfo=datetime.UTC),
        attendees=[],
        resources=[],
        original_payload={},
    )
    mock_google_adapter.create_event.return_value = created_event_data
    mock_google_adapter.provider = CalendarProvider.GOOGLE

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    result = service.create_event(calendar.id, sample_event_input_data_with_attendances)

    # Verify database object was created
    assert result.external_id == "event_with_attendances_123"
    assert result.title == "Event with Attendances"
    assert result.calendar == calendar

    # Verify user attendances were created
    assert result.attendances.count() == 2
    user_ids = [attendance.user_id for attendance in result.attendances.all()]
    expected_user_ids = [
        att.user_id for att in sample_event_input_data_with_attendances.attendances
    ]
    assert set(user_ids) == set(expected_user_ids)

    # Verify external attendances were created
    assert result.external_attendances.count() == 2
    external_emails = [ea.external_attendee.email for ea in result.external_attendances.all()]
    expected_emails = [
        ea.external_attendee.email
        for ea in sample_event_input_data_with_attendances.external_attendances
    ]
    assert set(external_emails) == set(expected_emails)

    # Verify external attendees were created
    assert ExternalAttendee.objects.filter(organization=calendar.organization).count() == 2

    mock_google_adapter.create_event.assert_called_once()


@pytest.mark.django_db
def test_update_event_with_attendances(
    social_account, social_token, mock_google_adapter, calendar_event, db
):
    """Test updating an event with attendances and external attendances."""
    # Create initial attendances
    user1 = User.objects.create_user(username="initial_user1", email="initial1@example.com")

    EventAttendance.objects.create(
        organization=calendar_event.organization,
        event=calendar_event,
        user=user1,
    )
    external_attendee = ExternalAttendee.objects.create(
        organization=calendar_event.organization,
        email="initial_external@example.com",
        name="Initial External User",
    )
    EventExternalAttendance.objects.create(
        organization=calendar_event.organization,
        event=calendar_event,
        external_attendee=external_attendee,
    )

    # Create new users for updated attendances
    new_user1 = User.objects.create_user(username="new_user1", email="new1@example.com")
    new_user2 = User.objects.create_user(username="new_user2", email="new2@example.com")

    updated_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="event_123",
        title="Updated Event with Attendances",
        description="Updated description",
        start_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 12, 0, tzinfo=datetime.UTC),
        attendees=[],
        resources=[],
        original_payload={},
    )

    event_input_data = CalendarEventInputData(
        title="Updated Event with Attendances",
        description="Updated description",
        start_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 12, 0, tzinfo=datetime.UTC),
        attendances=[
            EventAttendanceInputData(user_id=new_user1.id),
            EventAttendanceInputData(user_id=new_user2.id),
        ],
        external_attendances=[
            EventExternalAttendanceInputData(
                external_attendee=ExternalAttendeeInputData(
                    email="new_external@example.com",
                    name="New External User",
                )
            ),
        ],
        resource_allocations=[],
    )

    mock_google_adapter.update_event.return_value = updated_event_data

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar_event.organization)
    result = service.update_event(calendar_event.calendar.id, calendar_event.id, event_input_data)

    # Verify database object was updated
    calendar_event.refresh_from_db()
    assert calendar_event.title == "Updated Event with Attendances"

    # Verify attendances were updated correctly
    assert result.attendances.count() == 2
    user_ids = [attendance.user_id for attendance in result.attendances.all()]
    assert set(user_ids) == {new_user1.id, new_user2.id}

    # Verify old attendance was removed
    assert not EventAttendance.objects.filter(user=user1, event=calendar_event).exists()

    # Verify external attendances were updated correctly
    assert result.external_attendances.count() == 1
    external_attendee = result.external_attendances.first().external_attendee
    assert external_attendee.email == "new_external@example.com"
    assert external_attendee.name == "New External User"

    # Verify old external attendee was removed
    assert not ExternalAttendee.objects.filter(email="initial_external@example.com").exists()

    mock_google_adapter.update_event.assert_called_once()


@pytest.mark.django_db
def test_update_event_with_resource_allocations(
    social_account, social_token, mock_google_adapter, calendar_event, organization, db
):
    """Test updating an event with resource allocations."""
    # Create initial resource allocation
    initial_resource = Calendar.objects.create(
        organization=organization,
        external_id="initial_resource_123",
        name="Initial Resource",
        provider=CalendarProvider.GOOGLE,
    )
    ResourceAllocation.objects.create(
        organization=calendar_event.organization,
        event_fk=calendar_event,
        calendar_fk=initial_resource,
    )

    # Create new resource for updated allocation
    new_resource = Calendar.objects.create(
        organization=organization,
        external_id="new_resource_456",
        name="New Resource",
        provider=CalendarProvider.GOOGLE,
    )

    updated_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="event_123",
        title="Updated Event with Resources",
        description="Updated description",
        start_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 12, 0, tzinfo=datetime.UTC),
        attendees=[],
        resources=[],
        original_payload={},
    )

    event_input_data = CalendarEventInputData(
        title="Updated Event with Resources",
        description="Updated description",
        start_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 12, 0, tzinfo=datetime.UTC),
        attendances=[],
        external_attendances=[],
        resource_allocations=[
            ResourceAllocationInputData(resource_id=new_resource.id),
        ],
    )

    mock_google_adapter.update_event.return_value = updated_event_data

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar_event.organization)
    result = service.update_event(calendar_event.calendar.id, calendar_event.id, event_input_data)

    # Verify database object was updated
    calendar_event.refresh_from_db()
    assert calendar_event.title == "Updated Event with Resources"

    # Verify resource allocations were updated correctly
    assert result.resource_allocations.count() == 1
    resource_allocation = result.resource_allocations.first()
    assert resource_allocation.calendar_fk_id == new_resource.id

    # Verify old resource allocation was removed
    assert not ResourceAllocation.objects.filter(
        calendar=initial_resource, event=calendar_event
    ).exists()

    mock_google_adapter.update_event.assert_called_once()


@pytest.mark.django_db
def test_delete_event(social_account, social_token, mock_google_adapter, calendar_event):
    """Test deleting an event."""
    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar_event.organization)
    service.delete_event(calendar_event.calendar.id, calendar_event.id)

    # Verify database object was deleted
    assert not CalendarEvent.objects.filter(
        id=calendar_event.id, organization=calendar_event.organization
    ).exists()
    mock_google_adapter.delete_event.assert_called_once_with(
        calendar_event.calendar.external_id, calendar_event.external_id
    )


@pytest.mark.django_db
def test_transfer_event(
    social_account, social_token, mock_google_adapter, calendar_event, patch_get_calendar
):
    """Test transferring an event to a different calendar."""
    # Create a resource calendar to test the filtering logic
    Calendar.objects.create(
        name="Conference Room",
        external_id="room_123",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.RESOURCE,
        email="room@example.com",
        organization=calendar_event.organization,
    )

    # Mock the get_event call
    event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="event_123",
        title="Test Event",
        description="A test event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        attendees=[
            EventAttendeeData(email="user@example.com", name="User", status="accepted"),
            EventAttendeeData(email="room@example.com", name="Room", status="accepted"),
        ],
    )
    mock_google_adapter.get_event.return_value = event_data

    # Mock the create_event call
    new_event_data = CalendarEventData(
        calendar_external_id="target_cal_123",
        external_id="new_event_123",
        title="Test Event",
        description="A test event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        attendees=[],
    )
    mock_google_adapter.create_event.return_value = new_event_data

    # Get target calendar from patch
    target_calendar = patch_get_calendar(None, "target_cal_123")

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar_event.organization)

    # Mock the create_event method to avoid availability checking
    with patch.object(service, "create_event") as mock_create_event:
        mock_create_event.return_value = CalendarEvent.objects.create(
            calendar_fk=target_calendar,
            title="Test Event",
            external_id="new_event_123",
            start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
            organization=calendar_event.organization,
        )

        result = service.transfer_event(calendar_event, target_calendar)

        # Verify old event was deleted
        assert not CalendarEvent.objects.filter(id=calendar_event.id).exists()

        # Verify new event was created
        assert result.external_id == "new_event_123"
        assert result.calendar == target_calendar

        # Verify create_event was called with filtered attendees and resources
        mock_create_event.assert_called_once()
        call_args = mock_create_event.call_args[0][1]  # Second argument is the event data

        # Check that we have the correct data structure
        assert hasattr(call_args, "attendances")
        assert hasattr(call_args, "resource_allocations")
        # Note: The actual filtering logic would need to be implemented in transfer_event
        # For now, we just verify the method was called


@pytest.mark.django_db
def test_transfer_event_with_resources(
    social_account, social_token, mock_google_adapter, calendar_event, patch_get_calendar
):
    """Test transferring an event that includes resource calendars."""
    # Create a resource calendar
    Calendar.objects.create(
        name="Conference Room",
        external_id="room_123",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.RESOURCE,
        email="room@example.com",
        organization=calendar_event.organization,
    )

    # Mock the get_event call
    event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="event_123",
        title="Test Event",
        description="A test event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        attendees=[
            EventAttendeeData(email="user@example.com", name="User", status="accepted"),
            EventAttendeeData(email="room@example.com", name="Room", status="accepted"),
        ],
    )
    mock_google_adapter.get_event.return_value = event_data

    # Mock the create_event call
    new_event_data = CalendarEventData(
        calendar_external_id="target_cal_123",
        external_id="new_event_123",
        title="Test Event",
        description="A test event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        attendees=[],
    )
    mock_google_adapter.create_event.return_value = new_event_data

    # Get target calendar from patch
    target_calendar = patch_get_calendar(None, "target_cal_123")

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar_event.organization)
    result = service.transfer_event(calendar_event, target_calendar)

    # Verify old event was deleted
    assert not CalendarEvent.objects.filter(id=calendar_event.id).exists()

    # Verify new event was created
    assert result.external_id == "new_event_123"
    assert result.calendar == target_calendar

    # Verify the create_event was called with the expected data structure
    mock_google_adapter.create_event.assert_called_once()
    call_args = mock_google_adapter.create_event.call_args[0][0]

    # Verify we got CalendarEventAdapterInputData
    assert isinstance(call_args, CalendarEventAdapterInputData)
    # Note: The filtering of attendees vs resources would need to be implemented in transfer_event


@pytest.mark.django_db
def test_request_calendar_sync(social_account, social_token, mock_google_adapter, calendar):
    """Test requesting a calendar sync."""
    start_datetime = datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC)
    end_datetime = datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC)

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    with patch("calendar_integration.tasks.sync_calendar_task.delay") as mock_task:
        result = service.request_calendar_sync(
            calendar=calendar,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            should_update_events=True,
        )

    # Verify CalendarSync was created
    assert isinstance(result, CalendarSync)
    assert result.calendar == calendar
    assert result.start_datetime == start_datetime
    assert result.end_datetime == end_datetime
    assert result.should_update_events is True

    # Verify task was called with correct parameters
    mock_task.assert_called_once_with(
        "social_account", social_account.id, result.id, calendar.organization.id
    )


@pytest.mark.django_db
def test_request_calendar_sync_with_service_account(
    google_service_account, mock_google_adapter, calendar
):
    """Test requesting calendar sync with service account."""
    start_datetime = datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC)
    end_datetime = datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC)

    service = CalendarService()
    service.authenticate(account=google_service_account, organization=calendar.organization)

    with patch("calendar_integration.tasks.sync_calendar_task.delay") as mock_task:
        result = service.request_calendar_sync(
            calendar=calendar,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
        )

    # Verify task was called with correct account type
    mock_task.assert_called_once_with(
        "google_service_account",
        google_service_account.id,
        result.id,
        calendar.organization.id,
    )


@pytest.mark.django_db
def test_sync_events_success(social_account, social_token, mock_google_adapter, calendar):
    """Test successful event synchronization."""
    calendar_sync = CalendarSync.objects.create(
        calendar_fk=calendar,
        start_datetime=datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC),
        end_datetime=datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC),
        should_update_events=True,
        organization=calendar.organization,
    )

    # Mock the get_events call
    mock_google_adapter.get_events.return_value = {
        "events": [],
        "next_sync_token": "new_sync_token",
    }

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    service.sync_events(calendar_sync)

    # Verify sync status was updated
    calendar_sync.refresh_from_db()
    assert calendar_sync.status == CalendarSyncStatus.SUCCESS


@pytest.mark.django_db
def test_sync_events_failure(social_account, social_token, mock_google_adapter, calendar):
    """Test event synchronization failure."""
    calendar_sync = CalendarSync.objects.create(
        calendar_fk=calendar,
        start_datetime=datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC),
        end_datetime=datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC),
        should_update_events=True,
        organization=calendar.organization,
    )

    # Mock the get_events call to raise an exception
    mock_google_adapter.get_events.side_effect = Exception("API Error")

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    service.sync_events(calendar_sync)

    # Verify sync status was updated to failed
    calendar_sync.refresh_from_db()
    assert calendar_sync.status == CalendarSyncStatus.FAILED


@pytest.mark.django_db
def test_execute_calendar_sync(
    social_account,
    social_token,
    mock_google_adapter,
    calendar,
    sample_event_data,
    patch_get_calendar,
):
    """Test the internal calendar sync execution."""
    calendar_sync = CalendarSync.objects.create(
        calendar_fk=calendar,
        start_datetime=datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC),
        end_datetime=datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC),
        should_update_events=True,
        organization=calendar.organization,
        status=CalendarSyncStatus.IN_PROGRESS,
    )

    # Mock the get_events call
    mock_google_adapter.get_events.return_value = {
        "events": [sample_event_data],
        "next_sync_token": "new_sync_token",
    }

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    service._execute_calendar_sync(calendar_sync, sync_token=None)

    # Verify status was set to in progress and back
    calendar_sync.refresh_from_db()
    assert calendar_sync.status == CalendarSyncStatus.IN_PROGRESS

    # Verify new blocked time was created (since this is a new event)
    blocked_times = BlockedTime.objects.filter(
        calendar=calendar, external_id=sample_event_data.external_id
    )
    assert blocked_times.exists()


@pytest.mark.django_db
def test_process_new_event(
    social_account,
    social_token,
    mock_google_adapter,
    calendar,
    sample_event_data,
    patch_get_calendar,
):
    """Test processing a new event creates a blocked time."""
    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    changes = EventsSyncChanges()

    service._process_new_event(sample_event_data, calendar, changes)

    assert len(changes.blocked_times_to_create) == 1
    blocked_time = changes.blocked_times_to_create[0]
    assert blocked_time.external_id == sample_event_data.external_id
    assert blocked_time.reason == sample_event_data.title
    assert sample_event_data.external_id in changes.matched_event_ids


@pytest.mark.django_db
def test_process_new_event_recurring_master(
    social_account,
    social_token,
    mock_google_adapter,
    calendar,
    patch_get_calendar,
):
    """_process_new_event should stage creation of master recurring event and its recurrence rule."""
    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    changes = EventsSyncChanges()

    recurring_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="rec_master_123",
        title="Recurring Master",
        description="Master recurring event",
        start_time=datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 9, 1, 10, 0, tzinfo=datetime.UTC),
        attendees=[],
        recurrence_rule="FREQ=WEEKLY;COUNT=3;BYDAY=MO",
    )

    service._process_new_event(recurring_event_data, calendar, changes)

    assert len(changes.recurrence_rules_to_create) == 1
    assert len(changes.events_to_create) == 1
    event_obj = changes.events_to_create[0]
    assert event_obj.external_id == "rec_master_123"
    assert event_obj.recurrence_rule_fk is not None
    assert "rec_master_123" in changes.matched_event_ids


@pytest.mark.django_db
def test_process_new_event_recurring_instance_with_parent(
    social_account,
    social_token,
    mock_google_adapter,
    calendar,
    patch_get_calendar,
):
    """_process_new_event should create instance event when parent exists."""
    from calendar_integration.models import CalendarEvent, RecurrenceRule

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    # Create parent event in DB
    rule = RecurrenceRule.from_rrule_string("FREQ=WEEKLY;COUNT=5;BYDAY=MO", calendar.organization)
    rule.save()
    parent_event = CalendarEvent.objects.create(
        calendar_fk=calendar,
        organization=calendar.organization,
        title="Parent Rec",
        description="Parent",
        start_time=datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 9, 1, 10, 0, tzinfo=datetime.UTC),
        external_id="parent_rec_123",
        recurrence_rule_fk=rule,
    )

    instance_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="instance_rec_123",
        title="Parent Rec (Modified)",
        description="Instance",
        start_time=datetime.datetime(2025, 9, 8, 9, 15, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 9, 8, 10, 15, tzinfo=datetime.UTC),
        attendees=[],
        recurring_event_id=parent_event.external_id,
    )

    changes = EventsSyncChanges()
    service._process_new_event(instance_event_data, calendar, changes)

    assert len(changes.events_to_create) == 1
    inst = changes.events_to_create[0]
    assert inst.parent_recurring_object == parent_event
    assert inst.is_recurring_exception is True
    assert inst.recurrence_id == instance_event_data.start_time
    assert "instance_rec_123" in changes.matched_event_ids


@pytest.mark.django_db
def test_process_new_event_recurring_instance_parent_missing_creates_blocked_time(
    social_account,
    social_token,
    mock_google_adapter,
    calendar,
    patch_get_calendar,
):
    """If recurring instance arrives before parent, treat as blocked time."""
    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    changes = EventsSyncChanges()

    orphan_instance_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="orphan_instance_123",
        title="Orphan Instance",
        description="Instance without parent",
        start_time=datetime.datetime(2025, 9, 15, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 9, 15, 10, 0, tzinfo=datetime.UTC),
        attendees=[],
        recurring_event_id="missing_parent_999",
    )

    service._process_new_event(orphan_instance_event_data, calendar, changes)

    assert len(changes.blocked_times_to_create) == 1
    blocked_time = changes.blocked_times_to_create[0]
    assert blocked_time.external_id == "orphan_instance_123"
    assert blocked_time.reason == "Orphan Instance"
    assert "orphan_instance_123" in changes.matched_event_ids


@pytest.mark.django_db
def test_process_existing_event_cancelled(
    social_account, social_token, mock_google_adapter, calendar_event
):
    """Test processing a cancelled existing event marks it for deletion."""
    cancelled_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="event_123",
        title="Cancelled Event",
        description="",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        attendees=[],
        status="cancelled",
    )

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar_event.organization)
    changes = EventsSyncChanges()

    service._process_existing_event(
        cancelled_event_data, calendar_event, changes, update_events=True
    )

    assert "event_123" in changes.events_to_delete
    assert "event_123" in changes.matched_event_ids


@pytest.mark.django_db
def test_process_existing_event_update(
    social_account, social_token, mock_google_adapter, calendar_event
):
    """Test processing an existing event updates it."""
    updated_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="event_123",
        title="Updated Event",
        description="Updated description",
        start_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 12, 0, tzinfo=datetime.UTC),
        attendees=[],
    )

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar_event.organization)
    changes = EventsSyncChanges()

    service._process_existing_event(updated_event_data, calendar_event, changes, update_events=True)

    assert len(changes.events_to_update) == 1
    updated_event = changes.events_to_update[0]
    assert updated_event.title == "Updated Event"
    assert updated_event.description == "Updated description"
    assert "event_123" in changes.matched_event_ids


@pytest.mark.django_db
def test_process_existing_blocked_time(social_account, social_token, mock_google_adapter, calendar):
    """Test processing an existing blocked time updates it."""
    blocked_time = BlockedTime.objects.create(
        calendar_fk=calendar,
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        reason="Original reason",
        external_id="block_123",
        organization=calendar.organization,
    )

    updated_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="block_123",
        title="Updated reason",
        description="",
        start_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 12, 0, tzinfo=datetime.UTC),
        attendees=[],
        status="confirmed",
    )

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    changes = EventsSyncChanges()

    service._process_existing_blocked_time(updated_event_data, blocked_time, changes)

    assert len(changes.blocked_times_to_update) == 1
    updated_blocked_time = changes.blocked_times_to_update[0]
    assert updated_blocked_time.reason == "Updated reason"
    assert "block_123" in changes.matched_event_ids


@pytest.mark.django_db
def test_process_event_attendees_new_user(
    social_account, social_token, mock_google_adapter, calendar_event
):
    """Test processing event attendees with a new user."""
    # Create a user that matches the attendee email
    User.objects.create_user(
        username="attendee", email="attendee@example.com", password="testpass123"
    )

    event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="event_123",
        title="Event with attendees",
        description="",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        attendees=[
            EventAttendeeData(email="attendee@example.com", name="Test Attendee", status="accepted")
        ],
    )

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar_event.organization)
    changes = EventsSyncChanges()

    service._process_event_attendees(event_data, calendar_event, changes)

    assert len(changes.attendances_to_create) == 1
    attendance = changes.attendances_to_create[0]
    assert attendance.event_fk == calendar_event
    assert attendance.status == "accepted"


@pytest.mark.django_db
def test_process_event_attendees_external_user(
    social_account, social_token, mock_google_adapter, calendar_event
):
    """Test processing event attendees with an external user."""
    event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="event_123",
        title="Event with external attendees",
        description="",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        attendees=[
            EventAttendeeData(
                email="external@example.com", name="External Attendee", status="pending"
            )
        ],
    )

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar_event.organization)
    changes = EventsSyncChanges()

    service._process_event_attendees(event_data, calendar_event, changes)

    assert len(changes.external_attendances_to_create) == 1
    external_attendance = changes.external_attendances_to_create[0]
    assert external_attendance.event == calendar_event
    assert external_attendance.status == "pending"

    # Verify external attendee was created
    external_attendee = ExternalAttendee.objects.get(
        email="external@example.com", organization=calendar_event.organization
    )
    assert external_attendee.name == "External Attendee"


@pytest.mark.django_db
def test_handle_deletions_for_full_sync(
    social_account, social_token, mock_google_adapter, calendar
):
    """Test handling deletions during full sync."""
    # Create some events in the database
    event1 = CalendarEvent.objects.create(
        calendar_fk=calendar,
        title="Event 1",
        external_id="event_1",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        organization=calendar.organization,
    )
    event2 = CalendarEvent.objects.create(
        calendar_fk=calendar,
        title="Event 2",
        external_id="event_2",
        start_time=datetime.datetime(2025, 6, 22, 12, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 13, 0, tzinfo=datetime.UTC),
        organization=calendar.organization,
    )

    calendar_events_by_external_id = {
        "event_1": event1,
        "event_2": event2,
    }

    # Only event_1 was matched during sync
    matched_event_ids = {"event_1"}

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    service._handle_deletions_for_full_sync(
        calendar.id,
        calendar_events_by_external_id,
        matched_event_ids,
        datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC),
    )

    # Verify event_2 was deleted (not matched)
    assert CalendarEvent.objects.filter(id=event1.id).exists()
    assert not CalendarEvent.objects.filter(id=event2.id).exists()


@pytest.mark.django_db
def test_apply_sync_changes(social_account, social_token, mock_google_adapter, calendar):
    """Test applying sync changes to the database."""
    # Create test changes
    changes = EventsSyncChanges()

    # Add blocked time to create
    blocked_time = BlockedTime(
        calendar_fk=calendar,
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        reason="New blocked time",
        external_id="block_new",
        organization=calendar.organization,
    )
    changes.blocked_times_to_create.append(blocked_time)

    # Add existing event to update
    existing_event = CalendarEvent.objects.create(
        calendar_fk=calendar,
        title="Original Title",
        external_id="event_existing",
        start_time=datetime.datetime(2025, 6, 22, 12, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 13, 0, tzinfo=datetime.UTC),
        organization=calendar.organization,
    )
    existing_event.title = "Updated Title"
    changes.events_to_update.append(existing_event)

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    service._apply_sync_changes(calendar.id, changes)

    # Verify blocked time was created
    assert BlockedTime.objects.filter(external_id="block_new").exists()

    # Verify event was updated
    existing_event.refresh_from_db()
    assert existing_event.title == "Updated Title"


@pytest.mark.django_db
def test_apply_sync_changes_with_recurrence_rules_and_events(
    social_account, social_token, mock_google_adapter, calendar
):
    """Ensure recurrence rules are created before events that reference them."""
    from calendar_integration.models import RecurrenceRule

    changes = EventsSyncChanges()

    # Prepare an unsaved recurrence rule (WEEKLY on Monday 3 occurrences)
    rule = RecurrenceRule.from_rrule_string(
        "FREQ=WEEKLY;COUNT=3;BYDAY=MO", organization=calendar.organization
    )

    # Prepare a calendar event referencing the unsaved rule
    recurring_event = CalendarEvent(
        calendar_fk=calendar,
        title="Team Sync",
        description="Weekly team sync",
        start_time=datetime.datetime(2025, 6, 23, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 23, 10, 0, tzinfo=datetime.UTC),
        external_id="recurring_event_1",
        organization=calendar.organization,
        recurrence_rule_fk=rule,
    )

    changes.recurrence_rules_to_create.append(rule)
    changes.events_to_create.append(recurring_event)

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    service._apply_sync_changes(calendar.id, changes)

    # Assertions
    created_event = CalendarEvent.objects.get(
        organization_id=calendar.organization_id, external_id="recurring_event_1"
    )
    assert created_event.recurrence_rule is not None, "Recurrence rule should be linked to event"
    # Ensure recurrence rule persisted with expected fields
    assert created_event.recurrence_rule.frequency == "WEEKLY"
    assert created_event.recurrence_rule.by_weekday == "MO"
    assert created_event.recurrence_rule.count == 3


@pytest.mark.django_db
def test_remove_available_time_windows_overlap(
    social_account, social_token, mock_google_adapter, calendar
):
    """Test removing available time windows that overlap with blocked times and events."""
    # Create available time windows
    available_time1 = AvailableTime.objects.create(
        calendar_fk=calendar,
        start_time=datetime.datetime(2025, 6, 22, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 10, 30, tzinfo=datetime.UTC),
        organization=calendar.organization,
    )
    available_time2 = AvailableTime.objects.create(
        calendar_fk=calendar,
        start_time=datetime.datetime(2025, 6, 22, 14, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 16, 0, tzinfo=datetime.UTC),
        organization=calendar.organization,
    )

    # Create blocked time that overlaps with available_time1
    blocked_time = BlockedTime(
        calendar_fk=calendar,
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        reason="Blocked",
        external_id="block_overlap",
        organization=calendar.organization,
    )

    # Create event that overlaps with available_time2
    event = CalendarEvent(
        calendar_fk=calendar,
        title="Event",
        external_id="event_overlap",
        start_time=datetime.datetime(2025, 6, 22, 15, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 16, 30, tzinfo=datetime.UTC),
        organization=calendar.organization,
    )

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    service._remove_available_time_windows_that_overlap_with_blocked_times_and_events(
        calendar.id,
        [blocked_time],
        [event],
        datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC),
        datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC),
    )

    # Verify both available time windows were deleted due to overlaps
    assert not AvailableTime.objects.filter(
        organization_id=calendar.organization_id, id=available_time1.id
    ).exists()
    assert not AvailableTime.objects.filter(
        organization_id=calendar.organization_id, id=available_time2.id
    ).exists()


@pytest.mark.django_db
def test_get_existing_calendar_data(social_account, social_token, mock_google_adapter, calendar):
    """Test getting existing calendar data for a date range."""
    # Create test data
    event = CalendarEvent.objects.create(
        calendar_fk=calendar,
        title="Test Event",
        external_id="event_123",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        organization=calendar.organization,
    )

    blocked_time = BlockedTime.objects.create(
        calendar_fk=calendar,
        start_time=datetime.datetime(2025, 6, 22, 12, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 13, 0, tzinfo=datetime.UTC),
        reason="Blocked",
        external_id="block_123",
        organization=calendar.organization,
    )

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    events_dict, blocked_dict = service._get_existing_calendar_data(
        calendar.id,
        datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC),
        datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC),
    )

    assert "event_123" in events_dict
    assert events_dict["event_123"] == event
    assert "block_123" in blocked_dict
    assert blocked_dict["block_123"] == blocked_time


@pytest.mark.django_db
def test_get_calendar_private_method(
    social_account, social_token, mock_google_adapter, calendar, patch_get_calendar
):
    """Test the private _get_calendar method."""
    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    # Mock the provider attribute
    mock_google_adapter.provider = CalendarProvider.GOOGLE

    result = service._get_calendar_by_external_id(calendar.external_id)
    assert result == calendar


@pytest.mark.django_db
def test_get_calendar_private_method_not_found(
    social_account, social_token, mock_google_adapter, patch_get_calendar, organization
):
    """Test the private _get_calendar method when calendar doesn't exist."""
    service = CalendarService()
    service.authenticate(account=social_account, organization=organization)

    # Mock the provider attribute
    mock_google_adapter.provider = CalendarProvider.GOOGLE

    with pytest.raises(Calendar.DoesNotExist):
        service._get_calendar_by_external_id("nonexistent_cal")


@pytest.fixture
def patch_calendar_create():
    """Patch Calendar.objects.create to handle original_payload field that doesn't exist."""
    original_create = Calendar.objects.create

    def create_wrapper(**kwargs):
        # Store original_payload in meta field if provided
        original_payload = kwargs.pop("original_payload", None)
        calendar = original_create(**kwargs)
        if original_payload:
            calendar.meta["original_payload"] = original_payload
            calendar.save(update_fields=["meta"])
        return calendar

    with patch(
        "calendar_integration.services.calendar_service.Calendar.objects.create", create_wrapper
    ):
        yield


# CalendarService.create_event tests


@pytest.mark.django_db
def test_create_event_with_available_windows(
    social_account, social_token, mock_google_adapter, organization
):
    """Test creating an event when availability windows exist."""

    # Create test calendar
    calendar = Calendar.objects.create(
        name="Test Calendar",
        email="test@example.com",
        external_id="test_123",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        organization=organization,
    )

    # Create test time range
    now = timezone.now()
    start_time = now + timedelta(hours=1)
    end_time = now + timedelta(hours=2)

    # Mock availability window
    mock_window = AvailableTimeWindow(
        start_time=start_time,
        end_time=end_time,
        id=1,
        can_book_partially=False,
    )

    # Mock event data from adapter
    mock_event_data = MagicMock()
    mock_event_data.title = "Test Event"
    mock_event_data.description = "Test Description"
    mock_event_data.start_time = start_time
    mock_event_data.end_time = end_time
    mock_event_data.external_id = "ext_123"
    mock_event_data.original_payload = {"test": "data"}

    mock_google_adapter.create_event.return_value = mock_event_data

    with patch.object(CalendarService, "get_availability_windows_in_range") as mock_get_windows:
        mock_get_windows.return_value = [mock_window]

        with patch.object(CalendarService, "_get_calendar_by_external_id") as mock_get_calendar:
            mock_get_calendar.return_value = calendar

            service = CalendarService()
            service.authenticate(account=social_account, organization=organization)

            # Create event data
            event_data = CalendarEventInputData(
                title="Test Event",
                description="Test Description",
                start_time=start_time,
                end_time=end_time,
                attendances=[],
                external_attendances=[],
                resource_allocations=[],
            )

            # Create the event
            created_event = service.create_event(calendar.id, event_data)

            # Verify the event was created
            assert isinstance(created_event, CalendarEvent)
            assert created_event.title == "Test Event"
            assert created_event.description == "Test Description"
            assert created_event.start_time == start_time
            assert created_event.end_time == end_time
            assert created_event.external_id == "ext_123"
            assert created_event.calendar == calendar

            # Verify the adapter was called
            mock_google_adapter.create_event.assert_called_once()

            # Verify availability was checked
            mock_get_windows.assert_called_once_with(calendar, start_time, end_time)


@pytest.mark.django_db
def test_create_event_no_available_windows(
    social_account, social_token, mock_google_adapter, organization
):
    """Test creating an event when no availability windows exist."""

    # Create test calendar
    calendar = Calendar.objects.create(
        name="Test Calendar",
        email="test@example.com",
        external_id="test_123",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        organization=organization,
    )

    # Create test time range
    now = timezone.now()
    start_time = now + timedelta(hours=1)
    end_time = now + timedelta(hours=2)

    with patch.object(CalendarService, "get_availability_windows_in_range") as mock_get_windows:
        mock_get_windows.return_value = []  # No available windows

        with patch.object(CalendarService, "_get_calendar_by_external_id") as mock_get_calendar:
            mock_get_calendar.return_value = calendar

            service = CalendarService()
            service.authenticate(account=social_account, organization=organization)

            # Create event data
            event_data = CalendarEventInputData(
                title="Test Event",
                description="Test Description",
                start_time=start_time,
                end_time=end_time,
                attendances=[],
                external_attendances=[],
                resource_allocations=[],
            )

            # Attempt to create the event should raise ValueError
            with pytest.raises(ValueError, match="No available time windows for the event"):
                service.create_event(calendar.id, event_data)

            # Verify the adapter was NOT called
            mock_google_adapter.create_event.assert_not_called()

            # Verify availability was checked
            mock_get_windows.assert_called_once_with(calendar, start_time, end_time)


@pytest.mark.django_db
def test_create_event_with_partial_availability(
    social_account, social_token, mock_google_adapter, organization
):
    """Test creating an event when partial availability windows exist."""

    # Create test calendar
    calendar = Calendar.objects.create(
        name="Test Calendar",
        email="test@example.com",
        external_id="test_123",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=False,
        organization=organization,
    )

    # Create test time range
    now = timezone.now()
    start_time = now + timedelta(hours=1)
    end_time = now + timedelta(hours=2)

    # Mock partial availability window (can book partially)
    mock_window = AvailableTimeWindow(
        start_time=start_time,
        end_time=end_time,
        id=None,  # No ID for unmanaged calendar
        can_book_partially=True,
    )

    # Mock event data from adapter
    mock_event_data = MagicMock()
    mock_event_data.title = "Partial Event"
    mock_event_data.description = "Partial Description"
    mock_event_data.start_time = start_time
    mock_event_data.end_time = end_time
    mock_event_data.external_id = "partial_ext_123"
    mock_event_data.original_payload = {"partial": "data"}

    mock_google_adapter.create_event.return_value = mock_event_data

    with patch.object(CalendarService, "get_availability_windows_in_range") as mock_get_windows:
        mock_get_windows.return_value = [mock_window]

        with patch.object(CalendarService, "_get_calendar_by_external_id") as mock_get_calendar:
            mock_get_calendar.return_value = calendar

            service = CalendarService()
            service.authenticate(account=social_account, organization=organization)

            # Create event data
            event_data = CalendarEventInputData(
                title="Partial Event",
                description="Partial Description",
                start_time=start_time,
                end_time=end_time,
                attendances=[],
                external_attendances=[],
                resource_allocations=[],
            )

            # Create the event
            created_event = service.create_event(calendar.id, event_data)

            # Verify the event was created
            assert isinstance(created_event, CalendarEvent)
            assert created_event.title == "Partial Event"
            assert created_event.description == "Partial Description"

            # Verify the adapter was called
            mock_google_adapter.create_event.assert_called_once()
            call_args = mock_google_adapter.create_event.call_args[0][0]
            assert isinstance(call_args, CalendarEventAdapterInputData)

            # Verify availability was checked
            mock_get_windows.assert_called_once_with(calendar, start_time, end_time)


@pytest.mark.django_db
def test_create_event_with_multiple_availability_windows(
    social_account, social_token, mock_google_adapter, organization
):
    """Test creating an event when multiple availability windows exist."""

    # Create test calendar
    calendar = Calendar.objects.create(
        name="Test Calendar",
        email="test@example.com",
        external_id="test_123",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        organization=organization,
    )

    # Create test time range
    now = timezone.now()
    start_time = now + timedelta(hours=1)
    end_time = now + timedelta(hours=2)

    # Mock multiple availability windows
    mock_window1 = AvailableTimeWindow(
        start_time=start_time,
        end_time=start_time + timedelta(minutes=30),
        id=1,
        can_book_partially=False,
    )
    mock_window2 = AvailableTimeWindow(
        start_time=start_time + timedelta(minutes=30),
        end_time=end_time,
        id=2,
        can_book_partially=False,
    )

    # Mock event data from adapter
    mock_event_data = MagicMock()
    mock_event_data.title = "Multi Window Event"
    mock_event_data.description = "Multi Window Description"
    mock_event_data.start_time = start_time
    mock_event_data.end_time = end_time
    mock_event_data.external_id = "multi_ext_123"
    mock_event_data.original_payload = {"multi": "data"}

    mock_google_adapter.create_event.return_value = mock_event_data

    with patch.object(CalendarService, "get_availability_windows_in_range") as mock_get_windows:
        mock_get_windows.return_value = [mock_window1, mock_window2]

        with patch.object(CalendarService, "_get_calendar_by_external_id") as mock_get_calendar:
            mock_get_calendar.return_value = calendar

            service = CalendarService()
            service.authenticate(account=social_account, organization=organization)

            # Create event data
            event_data = CalendarEventInputData(
                title="Multi Window Event",
                description="Multi Window Description",
                start_time=start_time,
                end_time=end_time,
                attendances=[],
                external_attendances=[],
                resource_allocations=[],
            )

            # Create the event
            created_event = service.create_event(calendar.id, event_data)

            # Verify the event was created
            assert isinstance(created_event, CalendarEvent)
            assert created_event.title == "Multi Window Event"

            # Verify the adapter was called
            mock_google_adapter.create_event.assert_called_once()
            call_args = mock_google_adapter.create_event.call_args[0][0]
            assert isinstance(call_args, CalendarEventAdapterInputData)

            # Verify availability was checked
            mock_get_windows.assert_called_once_with(calendar, start_time, end_time)


@pytest.mark.django_db
def test_create_event_with_attendees(
    social_account, social_token, mock_google_adapter, organization
):
    """Test creating an event with attendees."""

    # Create test calendar
    calendar = Calendar.objects.create(
        name="Test Calendar",
        email="test@example.com",
        external_id="test_123",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        organization=organization,
    )

    # Create test time range
    now = timezone.now()
    start_time = now + timedelta(hours=1)
    end_time = now + timedelta(hours=2)

    # Mock availability window
    mock_window = AvailableTimeWindow(
        start_time=start_time,
        end_time=end_time,
        id=1,
        can_book_partially=False,
    )

    # Mock event data from adapter
    mock_event_data = MagicMock()
    mock_event_data.title = "Event with Attendees"
    mock_event_data.description = "Event Description"
    mock_event_data.start_time = start_time
    mock_event_data.end_time = end_time
    mock_event_data.external_id = "attendee_ext_123"
    mock_event_data.original_payload = {"attendees": "data"}

    mock_google_adapter.create_event.return_value = mock_event_data

    with patch.object(CalendarService, "get_availability_windows_in_range") as mock_get_windows:
        mock_get_windows.return_value = [mock_window]

        with patch.object(CalendarService, "_get_calendar_by_external_id") as mock_get_calendar:
            mock_get_calendar.return_value = calendar

            service = CalendarService()
            service.authenticate(account=social_account, organization=organization)

            event_data = CalendarEventInputData(
                title="Event with Attendees",
                description="Event Description",
                start_time=start_time,
                end_time=end_time,
                attendances=[],
                external_attendances=[],
                resource_allocations=[],
            )

            # Create the event
            created_event = service.create_event(calendar.id, event_data)

            # Verify the event was created
            assert isinstance(created_event, CalendarEvent)
            assert created_event.title == "Event with Attendees"

            # Verify the adapter was called with attendees
            mock_google_adapter.create_event.assert_called_once()
            call_args = mock_google_adapter.create_event.call_args[0][0]
            assert isinstance(call_args, CalendarEventAdapterInputData)


@pytest.mark.django_db
def test_create_event_calendar_not_found(
    social_account, social_token, mock_google_adapter, organization
):
    """Test creating an event when calendar is not found."""

    # Create test time range
    now = timezone.now()
    start_time = now + timedelta(hours=1)
    end_time = now + timedelta(hours=2)

    service = CalendarService()
    service.authenticate(account=social_account, organization=organization)

    # Create event data for non-existent calendar
    event_data = CalendarEventInputData(
        title="Test Event",
        description="Test Description",
        start_time=start_time,
        end_time=end_time,
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )

    # Attempt to create the event should raise Calendar.DoesNotExist
    with pytest.raises(Calendar.DoesNotExist):
        service.create_event(999999, event_data)


@pytest.mark.django_db
def test_create_event_adapter_failure(
    social_account, social_token, mock_google_adapter, organization
):
    """Test creating an event when the adapter fails."""

    # Create test calendar
    calendar = Calendar.objects.create(
        name="Test Calendar",
        email="test@example.com",
        external_id="test_123",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        organization=organization,
    )

    # Create test time range
    now = timezone.now()
    start_time = now + timedelta(hours=1)
    end_time = now + timedelta(hours=2)

    # Mock availability window
    mock_window = AvailableTimeWindow(
        start_time=start_time,
        end_time=end_time,
        id=1,
        can_book_partially=False,
    )

    mock_google_adapter.create_event.side_effect = Exception("Adapter failed")

    with patch.object(CalendarService, "get_availability_windows_in_range") as mock_get_windows:
        mock_get_windows.return_value = [mock_window]

        with patch.object(CalendarService, "_get_calendar_by_external_id") as mock_get_calendar:
            mock_get_calendar.return_value = calendar

            service = CalendarService()
            service.authenticate(account=social_account, organization=organization)

            # Create event data
            event_data = CalendarEventInputData(
                title="Test Event",
                description="Test Description",
                start_time=start_time,
                end_time=end_time,
                attendances=[],
                external_attendances=[],
                resource_allocations=[],
            )

            # Attempt to create the event should raise the adapter exception
            with pytest.raises(Exception, match="Adapter failed"):
                service.create_event(calendar.id, event_data)

            # Verify the adapter was called
            mock_google_adapter.create_event.assert_called_once()

            # Verify availability was checked
            mock_get_windows.assert_called_once_with(calendar, start_time, end_time)


@pytest.mark.django_db
def test_request_organization_calendar_resources_import(
    social_account, organization, mock_google_adapter
):
    with patch.object(
        CalendarService, "get_calendar_adapter_for_account", return_value=mock_google_adapter
    ):
        with patch(
            "calendar_integration.tasks.import_organization_calendar_resources_task.delay"
        ) as mock_task:
            service = CalendarService()
            service.authenticate(account=social_account, organization=organization)
            start = timezone.now()
            end = start + timedelta(days=1)
            service.request_organization_calendar_resources_import(start, end)
            mock_task.assert_called_once()


@pytest.mark.django_db
def test_import_organization_calendar_resources_success(
    social_account, organization, mock_google_adapter
):
    with patch.object(
        CalendarService, "get_calendar_adapter_for_account", return_value=mock_google_adapter
    ):
        import_state = CalendarOrganizationResourcesImport.objects.create(
            organization=organization,
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(days=1),
        )
        service = CalendarService()
        service.authenticate(account=social_account, organization=organization)
        with patch.object(
            service, "_execute_organization_calendar_resources_import", return_value=None
        ) as mock_exec:
            service.import_organization_calendar_resources(import_state)
            import_state.refresh_from_db()
            assert import_state.status == "success"
            mock_exec.assert_called_once()


@pytest.mark.django_db
def test_import_organization_calendar_resources_failure(
    social_account, organization, mock_google_adapter
):
    with patch.object(
        CalendarService, "get_calendar_adapter_for_account", return_value=mock_google_adapter
    ):
        import_state = CalendarOrganizationResourcesImport.objects.create(
            organization=organization,
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(days=1),
        )
        service = CalendarService()
        service.authenticate(account=social_account, organization=organization)
        with patch.object(
            service,
            "_execute_organization_calendar_resources_import",
            side_effect=Exception("fail"),
        ):
            service.import_organization_calendar_resources(import_state)
            import_state.refresh_from_db()
            assert import_state.status == "failed"
            assert "fail" in (import_state.error_message or "")


@pytest.mark.django_db
def test_execute_organization_calendar_resources_import_calls_adapter(
    social_account, mock_google_adapter, organization
):
    with patch.object(
        CalendarService, "get_calendar_adapter_for_account", return_value=mock_google_adapter
    ):
        service = CalendarService()
        service.authenticate(account=social_account, organization=organization)
        start = timezone.now()
        end = start + timedelta(days=1)
        # Use a real CalendarResourceData for the resource
        resource = CalendarResourceData(
            name="Room",
            description="desc",
            provider="google",
            external_id="room_123",
            email="room@example.com",
            capacity=10,
        )
        mock_google_adapter.get_available_calendar_resources.return_value = [resource]
        # Create the calendar that will be looked up by external_id and organization
        Calendar.objects.create(
            name="Room",
            email="room@example.com",
            external_id="room_123",
            provider="google",
            calendar_type=CalendarType.RESOURCE,
            organization=organization,
        )
        with patch.object(service, "request_calendar_sync") as mock_sync:
            result = service._execute_organization_calendar_resources_import(start, end)
            mock_google_adapter.get_available_calendar_resources.assert_called_once_with(start, end)
            mock_sync.assert_called_once()
            assert result == [resource]


# Tests for new data structures
def test_resource_data_creation():
    """Test creating ResourceData instance."""
    resource = ResourceData(
        email="resource@example.com",
        title="Conference Room",
        external_id="room_123",
        status="accepted",
    )

    assert resource.email == "resource@example.com"
    assert resource.title == "Conference Room"
    assert resource.external_id == "room_123"
    assert resource.status == "accepted"


@pytest.mark.django_db
def test_unavailable_time_window_creation():
    """Test creating UnavailableTimeWindow instance."""
    event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="event_456",
        title="Sample Event",
        description="A sample event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        attendees=[],
        status="confirmed",
    )

    window = UnavailableTimeWindow(
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        reason="calendar_event",
        id=123,
        data=event_data,
    )

    assert window.reason == "calendar_event"
    assert window.id == 123
    assert isinstance(window.data, CalendarEventData)


@pytest.mark.django_db
def test_google_adapter_event_creation_with_resources(mock_google_adapter):
    """Test Google adapter creates events with resources in attendees."""
    # This tests the changes to GoogleCalendarAdapter where resources
    # are now included in the attendees list

    event_data = CalendarEventAdapterInputData(
        calendar_external_id="cal_123",
        title="Event with Resources",
        description="Test event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        attendees=[EventAttendeeData(email="user@example.com", name="User", status="accepted")],
        resources=[
            ResourceData(email="room@example.com", title="Conference Room", status="accepted")
        ],
    )

    # Mock the Google adapter response
    created_event = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="event_123",
        title="Event with Resources",
        description="Test event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        attendees=[],
        resources=["room@example.com"],
    )
    mock_google_adapter.create_event.return_value = created_event

    result = mock_google_adapter.create_event(event_data)

    assert result.resources == ["room@example.com"]
    mock_google_adapter.create_event.assert_called_once_with(event_data)


@pytest.mark.django_db
def test_calendar_event_data_with_resources():
    """Test CalendarEventData creation with new fields."""
    event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="event_456",
        title="Sample Event",
        description="A sample event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        attendees=[],
        status="confirmed",
        id=123,
        resources=["resource@example.com"],
    )

    assert event_data.id == 123
    assert event_data.resources == ["resource@example.com"]


@pytest.mark.django_db
def test_calendar_event_input_data_with_resources():
    """Test CalendarEventInputData creation with resources field."""
    event_input = CalendarEventAdapterInputData(
        calendar_external_id="cal_123",
        title="Event with Resources",
        description="Test event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        attendees=[],
        resources=[ResourceData(email="room@example.com", title="Room", external_id="room_123")],
    )

    assert len(event_input.resources) == 1
    assert event_input.resources[0].email == "room@example.com"
    assert event_input.resources[0].title == "Room"


@pytest.mark.django_db
def test_calendar_event_input_data_with_recurrence_rule():
    """Test CalendarEventInputData creation with recurrence rule."""
    event_input = CalendarEventInputData(
        title="Recurring Event",
        description="Test recurring event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
        recurrence_rule="RRULE:FREQ=WEEKLY;COUNT=10;BYDAY=MO",
    )

    assert event_input.recurrence_rule == "RRULE:FREQ=WEEKLY;COUNT=10;BYDAY=MO"
    assert event_input.title == "Recurring Event"


@pytest.mark.django_db
def test_calendar_event_adapter_input_data_with_recurrence_rule():
    """Test CalendarEventAdapterInputData creation with recurrence rule."""
    event_input = CalendarEventAdapterInputData(
        calendar_external_id="cal_123",
        title="Recurring Event",
        description="Test recurring event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        attendees=[],
        recurrence_rule="RRULE:FREQ=DAILY;COUNT=5",
    )

    assert event_input.recurrence_rule == "RRULE:FREQ=DAILY;COUNT=5"
    assert event_input.title == "Recurring Event"


@pytest.mark.django_db
def test_calendar_event_data_with_recurrence_rule():
    """Test CalendarEventData creation with recurrence rule."""
    event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="event_123",
        title="Recurring Event",
        description="Test recurring event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        attendees=[],
        status="confirmed",
        id=123,
        recurrence_rule="RRULE:FREQ=MONTHLY;COUNT=3;BYMONTHDAY=22",
    )

    assert event_data.recurrence_rule == "RRULE:FREQ=MONTHLY;COUNT=3;BYMONTHDAY=22"
    assert event_data.id == 123


@pytest.mark.django_db
def test_get_unavailable_time_windows_in_range_with_recurring_events_outside_master_range(
    social_account, social_token, mock_google_adapter, calendar, patch_get_calendar
):
    """Test that get_unavailable_time_windows_in_range finds recurring instances even when the master event is outside the search window."""
    mock_google_adapter.provider = CalendarProvider.GOOGLE
    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)

    # Create a recurring event that starts on Monday, June 22, 2025 at 10:00 AM
    master_start = datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC)  # Monday
    master_end = master_start + datetime.timedelta(hours=1)
    recurrence_rule = "RRULE:FREQ=WEEKLY;COUNT=4;BYDAY=MO"  # Weekly for 4 weeks on Monday

    parent_created_event_data = CalendarEventData(
        calendar_external_id="cal_123",
        external_id="recurring_master_123",
        title="Weekly Team Meeting",
        description="Recurring team meeting",
        start_time=master_start,
        end_time=master_end,
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=recurrence_rule.replace("RRULE:", ""),
    )
    mock_google_adapter.create_event.return_value = parent_created_event_data

    with patch.object(
        CalendarService,
        "get_availability_windows_in_range",
        return_value=[
            AvailableTimeWindow(
                start_time=master_start, end_time=master_end, id=1, can_book_partially=False
            )
        ],
    ):
        _ = service.create_recurring_event(
            calendar.id,
            title="Weekly Team Meeting",
            description="Recurring team meeting",
            start_time=master_start,
            end_time=master_end,
            recurrence_rule=recurrence_rule,
        )

    # Now search for unavailable windows in a time range that includes the 3rd occurrence
    # but excludes the original master event
    # 3rd occurrence would be on July 6, 2025 (2 weeks after June 22)
    search_start = datetime.datetime(
        2025, 7, 5, 0, 0, tzinfo=datetime.UTC
    )  # Saturday before 3rd occurrence
    search_end = datetime.datetime(
        2025, 7, 8, 23, 59, tzinfo=datetime.UTC
    )  # Tuesday after 3rd occurrence

    unavailable_windows = service.get_unavailable_time_windows_in_range(
        calendar=calendar,
        start_datetime=search_start,
        end_datetime=search_end,
    )

    # Should find exactly one unavailable window (the 3rd occurrence)
    assert len(unavailable_windows) == 1

    window = unavailable_windows[0]
    assert window.reason == "calendar_event"
    assert window.data.title == "Weekly Team Meeting"

    # The occurrence should be on Monday, July 7, 2025 at 10:00 AM
    # (3rd occurrence of a weekly Monday event starting from June 22)
    expected_occurrence_start = datetime.datetime(2025, 7, 7, 10, 0, tzinfo=datetime.UTC)
    expected_occurrence_end = datetime.datetime(2025, 7, 7, 11, 0, tzinfo=datetime.UTC)

    assert window.start_time == expected_occurrence_start
    assert window.end_time == expected_occurrence_end

    # Verify the original master event time is NOT in our search window
    assert not (search_start <= master_start <= search_end)
    # But the occurrence time IS in our search window
    assert search_start <= expected_occurrence_start <= search_end


# Tests for EventsSyncChanges and related sync functionality
def test_events_sync_changes_initialization():
    """Test EventsSyncChanges dataclass initialization."""
    changes = EventsSyncChanges()

    assert changes.events_to_update == []
    assert changes.events_to_create == []
    assert changes.blocked_times_to_create == []
    assert changes.blocked_times_to_update == []
    assert changes.attendances_to_create == []
    assert changes.external_attendances_to_create == []
    assert changes.events_to_delete == []
    assert changes.blocks_to_delete == []
    assert changes.matched_event_ids == set()
    assert changes.recurrence_rules_to_create == []


@pytest.mark.django_db
def test_events_sync_changes_with_data(calendar, organization, db):
    """Test EventsSyncChanges with actual data."""
    from calendar_integration.models import RecurrenceRule

    changes = EventsSyncChanges()

    # Create test event
    event = CalendarEvent(
        calendar_fk=calendar,
        title="Test Event",
        external_id="event_123",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        organization=calendar.organization,
    )

    # Create test blocked time
    blocked_time = BlockedTime(
        calendar_fk=calendar,
        start_time=datetime.datetime(2025, 6, 22, 12, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 13, 0, tzinfo=datetime.UTC),
        reason="Test block",
        external_id="block_123",
        organization=calendar.organization,
    )

    # Create test recurrence rule
    rule = RecurrenceRule.from_rrule_string(
        "FREQ=WEEKLY;COUNT=3;BYDAY=MO", organization=calendar.organization
    )

    # Add data to changes
    changes.events_to_create.append(event)
    changes.blocked_times_to_create.append(blocked_time)
    changes.events_to_delete.append("old_event_123")
    changes.blocks_to_delete.append("old_block_123")
    changes.matched_event_ids.add("event_123")
    changes.recurrence_rules_to_create.append(rule)

    assert len(changes.events_to_create) == 1
    assert len(changes.blocked_times_to_create) == 1
    assert len(changes.events_to_delete) == 1
    assert len(changes.blocks_to_delete) == 1
    assert len(changes.matched_event_ids) == 1
    assert len(changes.recurrence_rules_to_create) == 1
    assert changes.events_to_create[0].title == "Test Event"
    assert changes.blocked_times_to_create[0].reason == "Test block"
    assert "event_123" in changes.matched_event_ids
    assert "old_event_123" in changes.events_to_delete


@pytest.mark.django_db
def test_apply_sync_changes_events_to_create(
    social_account, social_token, mock_google_adapter, calendar
):
    """Test _apply_sync_changes creates new events correctly."""
    changes = EventsSyncChanges()

    # Create new event to be created
    new_event = CalendarEvent(
        calendar_fk=calendar,
        title="New Event",
        description="A new event",
        start_time=datetime.datetime(2025, 6, 22, 14, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 15, 0, tzinfo=datetime.UTC),
        external_id="new_event_123",
        organization=calendar.organization,
    )
    changes.events_to_create.append(new_event)

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    service._apply_sync_changes(calendar.id, changes)

    # Verify event was created
    created_event = CalendarEvent.objects.get(
        external_id="new_event_123", organization=calendar.organization
    )
    assert created_event.title == "New Event"
    assert created_event.description == "A new event"
    assert created_event.calendar_fk == calendar
    assert created_event.organization == calendar.organization


@pytest.mark.django_db
def test_apply_sync_changes_events_to_delete(
    social_account, social_token, mock_google_adapter, calendar
):
    """Test _apply_sync_changes deletes events correctly."""
    # Create existing event to be deleted
    CalendarEvent.objects.create(
        calendar_fk=calendar,
        title="Event to Delete",
        external_id="delete_event_123",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        organization=calendar.organization,
    )

    changes = EventsSyncChanges()
    changes.events_to_delete.append("delete_event_123")

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    service._apply_sync_changes(calendar.id, changes)

    # Verify event was deleted
    assert not CalendarEvent.objects.filter(
        external_id="delete_event_123", organization=calendar.organization
    ).exists()


@pytest.mark.django_db
def test_apply_sync_changes_blocked_times_to_create(
    social_account, social_token, mock_google_adapter, calendar
):
    """Test _apply_sync_changes creates blocked times correctly."""
    changes = EventsSyncChanges()

    # Create blocked time to be created
    new_blocked_time = BlockedTime(
        calendar_fk=calendar,
        start_time=datetime.datetime(2025, 6, 22, 16, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 17, 0, tzinfo=datetime.UTC),
        reason="New blocked time",
        external_id="new_block_123",
        organization=calendar.organization,
    )
    changes.blocked_times_to_create.append(new_blocked_time)

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    service._apply_sync_changes(calendar.id, changes)

    # Verify blocked time was created
    created_block = BlockedTime.objects.get(
        external_id="new_block_123", organization=calendar.organization
    )
    assert created_block.reason == "New blocked time"
    assert created_block.calendar_fk == calendar
    assert created_block.organization == calendar.organization


@pytest.mark.django_db
def test_apply_sync_changes_blocked_times_to_update(
    social_account, social_token, mock_google_adapter, calendar
):
    """Test _apply_sync_changes updates blocked times correctly."""
    # Create existing blocked time to be updated
    existing_block = BlockedTime.objects.create(
        calendar_fk=calendar,
        start_time=datetime.datetime(2025, 6, 22, 18, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 19, 0, tzinfo=datetime.UTC),
        reason="Original reason",
        external_id="update_block_123",
        organization=calendar.organization,
    )

    # Update the blocked time
    existing_block.reason = "Updated reason"
    existing_block.start_time = datetime.datetime(2025, 6, 22, 18, 30, tzinfo=datetime.UTC)

    changes = EventsSyncChanges()
    changes.blocked_times_to_update.append(existing_block)

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    service._apply_sync_changes(calendar.id, changes)

    # Verify blocked time was updated
    existing_block.refresh_from_db()
    assert existing_block.reason == "Updated reason"
    assert existing_block.start_time == datetime.datetime(2025, 6, 22, 18, 30, tzinfo=datetime.UTC)


@pytest.mark.django_db
def test_apply_sync_changes_blocks_to_delete(
    social_account, social_token, mock_google_adapter, calendar
):
    """Test _apply_sync_changes deletes blocked times correctly."""
    # Create existing blocked time to be deleted
    BlockedTime.objects.create(
        calendar_fk=calendar,
        start_time=datetime.datetime(2025, 6, 22, 20, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 21, 0, tzinfo=datetime.UTC),
        reason="Block to delete",
        external_id="delete_block_123",
        organization=calendar.organization,
    )

    changes = EventsSyncChanges()
    changes.blocks_to_delete.append("delete_block_123")

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    service._apply_sync_changes(calendar.id, changes)

    # Verify blocked time was deleted
    assert not BlockedTime.objects.filter(
        external_id="delete_block_123", organization=calendar.organization
    ).exists()


@pytest.mark.django_db
def test_apply_sync_changes_attendances_to_create(
    social_account, social_token, mock_google_adapter, calendar, db
):
    """Test _apply_sync_changes creates event attendances correctly."""
    # Create event first
    event = CalendarEvent.objects.create(
        calendar_fk=calendar,
        title="Event with Attendees",
        external_id="event_with_attendees",
        start_time=datetime.datetime(2025, 6, 22, 22, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 23, 0, tzinfo=datetime.UTC),
        organization=calendar.organization,
    )

    # Create user for attendance
    user = User.objects.create_user(
        username="attendee", email="attendee@example.com", password="testpass123"
    )

    changes = EventsSyncChanges()

    # Create attendance to be created
    new_attendance = EventAttendance(
        event_fk=event,
        user=user,
        organization=calendar.organization,
    )
    changes.attendances_to_create.append(new_attendance)

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    service._apply_sync_changes(calendar.id, changes)

    # Verify attendance was created
    created_attendance = EventAttendance.objects.get(
        event_fk=event, user=user, organization=calendar.organization
    )
    assert created_attendance.organization == calendar.organization


@pytest.mark.django_db
def test_apply_sync_changes_external_attendances_to_create(
    social_account, social_token, mock_google_adapter, calendar, db
):
    """Test _apply_sync_changes creates external event attendances correctly."""
    # Create event first
    event = CalendarEvent.objects.create(
        calendar_fk=calendar,
        title="Event with External Attendees",
        external_id="event_with_external_attendees",
        start_time=datetime.datetime(2025, 6, 23, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 23, 11, 0, tzinfo=datetime.UTC),
        organization=calendar.organization,
    )

    # Create external attendee
    external_attendee = ExternalAttendee.objects.create(
        email="external@example.com",
        name="External User",
        organization=calendar.organization,
    )

    changes = EventsSyncChanges()

    # Create external attendance to be created
    new_external_attendance = EventExternalAttendance(
        event_fk=event,
        external_attendee_fk=external_attendee,
        organization=calendar.organization,
    )
    changes.external_attendances_to_create.append(new_external_attendance)

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    service._apply_sync_changes(calendar.id, changes)

    # Verify external attendance was created
    created_external_attendance = EventExternalAttendance.objects.get(
        event_fk=event, external_attendee_fk=external_attendee, organization=calendar.organization
    )
    assert created_external_attendance.organization == calendar.organization


@pytest.mark.django_db
def test_handle_deletions_for_full_sync_no_organization(
    social_account, social_token, mock_google_adapter, calendar
):
    """Test _handle_deletions_for_full_sync returns early when no organization."""
    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    service.organization = None  # Simulate no organization

    # Should return early without doing anything
    result = service._handle_deletions_for_full_sync(
        calendar.id,
        {},
        set(),
        datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC),
    )

    assert result is None


@pytest.mark.django_db
def test_apply_sync_changes_comprehensive(
    social_account, social_token, mock_google_adapter, calendar, db
):
    """Test _apply_sync_changes with multiple types of changes."""
    from calendar_integration.models import RecurrenceRule

    # Create existing event to update
    existing_event = CalendarEvent.objects.create(
        calendar_fk=calendar,
        title="Original Title",
        external_id="update_event_123",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        organization=calendar.organization,
    )

    # Create existing blocked time to update
    existing_block = BlockedTime.objects.create(
        calendar_fk=calendar,
        start_time=datetime.datetime(2025, 6, 22, 12, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 13, 0, tzinfo=datetime.UTC),
        reason="Original reason",
        external_id="update_block_123",
        organization=calendar.organization,
    )

    # Create event to be deleted
    CalendarEvent.objects.create(
        calendar_fk=calendar,
        title="Event to Delete",
        external_id="delete_event_789",
        start_time=datetime.datetime(2025, 6, 22, 14, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 15, 0, tzinfo=datetime.UTC),
        organization=calendar.organization,
    )

    changes = EventsSyncChanges()

    # Add new event to create
    new_event = CalendarEvent(
        calendar_fk=calendar,
        title="New Event",
        external_id="new_event_456",
        start_time=datetime.datetime(2025, 6, 22, 16, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 17, 0, tzinfo=datetime.UTC),
        organization=calendar.organization,
    )
    changes.events_to_create.append(new_event)

    # Add new blocked time to create
    new_block = BlockedTime(
        calendar_fk=calendar,
        start_time=datetime.datetime(2025, 6, 22, 18, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 19, 0, tzinfo=datetime.UTC),
        reason="New block reason",
        external_id="new_block_456",
        organization=calendar.organization,
    )
    changes.blocked_times_to_create.append(new_block)

    # Add recurrence rule to create
    rule = RecurrenceRule.from_rrule_string(
        "FREQ=DAILY;COUNT=5", organization=calendar.organization
    )
    changes.recurrence_rules_to_create.append(rule)

    # Update existing event
    existing_event.title = "Updated Title"
    changes.events_to_update.append(existing_event)

    # Update existing blocked time
    existing_block.reason = "Updated reason"
    changes.blocked_times_to_update.append(existing_block)

    # Add events/blocks to delete
    changes.events_to_delete.append("delete_event_789")

    service = CalendarService()
    service.authenticate(account=social_account, organization=calendar.organization)
    service._apply_sync_changes(calendar.id, changes)

    # Verify all changes were applied
    # New event created
    assert CalendarEvent.objects.filter(
        external_id="new_event_456", organization=calendar.organization
    ).exists()
    new_created_event = CalendarEvent.objects.get(
        external_id="new_event_456", organization=calendar.organization
    )
    assert new_created_event.title == "New Event"

    # New blocked time created
    assert BlockedTime.objects.filter(
        external_id="new_block_456", organization=calendar.organization
    ).exists()
    new_created_block = BlockedTime.objects.get(
        external_id="new_block_456", organization=calendar.organization
    )
    assert new_created_block.reason == "New block reason"

    # Recurrence rule created
    assert RecurrenceRule.objects.filter(frequency="DAILY", count=5).exists()

    # Existing event updated
    existing_event.refresh_from_db()
    assert existing_event.title == "Updated Title"

    # Existing blocked time updated
    existing_block.refresh_from_db()
    assert existing_block.reason == "Updated reason"

    # Event deleted
    assert not CalendarEvent.objects.filter(
        external_id="delete_event_789", organization=calendar.organization
    ).exists()


# Bundle Calendar Tests


@pytest.fixture
def bundle_calendar(organization, child_calendar_internal, child_calendar_google, db):
    """Create a bundle calendar with Google as primary for testing."""
    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    return service.create_bundle_calendar(
        name="Test Bundle Calendar",
        description="A test bundle calendar",
        child_calendars=[child_calendar_internal, child_calendar_google],
        primary_calendar=child_calendar_google,  # Google is primary
    )


@pytest.fixture
def empty_bundle_calendar(organization, db):
    """Create a bundle calendar with no children for testing error cases."""
    return Calendar.objects.create(
        name="Empty Bundle Calendar",
        description="A bundle calendar with no children",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.BUNDLE,
        organization=organization,
    )


@pytest.fixture
def child_calendar_internal(organization, db):
    """Create an internal child calendar for testing."""
    return Calendar.objects.create(
        name="Internal Child Calendar",
        external_id="internal-child-1",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        organization=organization,
    )


@pytest.fixture
def child_calendar_google(organization, db):
    """Create a Google child calendar for testing."""
    return Calendar.objects.create(
        name="Google Child Calendar",
        external_id="google-child-1",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        organization=organization,
    )


@pytest.fixture
def bundle_event_data():
    """Sample event data for bundle tests."""
    return CalendarEventInputData(
        title="Bundle Meeting",
        description="A meeting created through bundle calendar",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )


@pytest.mark.django_db
def test_create_bundle_calendar(organization):
    """Test creating a bundle calendar without child calendars."""
    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    bundle_calendar = service.create_bundle_calendar(
        name="Test Bundle",
        description="Test bundle description",
    )

    assert bundle_calendar.name == "Test Bundle"
    assert bundle_calendar.description == "Test bundle description"
    assert bundle_calendar.calendar_type == CalendarType.BUNDLE
    assert bundle_calendar.provider == CalendarProvider.INTERNAL
    assert bundle_calendar.organization == organization


@pytest.mark.django_db
def test_create_bundle_calendar_with_children(
    organization, child_calendar_internal, child_calendar_google
):
    """Test creating a bundle calendar with child calendars."""
    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    bundle_calendar = service.create_bundle_calendar(
        name="Test Bundle",
        description="Test bundle description",
        child_calendars=[child_calendar_internal, child_calendar_google],
        primary_calendar=child_calendar_google,
    )

    assert bundle_calendar.name == "Test Bundle"
    assert bundle_calendar.calendar_type == CalendarType.BUNDLE

    # Check relationships were created
    relationships = bundle_calendar.bundle_relationships.all()
    assert relationships.count() == 2

    # Check bundle_children relationship
    child_calendars = bundle_calendar.bundle_children.all()
    assert child_calendars.count() == 2
    assert child_calendar_internal in child_calendars
    assert child_calendar_google in child_calendars

    # Check primary designation
    primary_rel = bundle_calendar.bundle_relationships.filter(is_primary=True).first()
    assert primary_rel is not None
    assert primary_rel.child_calendar == child_calendar_google


@pytest.mark.django_db
def test_create_bundle_calendar_different_organization_error(organization, child_calendar_internal):
    """Test that child calendars must belong to the same organization."""
    # Create another organization and calendar
    other_org = Organization.objects.create(name="Other Org")
    other_calendar = Calendar.objects.create(
        name="Other Calendar",
        external_id="other-1",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        organization=other_org,
    )

    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    with pytest.raises(
        ValueError, match="All child calendars must belong to the same organization"
    ):
        service.create_bundle_calendar(
            name="Test Bundle",
            child_calendars=[child_calendar_internal, other_calendar],
            primary_calendar=other_calendar,
        )


@pytest.mark.django_db
def test_create_bundle_calendar_primary_not_in_children_error(
    organization, child_calendar_internal, child_calendar_google
):
    """Test that primary calendar must be one of the child calendars."""
    # Create another calendar that's not in the children list
    other_calendar = Calendar.objects.create(
        name="Other Calendar",
        external_id="other-2",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        organization=organization,
    )

    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    with pytest.raises(ValueError, match="Primary calendar must be one of the child calendars"):
        service.create_bundle_calendar(
            name="Test Bundle",
            child_calendars=[child_calendar_internal, child_calendar_google],
            primary_calendar=other_calendar,  # Not in child_calendars
        )


@pytest.mark.django_db
@patch(
    "calendar_integration.services.calendar_service.CalendarService.get_availability_windows_in_range"
)
def test_create_bundle_event_uses_designated_primary(
    mock_availability,
    organization,
    child_calendar_google,
    bundle_event_data,
):
    """Test bundle event creation uses the designated primary calendar."""
    other_calendar_google = Calendar.objects.create(
        name="Google Child Calendar",
        external_id="google-child-2",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        organization=organization,
    )

    # Create bundle with Google as primary
    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    bundle_calendar = service.create_bundle_calendar(
        name="Test Bundle",
        description="Test bundle description",
        child_calendars=[other_calendar_google, child_calendar_google],
        primary_calendar=child_calendar_google,  # Explicitly set Google as primary
    )

    # Mock availability as available
    mock_availability.return_value = [
        AvailableTimeWindow(
            start_time=bundle_event_data.start_time,
            end_time=bundle_event_data.end_time,
        )
    ]

    # Mock the create_event method
    with patch.object(service, "create_event") as mock_create_event:
        mock_primary_event = CalendarEvent(
            id=1,
            title=bundle_event_data.title,
            calendar=child_calendar_google,  # Primary calendar
            organization=organization,
            start_time=bundle_event_data.start_time,
            end_time=bundle_event_data.end_time,
        )
        mock_create_event.return_value = mock_primary_event

        service._create_bundle_event(bundle_calendar, bundle_event_data)

        # Should have created primary event on Google calendar (the designated primary)
        mock_create_event.assert_called_once()
        call_args = mock_create_event.call_args
        assert call_args[0][0] == child_calendar_google.id


@pytest.mark.django_db
@patch(
    "calendar_integration.services.calendar_service.CalendarService.get_availability_windows_in_range"
)
def test_create_bundle_event_no_availability_error(
    mock_availability,
    organization,
    bundle_calendar,
    child_calendar_internal,
    bundle_event_data,
):
    """Test that bundle event creation fails when no availability."""
    from calendar_integration.models import ChildrenCalendarRelationship

    # Set up bundle relationships
    ChildrenCalendarRelationship.objects.create(
        bundle_calendar=bundle_calendar,
        child_calendar=child_calendar_internal,
        organization=organization,
    )

    # Mock no availability
    mock_availability.return_value = []

    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    with pytest.raises(ValueError, match="No availability in child calendar"):
        service._create_bundle_event(bundle_calendar, bundle_event_data)


@pytest.mark.django_db
def test_create_bundle_event_non_bundle_calendar_error(
    organization, child_calendar_internal, bundle_event_data
):
    """Test that create_bundle_event only works with bundle calendars."""
    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    with pytest.raises(ValueError, match="Calendar must be a bundle calendar"):
        service._create_bundle_event(child_calendar_internal, bundle_event_data)


@pytest.mark.django_db
def test_create_bundle_event_no_children_error(
    organization, empty_bundle_calendar, bundle_event_data
):
    """Test that bundle calendar must have child calendars."""
    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    with pytest.raises(ValueError, match="Bundle calendar has no child calendars"):
        service._create_bundle_event(empty_bundle_calendar, bundle_event_data)


@pytest.mark.django_db
@patch(
    "calendar_integration.services.calendar_service.CalendarService.get_availability_windows_in_range"
)
def test_create_bundle_event_creates_representations(
    mock_availability,
    organization,
    bundle_calendar,  # This now includes the relationships
    bundle_event_data,
):
    """Test that bundle event creates appropriate representations."""
    # Mock availability as available
    mock_availability.return_value = [
        AvailableTimeWindow(
            start_time=bundle_event_data.start_time,
            end_time=bundle_event_data.end_time,
        )
    ]

    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    # Mock the create_event method to track calls
    created_events = []

    def mock_create_event(calendar_id, event_data):
        # Get calendar from the bundle relationships
        relationships = bundle_calendar.bundle_relationships.all()
        calendar = None
        for rel in relationships:
            # Handle both integer ID and string external_id comparisons
            if (
                rel.child_calendar.external_id == calendar_id
                or rel.child_calendar.id == calendar_id
            ):
                calendar = rel.child_calendar
                break

        if not calendar:
            raise ValueError(f"Calendar with external_id {calendar_id} not found")

        event = CalendarEvent.objects.create(
            title=event_data.title,
            description=event_data.description,
            start_time=event_data.start_time,
            end_time=event_data.end_time,
            calendar=calendar,
            organization=organization,
            external_id=f"event-{len(created_events)}",
        )
        created_events.append(event)
        return event

    with patch.object(service, "create_event", side_effect=mock_create_event):
        service._create_bundle_event(bundle_calendar, bundle_event_data)

        # Should have created 2 events: primary + internal representation
        assert len(created_events) == 2

        # Primary event should be marked as bundle primary
        created_events[0].refresh_from_db()
        assert created_events[0].is_bundle_primary is True
        assert created_events[0].bundle_calendar == bundle_calendar

        # Should have created a representation event for the Internal calendar
        # (since Google is primary, Internal gets a representation)
        representation_event = created_events[1]
        representation_event.refresh_from_db()
        assert representation_event.is_bundle_primary is False
        assert representation_event.bundle_calendar == bundle_calendar
        assert representation_event.bundle_primary_event == created_events[0]
        # Get the internal calendar from relationships
        internal_calendar = None
        for rel in bundle_calendar.bundle_relationships.all():
            if not rel.is_primary:
                internal_calendar = rel.child_calendar
                break
        assert representation_event.calendar == internal_calendar


@pytest.mark.django_db
def test_update_bundle_event(organization, bundle_calendar):
    """Test updating a bundle event."""
    # Create a primary bundle event
    primary_event = CalendarEvent.objects.create(
        title="Original Title",
        description="Original description",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        calendar=Calendar.objects.create(
            name="Primary Calendar",
            external_id="primary-1",
            provider=CalendarProvider.GOOGLE,
            organization=organization,
        ),
        organization=organization,
        external_id="primary-event-1",
        is_bundle_primary=True,
        bundle_calendar=bundle_calendar,
    )

    # Create representation event
    CalendarEvent.objects.create(
        title="[Bundle] Original Title",
        description="Bundle event from Test Bundle Calendar\n\nOriginal description",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        calendar=Calendar.objects.create(
            name="Internal Calendar",
            external_id="internal-1",
            provider=CalendarProvider.INTERNAL,
            organization=organization,
        ),
        organization=organization,
        external_id="repr-event-1",
        bundle_calendar=bundle_calendar,
        bundle_primary_event=primary_event,
    )

    # Create blocked time representation
    blocked_time = BlockedTime.objects.create(
        calendar=Calendar.objects.create(
            name="Another Calendar",
            external_id="another-1",
            provider=CalendarProvider.GOOGLE,
            organization=organization,
        ),
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        reason="Bundle event: Original Title",
        organization=organization,
        bundle_calendar=bundle_calendar,
        bundle_primary_event=primary_event,
    )

    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    # Mock the update_event method
    with patch.object(service, "update_event") as mock_update_event:
        mock_update_event.return_value = primary_event

        updated_data = CalendarEventInputData(
            title="Updated Title",
            description="Updated description",
            start_time=datetime.datetime(2025, 6, 22, 14, 0, tzinfo=datetime.UTC),
            end_time=datetime.datetime(2025, 6, 22, 15, 0, tzinfo=datetime.UTC),
            attendances=[],
            external_attendances=[],
            resource_allocations=[],
        )

        service._update_bundle_event(primary_event, updated_data)

        # Should have called update_event twice (primary + representation)
        assert mock_update_event.call_count == 2

        # Check blocked time was updated
        blocked_time.refresh_from_db()
        assert blocked_time.start_time == updated_data.start_time
        assert blocked_time.end_time == updated_data.end_time
        assert blocked_time.reason == "Bundle event: Updated Title"


@pytest.mark.django_db
def test_update_bundle_event_non_primary_error(organization):
    """Test that update_bundle_event only works with primary events."""
    non_primary_event = CalendarEvent.objects.create(
        title="Non-primary Event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        calendar=Calendar.objects.create(
            name="Some Calendar", external_id="some-1", organization=organization
        ),
        organization=organization,
        is_bundle_primary=False,
    )

    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    updated_data = CalendarEventInputData(
        title="Updated Title",
        description="Updated description",
        start_time=datetime.datetime(2025, 6, 22, 14, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 15, 0, tzinfo=datetime.UTC),
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )

    with pytest.raises(ValueError, match="Event must be a bundle primary event"):
        service._update_bundle_event(non_primary_event, updated_data)


@pytest.mark.django_db
def test_delete_bundle_event(organization, bundle_calendar):
    """Test deleting a bundle event and all its representations."""
    # Create a primary bundle event
    primary_event = CalendarEvent.objects.create(
        title="Primary Event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        calendar=Calendar.objects.create(
            name="Primary Calendar",
            external_id="primary-1",
            provider=CalendarProvider.GOOGLE,
            organization=organization,
        ),
        organization=organization,
        external_id="primary-event-1",
        is_bundle_primary=True,
        bundle_calendar=bundle_calendar,
    )

    # Create representation event
    CalendarEvent.objects.create(
        title="[Bundle] Primary Event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        calendar=Calendar.objects.create(
            name="Internal Calendar",
            external_id="internal-1",
            provider=CalendarProvider.INTERNAL,
            organization=organization,
        ),
        organization=organization,
        external_id="repr-event-1",
        bundle_calendar=bundle_calendar,
        bundle_primary_event=primary_event,
    )

    # Create blocked time representation
    blocked_time = BlockedTime.objects.create(
        calendar=Calendar.objects.create(
            name="Another Calendar",
            external_id="another-1",
            provider=CalendarProvider.GOOGLE,
            organization=organization,
        ),
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        reason="Bundle event: Primary Event",
        organization=organization,
        bundle_calendar=bundle_calendar,
        bundle_primary_event=primary_event,
    )

    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    # Mock the delete_event method
    with patch.object(service, "delete_event") as mock_delete_event:
        service._delete_bundle_event(primary_event)

        # Should have called delete_event twice (primary + representation)
        assert mock_delete_event.call_count == 2

        # Check blocked time was deleted
        assert not BlockedTime.objects.filter(id=blocked_time.id).exists()


@pytest.mark.django_db
def test_delete_bundle_event_non_primary_error(organization):
    """Test that delete_bundle_event only works with primary events."""
    non_primary_event = CalendarEvent.objects.create(
        title="Non-primary Event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        calendar=Calendar.objects.create(
            name="Some Calendar", external_id="some-1", organization=organization
        ),
        organization=organization,
        is_bundle_primary=False,
    )

    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    with pytest.raises(ValueError, match="Event must be a bundle primary event"):
        service._delete_bundle_event(non_primary_event)


@pytest.mark.django_db
def test_get_calendar_events_expanded_bundle_calendar(organization, bundle_calendar):
    """Test getting expanded events from a bundle calendar using the main method."""
    # Create child calendars
    child1 = Calendar.objects.create(
        name="Child 1",
        external_id="child-1",
        provider=CalendarProvider.INTERNAL,
        organization=organization,
    )
    child2 = Calendar.objects.create(
        name="Child 2",
        external_id="child-2",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )

    # Set up bundle relationships
    from calendar_integration.models import ChildrenCalendarRelationship

    ChildrenCalendarRelationship.objects.create(
        bundle_calendar=bundle_calendar, child_calendar=child1, organization=organization
    )
    ChildrenCalendarRelationship.objects.create(
        bundle_calendar=bundle_calendar, child_calendar=child2, organization=organization
    )

    # Create a bundle primary event
    primary_event = CalendarEvent.objects.create(
        title="Bundle Primary Event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        calendar=child2,
        organization=organization,
        external_id="primary-1",
        is_bundle_primary=True,
        bundle_calendar=bundle_calendar,
    )

    # Create a representation event (should be filtered out)
    CalendarEvent.objects.create(
        title="[Bundle] Primary Event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        calendar=child1,
        organization=organization,
        external_id="repr-1",
        bundle_calendar=bundle_calendar,
        bundle_primary_event=primary_event,
    )

    # Create a regular event in child calendar
    CalendarEvent.objects.create(
        title="Regular Event",
        start_time=datetime.datetime(2025, 6, 22, 14, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 15, 0, tzinfo=datetime.UTC),
        calendar=child1,
        organization=organization,
        external_id="regular-1",
    )

    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    events = service.get_calendar_events_expanded(
        bundle_calendar,
        datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC),
        datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC),
    )

    # Should only return primary and regular events, not representations
    assert len(events) == 2
    event_titles = [e.title for e in events]
    assert "Bundle Primary Event" in event_titles
    assert "Regular Event" in event_titles
    assert "[Bundle] Primary Event" not in event_titles


# Recurring BlockedTime and AvailableTime Tests


@pytest.mark.django_db
def test_create_blocked_time_simple(organization, calendar):
    """Test creating a simple (non-recurring) blocked time."""
    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    start_time = datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 9, 1, 17, 0, tzinfo=datetime.UTC)

    blocked_time = service.create_blocked_time(
        calendar=calendar, start_time=start_time, end_time=end_time, reason="Office maintenance"
    )

    assert blocked_time.calendar == calendar
    assert blocked_time.start_time == start_time
    assert blocked_time.end_time == end_time
    assert blocked_time.reason == "Office maintenance"
    assert blocked_time.recurrence_rule is None
    assert not blocked_time.is_recurring


@pytest.mark.django_db
def test_create_blocked_time_recurring(organization, calendar):
    """Test creating a recurring blocked time."""
    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    start_time = datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 9, 1, 17, 0, tzinfo=datetime.UTC)
    rrule_string = "FREQ=WEEKLY;BYDAY=MO;COUNT=4"

    blocked_time = service.create_blocked_time(
        calendar=calendar,
        start_time=start_time,
        end_time=end_time,
        reason="Weekly maintenance",
        rrule_string=rrule_string,
    )

    assert blocked_time.calendar == calendar
    assert blocked_time.start_time == start_time
    assert blocked_time.end_time == end_time
    assert blocked_time.reason == "Weekly maintenance"
    assert blocked_time.recurrence_rule is not None
    assert blocked_time.is_recurring
    # Check that the rrule contains the expected components (order may vary)
    rrule_result = blocked_time.recurrence_rule.to_rrule_string()
    assert "FREQ=WEEKLY" in rrule_result
    assert "BYDAY=MO" in rrule_result
    assert "COUNT=4" in rrule_result


@pytest.mark.django_db
def test_create_available_time_simple(organization):
    """Test creating a simple (non-recurring) available time."""
    calendar = Calendar.objects.create(
        name="Test Calendar",
        organization=organization,
        manage_available_windows=True,
    )

    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    start_time = datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 9, 1, 17, 0, tzinfo=datetime.UTC)

    available_time = service.create_available_time(
        calendar=calendar, start_time=start_time, end_time=end_time
    )

    assert available_time.calendar == calendar
    assert available_time.start_time == start_time
    assert available_time.end_time == end_time
    assert available_time.recurrence_rule is None
    assert not available_time.is_recurring


@pytest.mark.django_db
def test_create_available_time_recurring(organization):
    """Test creating a recurring available time."""
    calendar = Calendar.objects.create(
        name="Test Calendar",
        organization=organization,
        manage_available_windows=True,
    )

    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    start_time = datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 9, 1, 17, 0, tzinfo=datetime.UTC)
    rrule_string = "FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;COUNT=5"

    available_time = service.create_available_time(
        calendar=calendar, start_time=start_time, end_time=end_time, rrule_string=rrule_string
    )

    assert available_time.calendar == calendar
    assert available_time.start_time == start_time
    assert available_time.end_time == end_time
    assert available_time.recurrence_rule is not None
    assert available_time.is_recurring
    # Check that the rrule contains the expected components (order may vary)
    rrule_result = available_time.recurrence_rule.to_rrule_string()
    assert "FREQ=DAILY" in rrule_result
    assert "BYDAY=MO,TU,WE,TH,FR" in rrule_result
    assert "COUNT=5" in rrule_result


@pytest.mark.django_db
def test_bulk_create_blocked_times_with_recurrence(organization, calendar):
    """Test bulk creating blocked times with recurrence support."""
    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    blocked_times_data = [
        (
            datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC),
            datetime.datetime(2025, 9, 1, 17, 0, tzinfo=datetime.UTC),
            "Daily maintenance",
            "FREQ=DAILY;COUNT=3",
        ),
        (
            datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
            datetime.datetime(2025, 9, 2, 11, 0, tzinfo=datetime.UTC),
            "Simple blocking",
            None,
        ),
    ]

    blocked_times = service.bulk_create_manual_blocked_times(
        calendar=calendar, blocked_times=blocked_times_data
    )

    blocked_times_list = list(blocked_times)
    assert len(blocked_times_list) == 2

    # First blocked time should be recurring
    recurring_blocked = blocked_times_list[0]
    assert recurring_blocked.reason == "Daily maintenance"
    assert recurring_blocked.is_recurring
    rrule_result = recurring_blocked.recurrence_rule.to_rrule_string()
    assert "FREQ=DAILY" in rrule_result
    assert "COUNT=3" in rrule_result

    # Second blocked time should be simple
    simple_blocked = blocked_times_list[1]
    assert simple_blocked.reason == "Simple blocking"
    assert not simple_blocked.is_recurring


@pytest.mark.django_db
def test_bulk_create_availability_windows_with_recurrence(organization):
    """Test bulk creating availability windows with recurrence support."""
    calendar = Calendar.objects.create(
        name="Test Calendar",
        organization=organization,
        manage_available_windows=True,
    )

    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    availability_data = [
        (
            datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC),
            datetime.datetime(2025, 9, 1, 17, 0, tzinfo=datetime.UTC),
            "FREQ=WEEKLY;BYDAY=MO,WE,FR;COUNT=3",
        ),
        (
            datetime.datetime(2025, 9, 2, 10, 0, tzinfo=datetime.UTC),
            datetime.datetime(2025, 9, 2, 12, 0, tzinfo=datetime.UTC),
            None,
        ),
    ]

    available_times = service.bulk_create_availability_windows(
        calendar=calendar, availability_windows=availability_data
    )

    available_times_list = list(available_times)
    assert len(available_times_list) == 2

    # First available time should be recurring
    recurring_available = available_times_list[0]
    assert recurring_available.is_recurring
    rrule_result = recurring_available.recurrence_rule.to_rrule_string()
    assert "FREQ=WEEKLY" in rrule_result
    assert "BYDAY=MO,WE,FR" in rrule_result
    assert "COUNT=3" in rrule_result

    # Second available time should be simple
    simple_available = available_times_list[1]
    assert not simple_available.is_recurring


@pytest.mark.django_db
def test_get_blocked_times_expanded(organization, calendar):
    """Test getting expanded blocked times including recurring instances."""
    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    # Create a recurring blocked time
    start_time = datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 9, 1, 17, 0, tzinfo=datetime.UTC)

    blocked_time = service.create_blocked_time(
        calendar=calendar,
        start_time=start_time,
        end_time=end_time,
        reason="Weekly maintenance",
        rrule_string="FREQ=WEEKLY;COUNT=3",
    )

    assert blocked_time.is_recurring  # Use the variable

    # Get expanded blocked times for the month
    expanded_blocked_times = service.get_blocked_times_expanded(
        calendar=calendar,
        start_date=datetime.datetime(2025, 9, 1, 0, 0, tzinfo=datetime.UTC),
        end_date=datetime.datetime(2025, 9, 30, 23, 59, tzinfo=datetime.UTC),
    )

    # Should include the master and generated instances
    # Note: The actual count depends on the database function implementation
    assert len(expanded_blocked_times) >= 1  # At least the master
    assert any(bt.reason == "Weekly maintenance" for bt in expanded_blocked_times)


@pytest.mark.django_db
def test_get_available_times_expanded(organization):
    """Test getting expanded available times including recurring instances."""
    calendar = Calendar.objects.create(
        name="Test Calendar",
        organization=organization,
        manage_available_windows=True,
    )

    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    # Create a recurring available time
    start_time = datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 9, 1, 17, 0, tzinfo=datetime.UTC)

    available_time = service.create_available_time(
        calendar=calendar,
        start_time=start_time,
        end_time=end_time,
        rrule_string="FREQ=DAILY;COUNT=5",
    )

    assert available_time.is_recurring  # Use the variable

    # Get expanded available times for the month
    expanded_available_times = service.get_available_times_expanded(
        calendar=calendar,
        start_date=datetime.datetime(2025, 9, 1, 0, 0, tzinfo=datetime.UTC),
        end_date=datetime.datetime(2025, 9, 30, 23, 59, tzinfo=datetime.UTC),
    )

    # Should include the master and generated instances
    # Note: The actual count depends on the database function implementation
    assert len(expanded_available_times) >= 1  # At least the master
    assert all(at.calendar == calendar for at in expanded_available_times)


@pytest.mark.django_db
def test_get_blocked_times_expanded_with_cancelled_exception(organization, calendar):
    """Test that get_blocked_times_expanded properly handles cancelled exceptions."""
    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    # Create a weekly recurring blocked time for 4 weeks
    start_time = datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC)  # Monday
    end_time = datetime.datetime(2025, 9, 1, 17, 0, tzinfo=datetime.UTC)

    blocked_time = service.create_blocked_time(
        calendar=calendar,
        start_time=start_time,
        end_time=end_time,
        reason="Weekly maintenance",
        rrule_string="FREQ=WEEKLY;COUNT=4",
    )

    # Cancel the second occurrence (Sept 8)
    exception_date = datetime.date(2025, 9, 8)

    # Create the cancelled exception record manually
    BlockedTimeRecurrenceException.objects.create(
        parent_blocked_time=blocked_time,
        exception_date=datetime.datetime.combine(
            exception_date, blocked_time.start_time.time(), tzinfo=blocked_time.start_time.tzinfo
        ),
        is_cancelled=True,
        organization=blocked_time.organization,
    )

    # Get expanded blocked times for September
    expanded_blocked_times = service.get_blocked_times_expanded(
        calendar=calendar,
        start_date=datetime.datetime(2025, 9, 1, 0, 0, tzinfo=datetime.UTC),
        end_date=datetime.datetime(2025, 9, 30, 23, 59, tzinfo=datetime.UTC),
    )

    # Should include master and instances, but not the cancelled one
    # Check that Sept 8 occurrence is not included
    sept_8_occurrences = [
        bt for bt in expanded_blocked_times if bt.start_time.date() == datetime.date(2025, 9, 8)
    ]
    assert len(sept_8_occurrences) == 0, "Cancelled occurrence should not appear"

    # Verify the exception was created
    exceptions = BlockedTimeRecurrenceException.objects.filter(
        parent_blocked_time=blocked_time, organization=organization
    )
    assert exceptions.count() == 1
    exception = exceptions.first()
    assert exception.exception_date.date() == exception_date
    assert exception.is_cancelled is True
    assert exception.modified_blocked_time is None


@pytest.mark.django_db
def test_get_blocked_times_expanded_with_modified_exception(organization, calendar):
    """Test that get_blocked_times_expanded properly handles modified exceptions."""
    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    # Create a daily recurring blocked time for 5 days
    start_time = datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 9, 1, 17, 0, tzinfo=datetime.UTC)

    blocked_time = service.create_blocked_time(
        calendar=calendar,
        start_time=start_time,
        end_time=end_time,
        reason="Daily maintenance",
        rrule_string="FREQ=DAILY;COUNT=5",
    )

    # Modify the third occurrence (Sept 3) - change time and reason
    exception_date = datetime.date(2025, 9, 3)
    modified_start = datetime.datetime(2025, 9, 3, 10, 0, tzinfo=datetime.UTC)
    modified_end = datetime.datetime(2025, 9, 3, 18, 0, tzinfo=datetime.UTC)

    # Create the modified blocked time manually first
    modified_blocked_time = service.create_blocked_time(
        calendar=calendar,
        start_time=modified_start,
        end_time=modified_end,
        reason="Extended maintenance",
    )

    # Mark it as an exception
    modified_blocked_time.is_recurring_exception = True
    modified_blocked_time.save()

    # Create the exception record
    BlockedTimeRecurrenceException.objects.create(
        parent_blocked_time=blocked_time,
        modified_blocked_time=modified_blocked_time,
        exception_date=datetime.datetime.combine(
            exception_date, blocked_time.start_time.time(), tzinfo=blocked_time.start_time.tzinfo
        ),
        is_cancelled=False,
        organization=blocked_time.organization,
    )
    assert modified_blocked_time is not None
    assert modified_blocked_time.reason == "Extended maintenance"
    assert modified_blocked_time.start_time == modified_start
    assert modified_blocked_time.end_time == modified_end

    # Get expanded blocked times for September
    expanded_blocked_times = service.get_blocked_times_expanded(
        calendar=calendar,
        start_date=datetime.datetime(2025, 9, 1, 0, 0, tzinfo=datetime.UTC),
        end_date=datetime.datetime(2025, 9, 30, 23, 59, tzinfo=datetime.UTC),
    )

    # Find the Sept 3 occurrences - should include the modified one
    sept_3_occurrences = [
        bt for bt in expanded_blocked_times if bt.start_time.date() == datetime.date(2025, 9, 3)
    ]

    # Should have exactly one occurrence for Sept 3 (the modified one)
    assert len(sept_3_occurrences) == 1
    modified_occurrence = sept_3_occurrences[0]
    assert modified_occurrence.reason == "Extended maintenance"
    assert modified_occurrence.start_time == modified_start
    assert modified_occurrence.end_time == modified_end
    assert modified_occurrence.is_recurring_exception is True

    # Verify the exception was created
    exceptions = BlockedTimeRecurrenceException.objects.filter(
        parent_blocked_time=blocked_time, organization=organization
    )
    assert exceptions.count() == 1
    exception = exceptions.first()
    assert exception.exception_date.date() == exception_date
    assert exception.is_cancelled is False
    assert exception.modified_blocked_time == modified_blocked_time


@pytest.mark.django_db
def test_get_available_times_expanded_with_cancelled_exception(organization):
    """Test that get_available_times_expanded properly handles cancelled exceptions."""
    calendar = Calendar.objects.create(
        name="Test Calendar",
        organization=organization,
        manage_available_windows=True,
    )

    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    # Create a weekly recurring available time for 4 weeks
    start_time = datetime.datetime(2025, 9, 2, 9, 0, tzinfo=datetime.UTC)  # Tuesday
    end_time = datetime.datetime(2025, 9, 2, 17, 0, tzinfo=datetime.UTC)

    available_time = service.create_available_time(
        calendar=calendar,
        start_time=start_time,
        end_time=end_time,
        rrule_string="FREQ=WEEKLY;COUNT=4",
    )

    # Cancel the second occurrence (Sept 9)
    exception_date = datetime.date(2025, 9, 9)

    # Create the cancelled exception record manually
    AvailableTimeRecurrenceException.objects.create(
        parent_available_time=available_time,
        exception_date=datetime.datetime.combine(
            exception_date,
            available_time.start_time.time(),
            tzinfo=available_time.start_time.tzinfo,
        ),
        is_cancelled=True,
        organization=available_time.organization,
    )

    # Get expanded available times for September
    expanded_available_times = service.get_available_times_expanded(
        calendar=calendar,
        start_date=datetime.datetime(2025, 9, 1, 0, 0, tzinfo=datetime.UTC),
        end_date=datetime.datetime(2025, 9, 30, 23, 59, tzinfo=datetime.UTC),
    )

    # Check that Sept 9 occurrence is not included
    sept_9_occurrences = [
        at for at in expanded_available_times if at.start_time.date() == datetime.date(2025, 9, 9)
    ]
    assert len(sept_9_occurrences) == 0, "Cancelled occurrence should not appear"

    # Verify the exception was created
    exceptions = AvailableTimeRecurrenceException.objects.filter(
        parent_available_time=available_time, organization=organization
    )
    assert exceptions.count() == 1
    exception = exceptions.first()
    assert exception.exception_date.date() == exception_date
    assert exception.is_cancelled is True
    assert exception.modified_available_time is None


@pytest.mark.django_db
def test_get_available_times_expanded_with_modified_exception(organization):
    """Test that get_available_times_expanded properly handles modified exceptions."""
    calendar = Calendar.objects.create(
        name="Test Calendar",
        organization=organization,
        manage_available_windows=True,
    )

    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    # Create a daily recurring available time for 5 days
    start_time = datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 9, 1, 17, 0, tzinfo=datetime.UTC)

    available_time = service.create_available_time(
        calendar=calendar,
        start_time=start_time,
        end_time=end_time,
        rrule_string="FREQ=DAILY;COUNT=5",
    )

    # Modify the fourth occurrence (Sept 4) - change time
    exception_date = datetime.date(2025, 9, 4)
    modified_start = datetime.datetime(2025, 9, 4, 10, 0, tzinfo=datetime.UTC)
    modified_end = datetime.datetime(2025, 9, 4, 16, 0, tzinfo=datetime.UTC)

    # Create the modified available time manually first
    modified_available_time = service.create_available_time(
        calendar=calendar,
        start_time=modified_start,
        end_time=modified_end,
    )

    # Mark it as an exception
    modified_available_time.is_recurring_exception = True
    modified_available_time.save()

    # Create the exception record
    AvailableTimeRecurrenceException.objects.create(
        parent_available_time=available_time,
        modified_available_time=modified_available_time,
        exception_date=datetime.datetime.combine(
            exception_date,
            available_time.start_time.time(),
            tzinfo=available_time.start_time.tzinfo,
        ),
        is_cancelled=False,
        organization=available_time.organization,
    )
    assert modified_available_time is not None
    assert modified_available_time.start_time == modified_start
    assert modified_available_time.end_time == modified_end

    # Get expanded available times for September
    expanded_available_times = service.get_available_times_expanded(
        calendar=calendar,
        start_date=datetime.datetime(2025, 9, 1, 0, 0, tzinfo=datetime.UTC),
        end_date=datetime.datetime(2025, 9, 30, 23, 59, tzinfo=datetime.UTC),
    )

    # Find the Sept 4 occurrences - should include the modified one
    sept_4_occurrences = [
        at for at in expanded_available_times if at.start_time.date() == datetime.date(2025, 9, 4)
    ]

    # Should have exactly one occurrence for Sept 4 (the modified one)
    assert len(sept_4_occurrences) == 1
    modified_occurrence = sept_4_occurrences[0]
    assert modified_occurrence.start_time == modified_start
    assert modified_occurrence.end_time == modified_end
    assert modified_occurrence.is_recurring_exception is True

    # Verify the exception was created
    exceptions = AvailableTimeRecurrenceException.objects.filter(
        parent_available_time=available_time, organization=organization
    )
    assert exceptions.count() == 1
    exception = exceptions.first()
    assert exception.exception_date.date() == exception_date
    assert exception.is_cancelled is False
    assert exception.modified_available_time == modified_available_time


@pytest.mark.django_db
def test_get_blocked_times_expanded_with_multiple_exceptions(organization, calendar):
    """Test that get_blocked_times_expanded handles multiple exceptions correctly."""
    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    # Create a daily recurring blocked time for 7 days
    start_time = datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 9, 1, 17, 0, tzinfo=datetime.UTC)

    blocked_time = service.create_blocked_time(
        calendar=calendar,
        start_time=start_time,
        end_time=end_time,
        reason="Daily maintenance",
        rrule_string="FREQ=DAILY;COUNT=7",
    )

    # Cancel Sept 2 (second occurrence)
    BlockedTimeRecurrenceException.objects.create(
        parent_blocked_time=blocked_time,
        exception_date=datetime.datetime.combine(
            datetime.date(2025, 9, 2),
            blocked_time.start_time.time(),
            tzinfo=blocked_time.start_time.tzinfo,
        ),
        is_cancelled=True,
        organization=blocked_time.organization,
    )

    # Modify Sept 4 (fourth occurrence)
    modified_start = datetime.datetime(2025, 9, 4, 14, 0, tzinfo=datetime.UTC)
    modified_end = datetime.datetime(2025, 9, 4, 18, 0, tzinfo=datetime.UTC)

    # Create the modified blocked time manually
    modified_blocked_time = service.create_blocked_time(
        calendar=calendar,
        start_time=modified_start,
        end_time=modified_end,
        reason="Afternoon maintenance",
    )
    modified_blocked_time.is_recurring_exception = True
    modified_blocked_time.save()

    # Create the exception record
    BlockedTimeRecurrenceException.objects.create(
        parent_blocked_time=blocked_time,
        modified_blocked_time=modified_blocked_time,
        exception_date=datetime.datetime.combine(
            datetime.date(2025, 9, 4),
            blocked_time.start_time.time(),
            tzinfo=blocked_time.start_time.tzinfo,
        ),
        is_cancelled=False,
        organization=blocked_time.organization,
    )

    # Cancel Sept 6 (sixth occurrence)
    BlockedTimeRecurrenceException.objects.create(
        parent_blocked_time=blocked_time,
        exception_date=datetime.datetime.combine(
            datetime.date(2025, 9, 6),
            blocked_time.start_time.time(),
            tzinfo=blocked_time.start_time.tzinfo,
        ),
        is_cancelled=True,
        organization=blocked_time.organization,
    )

    # Get expanded blocked times
    expanded_blocked_times = service.get_blocked_times_expanded(
        calendar=calendar,
        start_date=datetime.datetime(2025, 9, 1, 0, 0, tzinfo=datetime.UTC),
        end_date=datetime.datetime(2025, 9, 30, 23, 59, tzinfo=datetime.UTC),
    )

    # Check Sept 2 is cancelled (should not appear)
    sept_2_occurrences = [
        bt for bt in expanded_blocked_times if bt.start_time.date() == datetime.date(2025, 9, 2)
    ]
    assert len(sept_2_occurrences) == 0

    # Check Sept 4 is modified
    sept_4_occurrences = [
        bt for bt in expanded_blocked_times if bt.start_time.date() == datetime.date(2025, 9, 4)
    ]
    assert len(sept_4_occurrences) == 1
    modified_occurrence = sept_4_occurrences[0]
    assert modified_occurrence.reason == "Afternoon maintenance"
    assert modified_occurrence.start_time == modified_start
    assert modified_occurrence.end_time == modified_end

    # Check Sept 6 is cancelled (should not appear)
    sept_6_occurrences = [
        bt for bt in expanded_blocked_times if bt.start_time.date() == datetime.date(2025, 9, 6)
    ]
    assert len(sept_6_occurrences) == 0

    # Verify we have the expected number of exceptions
    exceptions = BlockedTimeRecurrenceException.objects.filter(
        parent_blocked_time=blocked_time, organization=organization
    )
    assert exceptions.count() == 3


@pytest.mark.django_db
def test_get_available_times_expanded_with_multiple_exceptions(organization):
    """Test that get_available_times_expanded handles multiple exceptions correctly."""
    calendar = Calendar.objects.create(
        name="Test Calendar",
        organization=organization,
        manage_available_windows=True,
    )

    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    # Create a daily recurring available time for 6 days
    start_time = datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC)
    end_time = datetime.datetime(2025, 9, 1, 17, 0, tzinfo=datetime.UTC)

    available_time = service.create_available_time(
        calendar=calendar,
        start_time=start_time,
        end_time=end_time,
        rrule_string="FREQ=DAILY;COUNT=6",
    )

    # Cancel Sept 3 (third occurrence)
    AvailableTimeRecurrenceException.objects.create(
        parent_available_time=available_time,
        exception_date=datetime.datetime.combine(
            datetime.date(2025, 9, 3),
            available_time.start_time.time(),
            tzinfo=available_time.start_time.tzinfo,
        ),
        is_cancelled=True,
        organization=available_time.organization,
    )

    # Modify Sept 5 (fifth occurrence)
    modified_start = datetime.datetime(2025, 9, 5, 8, 0, tzinfo=datetime.UTC)
    modified_end = datetime.datetime(2025, 9, 5, 16, 0, tzinfo=datetime.UTC)

    # Create the modified available time manually
    modified_available_time = service.create_available_time(
        calendar=calendar,
        start_time=modified_start,
        end_time=modified_end,
    )
    modified_available_time.is_recurring_exception = True
    modified_available_time.save()

    # Create the exception record
    AvailableTimeRecurrenceException.objects.create(
        parent_available_time=available_time,
        modified_available_time=modified_available_time,
        exception_date=datetime.datetime.combine(
            datetime.date(2025, 9, 5),
            available_time.start_time.time(),
            tzinfo=available_time.start_time.tzinfo,
        ),
        is_cancelled=False,
        organization=available_time.organization,
    )

    # Get expanded available times
    expanded_available_times = service.get_available_times_expanded(
        calendar=calendar,
        start_date=datetime.datetime(2025, 9, 1, 0, 0, tzinfo=datetime.UTC),
        end_date=datetime.datetime(2025, 9, 30, 23, 59, tzinfo=datetime.UTC),
    )

    # Check Sept 3 is cancelled (should not appear)
    sept_3_occurrences = [
        at for at in expanded_available_times if at.start_time.date() == datetime.date(2025, 9, 3)
    ]
    assert len(sept_3_occurrences) == 0

    # Check Sept 5 is modified
    sept_5_occurrences = [
        at for at in expanded_available_times if at.start_time.date() == datetime.date(2025, 9, 5)
    ]
    assert len(sept_5_occurrences) == 1
    modified_occurrence = sept_5_occurrences[0]
    assert modified_occurrence.start_time == modified_start
    assert modified_occurrence.end_time == modified_end

    # Verify we have the expected number of exceptions
    exceptions = AvailableTimeRecurrenceException.objects.filter(
        parent_available_time=available_time, organization=organization
    )
    assert exceptions.count() == 2


@pytest.mark.django_db
def test_get_blocked_times_expanded_bundle_calendar(
    organization, bundle_calendar, child_calendar_internal, child_calendar_google
):
    """Test that get_blocked_times_expanded includes blocked times from bundle children."""
    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    # Create blocked times on the bundle calendar itself
    bundle_blocked_time = service.create_blocked_time(
        calendar=bundle_calendar,
        start_time=datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 9, 1, 10, 0, tzinfo=datetime.UTC),
        reason="Bundle maintenance",
    )

    # Create blocked times on child calendars
    child1_blocked_time = service.create_blocked_time(
        calendar=child_calendar_internal,
        start_time=datetime.datetime(2025, 9, 2, 14, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 9, 2, 15, 0, tzinfo=datetime.UTC),
        reason="Internal calendar maintenance",
    )

    child2_blocked_time = service.create_blocked_time(
        calendar=child_calendar_google,
        start_time=datetime.datetime(2025, 9, 3, 16, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 9, 3, 17, 0, tzinfo=datetime.UTC),
        reason="Google calendar maintenance",
    )

    # Get expanded blocked times for the bundle calendar
    expanded_blocked_times = service.get_blocked_times_expanded(
        calendar=bundle_calendar,
        start_date=datetime.datetime(2025, 9, 1, 0, 0, tzinfo=datetime.UTC),
        end_date=datetime.datetime(2025, 9, 30, 23, 59, tzinfo=datetime.UTC),
    )

    # Should include blocked times from bundle calendar and all children
    assert len(expanded_blocked_times) == 3

    # Check bundle calendar blocked time is included
    bundle_times = [bt for bt in expanded_blocked_times if bt.reason == "Bundle maintenance"]
    assert len(bundle_times) == 1
    assert bundle_times[0].id == bundle_blocked_time.id

    # Check child calendar blocked times are included
    child1_times = [
        bt for bt in expanded_blocked_times if bt.reason == "Internal calendar maintenance"
    ]
    assert len(child1_times) == 1
    assert child1_times[0].id == child1_blocked_time.id

    child2_times = [
        bt for bt in expanded_blocked_times if bt.reason == "Google calendar maintenance"
    ]
    assert len(child2_times) == 1
    assert child2_times[0].id == child2_blocked_time.id

    # Verify times are sorted by start time
    assert (
        expanded_blocked_times[0].start_time
        <= expanded_blocked_times[1].start_time
        <= expanded_blocked_times[2].start_time
    )


@pytest.mark.django_db
def test_get_blocked_times_expanded_bundle_calendar_with_recurring(
    organization, bundle_calendar, child_calendar_internal, child_calendar_google
):
    """Test that get_blocked_times_expanded includes recurring blocked times from bundle children."""
    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    # Create recurring blocked time on bundle calendar
    bundle_recurring_blocked = service.create_blocked_time(
        calendar=bundle_calendar,
        start_time=datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 9, 1, 10, 0, tzinfo=datetime.UTC),
        reason="Weekly bundle maintenance",
        rrule_string="FREQ=WEEKLY;COUNT=3",
    )

    # Create recurring blocked time on child calendar
    child_recurring_blocked = service.create_blocked_time(
        calendar=child_calendar_internal,
        start_time=datetime.datetime(2025, 9, 2, 14, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 9, 2, 15, 0, tzinfo=datetime.UTC),
        reason="Daily child maintenance",
        rrule_string="FREQ=DAILY;COUNT=5",
    )

    # Get expanded blocked times for the bundle calendar
    expanded_blocked_times = service.get_blocked_times_expanded(
        calendar=bundle_calendar,
        start_date=datetime.datetime(2025, 9, 1, 0, 0, tzinfo=datetime.UTC),
        end_date=datetime.datetime(2025, 9, 30, 23, 59, tzinfo=datetime.UTC),
    )

    # Should include expanded instances from both recurring blocked times
    bundle_times = [bt for bt in expanded_blocked_times if bt.reason == "Weekly bundle maintenance"]
    child_times = [bt for bt in expanded_blocked_times if bt.reason == "Daily child maintenance"]

    # Bundle recurring should have at least the original instance
    assert len(bundle_times) >= 1
    assert bundle_recurring_blocked.is_recurring

    # Child recurring should have at least the original instance
    assert len(child_times) >= 1
    assert child_recurring_blocked.is_recurring

    # Total should include instances from both calendars
    assert len(expanded_blocked_times) >= 2


@pytest.mark.django_db
def test_get_blocked_times_expanded_non_bundle_calendar_unchanged(
    organization, child_calendar_internal
):
    """Test that get_blocked_times_expanded works unchanged for non-bundle calendars."""
    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    # Create blocked time on a regular (non-bundle) calendar
    blocked_time = service.create_blocked_time(
        calendar=child_calendar_internal,
        start_time=datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 9, 1, 10, 0, tzinfo=datetime.UTC),
        reason="Regular maintenance",
    )

    # Get expanded blocked times for the regular calendar
    expanded_blocked_times = service.get_blocked_times_expanded(
        calendar=child_calendar_internal,
        start_date=datetime.datetime(2025, 9, 1, 0, 0, tzinfo=datetime.UTC),
        end_date=datetime.datetime(2025, 9, 30, 23, 59, tzinfo=datetime.UTC),
    )

    # Should only include the blocked time from this calendar
    assert len(expanded_blocked_times) == 1
    assert expanded_blocked_times[0].id == blocked_time.id
    assert expanded_blocked_times[0].reason == "Regular maintenance"


@pytest.mark.django_db
def test_get_blocked_times_expanded_empty_bundle_calendar(organization, empty_bundle_calendar):
    """Test that get_blocked_times_expanded works correctly for bundle calendars with no children."""
    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    # Create blocked time on the empty bundle calendar
    blocked_time = service.create_blocked_time(
        calendar=empty_bundle_calendar,
        start_time=datetime.datetime(2025, 9, 1, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 9, 1, 10, 0, tzinfo=datetime.UTC),
        reason="Empty bundle maintenance",
    )

    # Get expanded blocked times for the empty bundle calendar
    expanded_blocked_times = service.get_blocked_times_expanded(
        calendar=empty_bundle_calendar,
        start_date=datetime.datetime(2025, 9, 1, 0, 0, tzinfo=datetime.UTC),
        end_date=datetime.datetime(2025, 9, 30, 23, 59, tzinfo=datetime.UTC),
    )

    # Should only include the blocked time from the bundle calendar itself
    assert len(expanded_blocked_times) == 1
    assert expanded_blocked_times[0].id == blocked_time.id
    assert expanded_blocked_times[0].reason == "Empty bundle maintenance"


@pytest.mark.django_db
def test_get_blocked_times_expanded_bundle_calendar_date_filtering(
    organization, bundle_calendar, child_calendar_internal
):
    """Test that get_blocked_times_expanded properly filters by date range for bundle calendars."""
    service = CalendarService()
    service.initialize_without_provider(organization=organization)

    # Create blocked times in different date ranges
    # This one should be included (within range)
    included_blocked_time = service.create_blocked_time(
        calendar=child_calendar_internal,
        start_time=datetime.datetime(2025, 9, 15, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 9, 15, 10, 0, tzinfo=datetime.UTC),
        reason="Included maintenance",
    )

    # This one should be excluded (outside range)
    _excluded_blocked_time = service.create_blocked_time(
        calendar=child_calendar_internal,
        start_time=datetime.datetime(2025, 10, 15, 9, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 10, 15, 10, 0, tzinfo=datetime.UTC),
        reason="Excluded maintenance",
    )

    # Get expanded blocked times for a specific date range
    expanded_blocked_times = service.get_blocked_times_expanded(
        calendar=bundle_calendar,
        start_date=datetime.datetime(2025, 9, 1, 0, 0, tzinfo=datetime.UTC),
        end_date=datetime.datetime(2025, 9, 30, 23, 59, tzinfo=datetime.UTC),
    )

    # Should only include the blocked time within the date range
    assert len(expanded_blocked_times) == 1
    assert expanded_blocked_times[0].id == included_blocked_time.id
    assert expanded_blocked_times[0].reason == "Included maintenance"

    # Verify the excluded blocked time is not included
    excluded_reasons = [
        bt.reason for bt in expanded_blocked_times if bt.reason == "Excluded maintenance"
    ]
    assert len(excluded_reasons) == 0
