"""Direct unit tests for ``CalendarEventService``.

These construct the event sub-service directly from a shared
:class:`CalendarServiceContext` (built by authenticating a facade, then reused —
the perf guardrail) and exercise create / update / delete of a single event, a
recurring-event path, and transfer against a mocked adapter. The exhaustive
event-behavior coverage stays in ``test_calendar_service.py`` against the facade;
these assert the extracted service behaves identically in isolation and that the
facade forwards to it.
"""

import datetime
from unittest.mock import Mock, patch

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken

from calendar_integration.constants import CalendarProvider
from calendar_integration.models import Calendar, CalendarEvent, CalendarManagementToken
from calendar_integration.services.calendar_event_service import CalendarEventService
from calendar_integration.services.calendar_permission_service import (
    DEFAULT_CALENDAR_OWNER_PERMISSIONS,
)
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    CalendarEventAdapterOutputData,
    CalendarEventInputData,
)
from organizations.models import Organization
from users.models import Profile, User


@pytest.fixture
def mock_google_adapter():
    """Mock Google Calendar adapter (mirrors the facade test suite fixture)."""
    with patch(
        "calendar_integration.services.calendar_adapters.google_calendar_adapter.GoogleCalendarAdapter"
    ) as mock_adapter_class:
        mock_adapter = Mock()
        mock_adapter.provider = CalendarProvider.GOOGLE
        # Avoid Django expression-resolution attribute hits on the Mock.
        del mock_adapter.resolve_expression
        del mock_adapter.get_source_expressions
        mock_adapter_class.return_value = mock_adapter
        mock_adapter_class.from_service_account_credentials.return_value = mock_adapter
        yield mock_adapter


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Event Service Org", should_sync_rooms=False)


@pytest.fixture
def social_account(db):
    user = User.objects.create_user(email="event-service@example.com", password="testpass123")
    Profile.objects.create(user=user)
    return SocialAccount.objects.create(user=user, provider=CalendarProvider.GOOGLE, uid="99999")


@pytest.fixture
def social_token(social_account):
    return SocialToken.objects.create(
        account=social_account,
        token="test_access_token",
        token_secret="test_refresh_token",
        expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
    )


@pytest.fixture
def calendar(db, organization):
    return Calendar.objects.create(
        name="Event Service Calendar",
        description="A test calendar",
        external_id="evt_cal_1",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )


@pytest.fixture
def calendar_management_token(db, calendar, social_account):
    token = CalendarManagementToken.objects.create(
        calendar=calendar,
        user=social_account.user,
        token_hash="evt_service_token_hash",
        organization=calendar.organization,
    )
    token.permissions.all().delete()
    for permission_str in DEFAULT_CALENDAR_OWNER_PERMISSIONS:
        token.permissions.create(
            permission=permission_str,
            organization_id=calendar.organization_id,
        )
    return token


@pytest.fixture
def authenticated_facade(social_account, social_token, mock_google_adapter, calendar):
    """An authenticated facade — used only as the source of the shared context."""
    service = CalendarService()
    service.authenticate(account=social_account.user, organization=calendar.organization)
    return service


@pytest.fixture
def event_service(authenticated_facade):
    """``CalendarEventService`` wired from the facade's shared context.

    The facade is supplied as the ``host`` (availability — Phase 4, bundle fan-out —
    Phase 3, and the shared write-adapter / attendee-permission helpers route through
    it), exactly the wiring the facade uses internally.
    """
    return CalendarEventService(
        context=authenticated_facade._context,
        recurrence_manager=authenticated_facade._recurrence_manager,
        calendar_cache=authenticated_facade._calendar_cache,
        host=authenticated_facade,
    )


def _grant_event_owner_token(event, user, organization):
    """Create an owner-level event management token so update/delete permission passes."""
    token = CalendarManagementToken.objects.create(
        event_fk=event,
        user=user,
        token_hash=f"evt_token_{event.id}",
        organization=organization,
    )
    token.permissions.all().delete()
    for permission_str in DEFAULT_CALENDAR_OWNER_PERMISSIONS:
        token.permissions.create(
            permission=permission_str,
            organization_id=organization.id,
        )
    return token


