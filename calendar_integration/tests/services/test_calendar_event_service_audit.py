"""Audit-emission tests for ``CalendarEventService`` business writes.

These drive the real event sub-service (built from a facade whose ``audit_service``
is injected via the DI container, exactly as the production wiring does) and assert
that ``create_event`` / ``update_event`` / ``delete_event`` enqueue the expected audit
records. We patch ``audit.services.persist_audit_record`` and execute the on_commit
callbacks so the enqueue happens, then inspect the serialized payloads. Mechanical /
sync writes (attendees, resources, recurrence rule) are intentionally NOT audited and
must not appear in the payloads.
"""

import datetime
from unittest.mock import Mock, patch

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken

from audit.constants import AuditAction
from calendar_integration.constants import CalendarProvider
from calendar_integration.models import Calendar, CalendarManagementToken
from calendar_integration.services.calendar_event_service import CalendarEventService
from calendar_integration.services.calendar_permission_service import (
    DEFAULT_CALENDAR_OWNER_PERMISSIONS,
)
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    CalendarEventAdapterOutputData,
    CalendarEventInputData,
    EventAttendanceInputData,
)
from organizations.models import Organization, OrganizationMembership
from users.models import Profile, User


@pytest.fixture
def mock_google_adapter():
    with patch(
        "calendar_integration.services.calendar_adapters.google_calendar_adapter.GoogleCalendarAdapter"
    ) as mock_adapter_class:
        mock_adapter = Mock()
        mock_adapter.provider = CalendarProvider.GOOGLE
        del mock_adapter.resolve_expression
        del mock_adapter.get_source_expressions
        mock_adapter_class.return_value = mock_adapter
        mock_adapter_class.from_service_account_credentials.return_value = mock_adapter
        yield mock_adapter


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Event Audit Org", should_sync_rooms=False)


@pytest.fixture
def social_account(db):
    user = User.objects.create_user(email="event-audit@example.com", password="testpass123")
    Profile.objects.create(user=user)
    return SocialAccount.objects.create(user=user, provider=CalendarProvider.GOOGLE, uid="88888")


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
        name="Event Audit Calendar",
        description="A test calendar",
        external_id="evt_audit_cal_1",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )


@pytest.fixture
def calendar_management_token(db, calendar, social_account):
    OrganizationMembership.objects.get_or_create(
        user=social_account.user, organization=calendar.organization
    )
    token = CalendarManagementToken.objects.create(
        calendar=calendar,
        membership_user_id=social_account.user.id,
        token_hash="evt_audit_token_hash",
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
    service = CalendarService()
    service.authenticate(account=social_account.user, organization=calendar.organization)
    return service


@pytest.fixture
def event_service(authenticated_facade):
    return CalendarEventService(
        context=authenticated_facade._context,
        recurrence_manager=authenticated_facade._recurrence_manager,
        calendar_cache=authenticated_facade._calendar_cache,
        host=authenticated_facade,
    )


def _grant_event_owner_token(event, user, organization):
    OrganizationMembership.objects.get_or_create(user=user, organization=organization)
    token = CalendarManagementToken.objects.create(
        event_fk=event,
        membership_user_id=user.id,
        token_hash=f"evt_audit_token_{event.id}",
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
        title="Audit Event",
        description="An audited event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )


def _adapter_output(external_id: str):
    return CalendarEventAdapterOutputData(
        calendar_external_id="evt_audit_cal_1",
        external_id=external_id,
        title="Audit Event",
        description="An audited event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=None,
    )


def _payloads(mock_task) -> list[dict]:
    return [call.args[0] for call in mock_task.delay.call_args_list]


@pytest.mark.django_db
def test_create_event_records_create(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    sample_event_input_data,
    social_account,
    django_capture_on_commit_callbacks,
):
    mock_google_adapter.create_event.return_value = _adapter_output("event_audit_create")

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            event = event_service.create_event(calendar.id, sample_event_input_data)

    payloads = _payloads(mock_task)
    event_payloads = [
        p for p in payloads if p["subject"]["subject_type"] == "calendar_integration.CalendarEvent"
    ]
    assert len(event_payloads) == 1
    payload = event_payloads[0]
    assert payload["action"] == AuditAction.CREATE
    assert payload["subject"]["subject_id"] == str(event.id)
    assert payload["subject"]["subject_label"] == "Audit Event"
    assert payload["organization_id"] == calendar.organization_id
    assert payload["actor"]["actor_type"] == "membership"
    assert payload["actor"]["actor_id"] == social_account.user.id
    assert payload["diff"] is None
    # With no attendees, the acting member (organizer/host) is the only affected party.
    assert payload["affected_membership_ids"] == [social_account.user.id]


@pytest.mark.django_db
def test_create_event_affected_memberships_include_organizer_and_attendees(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    social_account,
    django_capture_on_commit_callbacks,
):
    """affected_membership_ids = the acting member (organizer) + internal attendees."""
    # A second org member who attends the event.
    attendee_user = User.objects.create_user(email="attendee@example.com", password="pw12345")
    Profile.objects.create(user=attendee_user)
    OrganizationMembership.objects.create(user=attendee_user, organization=calendar.organization)

    event_input = CalendarEventInputData(
        title="Audit Event",
        description="An audited event",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendances=[EventAttendanceInputData(user_id=attendee_user.id)],
        external_attendances=[],
        resource_allocations=[],
    )
    mock_google_adapter.create_event.return_value = _adapter_output("event_audit_affected")

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            event_service.create_event(calendar.id, event_input)

    event_payloads = [
        p
        for p in _payloads(mock_task)
        if p["subject"]["subject_type"] == "calendar_integration.CalendarEvent"
    ]
    assert len(event_payloads) == 1
    affected = event_payloads[0]["affected_membership_ids"]
    # Both the organizer (acting member) and the internal attendee are affected.
    assert affected == sorted({social_account.user.id, attendee_user.id})


@pytest.mark.django_db
def test_update_event_records_update_with_diff(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    sample_event_input_data,
    social_account,
    django_capture_on_commit_callbacks,
):
    mock_google_adapter.create_event.return_value = _adapter_output("event_audit_update")
    mock_google_adapter.update_event.return_value = _adapter_output("event_audit_update")

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

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            event_service.update_event(calendar.id, created.id, updated_input)

    event_payloads = [
        p
        for p in _payloads(mock_task)
        if p["subject"]["subject_type"] == "calendar_integration.CalendarEvent"
    ]
    assert len(event_payloads) == 1
    payload = event_payloads[0]
    assert payload["action"] == AuditAction.UPDATE
    assert payload["subject"]["subject_id"] == str(created.id)
    diff = payload["diff"]
    assert diff is not None
    assert diff["title"] == {"old": "Audit Event", "new": "Updated Title"}
    assert diff["description"] == {"old": "An audited event", "new": "Updated description"}
    # Datetime fields are serialized to ISO strings so the diff stays JSON-safe.
    assert "start_time_tz_unaware" in diff
    assert "end_time_tz_unaware" in diff
    # No attendee / resource keys leak into the event diff.
    assert set(diff.keys()) <= {
        "title",
        "description",
        "start_time_tz_unaware",
        "end_time_tz_unaware",
        "timezone",
    }


@pytest.mark.django_db
def test_delete_event_records_delete(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    sample_event_input_data,
    social_account,
    django_capture_on_commit_callbacks,
):
    mock_google_adapter.create_event.return_value = _adapter_output("event_audit_delete")

    created = event_service.create_event(calendar.id, sample_event_input_data)
    created_id = created.id
    _grant_event_owner_token(created, social_account.user, calendar.organization)

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            event_service.delete_event(calendar.id, created_id)

    event_payloads = [
        p
        for p in _payloads(mock_task)
        if p["subject"]["subject_type"] == "calendar_integration.CalendarEvent"
    ]
    assert len(event_payloads) == 1
    payload = event_payloads[0]
    assert payload["action"] == AuditAction.DELETE
    assert payload["subject"]["subject_id"] == str(created_id)
    assert payload["organization_id"] == calendar.organization_id