@pytest.fixture
def sample_event_input_data():
    return CalendarEventInputData(
        title="New Event",
        description="A new event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )


def _adapter_output(external_id: str, *, recurrence_rule: str | None = None):
    return CalendarEventAdapterOutputData(
        calendar_external_id="evt_cal_1",
        external_id=external_id,
        title="New Event",
        description="A new event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=recurrence_rule,
    )


@pytest.mark.django_db
def test_create_event(
    event_service, mock_google_adapter, calendar, calendar_management_token, sample_event_input_data
):
    """The service creates the event and forwards the adapter input."""
    mock_google_adapter.create_event.return_value = _adapter_output("event_new_123")

    result = event_service.create_event(calendar.id, sample_event_input_data)

    assert result.external_id == "event_new_123"
    assert result.title == "New Event"
    assert result.calendar == calendar
    mock_google_adapter.create_event.assert_called_once()


@pytest.mark.django_db
def test_create_recurring_event(
    event_service, mock_google_adapter, calendar, calendar_management_token
):
    """The recurring create-shortcut persists the recurrence rule."""
    mock_google_adapter.create_event.return_value = _adapter_output(
        "recurring_event_123", recurrence_rule="RRULE:FREQ=WEEKLY;COUNT=10;BYDAY=MO"
    )

    result = event_service.create_recurring_event(
        calendar_id=calendar.id,
        title="Weekly Meeting",
        description="Recurring weekly meeting",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        recurrence_rule="RRULE:FREQ=WEEKLY;COUNT=10;BYDAY=MO",
    )

    assert result.external_id == "recurring_event_123"
    assert result.is_recurring is True
    assert result.recurrence_rule is not None


@pytest.mark.django_db
def test_update_event(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    sample_event_input_data,
    social_account,
):
    """The service updates an existing event in place."""
    mock_google_adapter.create_event.return_value = _adapter_output("event_to_update")
    mock_google_adapter.update_event.return_value = _adapter_output("event_to_update")

    created = event_service.create_event(calendar.id, sample_event_input_data)
    _grant_event_owner_token(created, social_account.user, calendar.organization)

    updated_input = CalendarEventInputData(
        title="Updated Title",
        description="Updated description",
        start_time=datetime.datetime(2025, 6, 22, 12, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 13, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )

    result = event_service.update_event(calendar.id, created.id, updated_input)

    assert result.id == created.id
    assert result.title == "Updated Title"
    mock_google_adapter.update_event.assert_called_once()


@pytest.mark.django_db
def test_delete_event(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    sample_event_input_data,
    social_account,
):
    """The service deletes the event and calls the adapter delete."""
    mock_google_adapter.create_event.return_value = _adapter_output("event_to_delete")

    created = event_service.create_event(calendar.id, sample_event_input_data)
    created_id = created.id
    _grant_event_owner_token(created, social_account.user, calendar.organization)

    event_service.delete_event(calendar.id, created_id)

    assert not CalendarEvent.objects.filter(
        id=created_id, organization_id=calendar.organization_id
    ).exists()
    mock_google_adapter.delete_event.assert_called_once()


@pytest.mark.django_db
def test_transfer_event(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    sample_event_input_data,
    social_account,
):
    """Transfer creates a new event on the target calendar and removes the original."""
    mock_google_adapter.create_event.side_effect = [
        _adapter_output("original_event"),
        _adapter_output("transferred_event"),
    ]
    mock_google_adapter.get_event.return_value = _adapter_output("original_event")

    original = event_service.create_event(calendar.id, sample_event_input_data)
    _grant_event_owner_token(original, social_account.user, calendar.organization)

    target_calendar = Calendar.objects.create(
        name="Target Calendar",
        external_id="evt_target_cal_1",
        provider=CalendarProvider.GOOGLE,
        organization=calendar.organization,
    )
    target_token = CalendarManagementToken.objects.create(
        calendar=target_calendar,
        user=social_account.user,
        token_hash="evt_target_token_hash",
        organization=calendar.organization,
    )
    target_token.permissions.all().delete()
    for permission_str in DEFAULT_CALENDAR_OWNER_PERMISSIONS:
        target_token.permissions.create(
            permission=permission_str,
            organization_id=calendar.organization_id,
        )

    new_event = event_service.transfer_event(original, target_calendar)

    assert new_event.calendar == target_calendar
    assert new_event.external_id == "transferred_event"
    assert not CalendarEvent.objects.filter(
        id=original.id, organization_id=calendar.organization_id
    ).exists()


@pytest.mark.django_db
def test_facade_create_event_delegates_to_event_service(
    authenticated_facade, sample_event_input_data
):
    """The facade's ``create_event`` forwards args + result to the event sub-service."""
    sentinel = object()
    fake_event_service = Mock()
    fake_event_service.create_event.return_value = sentinel

    with patch.object(authenticated_facade, "_get_event_service", return_value=fake_event_service):
        result = authenticated_facade.create_event(123, sample_event_input_data)

    assert result is sentinel
    fake_event_service.create_event.assert_called_once_with(123, sample_event_input_data)
